#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Delta Cardkey HTTP API
# 启动: python server.py --port 2233

import sys, os, time, asyncio, argparse, hashlib, threading, json, tempfile
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

try:                     # Linux 才有；Windows 上多进程缓存合并退化为单进程语义
    import fcntl
except ImportError:      # pragma: no cover
    fcntl = None

from fastapi import FastAPI, Request
import uvicorn
# ORJSONResponse 这个类在新版 FastAPI 里总是能被 import 到，即使没装 orjson ——
# 真正的检查在 render() 里的 `assert orjson is not None`，于是每一条请求都会
# AssertionError -> 500（求解已经跑完、key 也拿到了，只是死在序列化上）。
# 所以这里必须先确认 orjson 本身可用，拿不到才退回纯 JSON 实现。
try:
    import orjson  # noqa: F401
    from fastapi.responses import ORJSONResponse as JSONResponse
except Exception:
    from fastapi.responses import JSONResponse

# 先把 stdout 编码修好再打印任何东西（详见 auth_client.enable_utf8_stdio）
import auth_client as AUTH
from main import solve_chain
from auth_client import extract_ticket, MIN_TICKET_LEN

# 自愈线程（探测 + 修复 + 学习）。以前这是独立脚本 probe_gate.py --watch，
# 服务器上只让跑一个进程，所以收进服务内部。
try:
    import selfheal as HEAL
    import link_source as LINK
    import fastlane as FL
    import retryq as RQ
except Exception as _e:                                  # pragma: no cover
    HEAL, LINK, FL, RQ = None, None, None, None
    print(f'[server] 自愈模块不可用: {type(_e).__name__}: {_e}', flush=True)

_healer = None
_healer_announced = False
_queue = None


class SolveStats(object):
    """最近 N 次求解的耗时/成败环形统计。只存内存，进程重启即清零。"""

    def __init__(self, cap=300):
        self.cap = cap
        self.rows = []
        self.lock = threading.Lock()
        self.total = {'ok': 0, 'fail': 0, 'cache': 0, 'retry_hit': 0}

    def add(self, secs, ok, cached=False, retry_hit=False):
        with self.lock:
            self.rows.append((float(secs or 0), bool(ok)))
            if len(self.rows) > self.cap:
                self.rows.pop(0)
            self.total['cache' if cached else ('ok' if ok else 'fail')] += 1
            if retry_hit:
                self.total['retry_hit'] += 1

    def snapshot(self):
        with self.lock:
            rows = list(self.rows)
            total = dict(self.total)
        secs = sorted(x[0] for x in rows)
        n = len(secs)

        def pct(p):
            return round(secs[min(n - 1, int(n * p))], 3) if n else None

        decided = sum(1 for _, ok in rows if ok)
        return {
            'sample': n,
            'success_rate': round(decided / n, 4) if n else None,
            'avg': round(sum(secs) / n, 3) if n else None,
            'p50': pct(0.5), 'p90': pct(0.9), 'p95': pct(0.95),
            'max': round(secs[-1], 3) if n else None,
            'total': total,
        }


STATS = SolveStats()


def _stat_key(ticket):
    return hashlib.md5(ticket.encode()).hexdigest()


def _remember_probe_ticket(ticket):
    """把最近一条成功票据记下来，供自愈探求解链路。

    写 state 文件有 IO，限频到 30s 一次；票据本身也可能无效（上游过期），
    probe_solve 对无效票有专门处理，不会误触发修复。
    """
    if LINK is None:
        return
    try:
        st = LINK.load_state()
        if st.get('probe_ticket') == ticket and \
                time.time() - float(st.get('probe_ticket_at') or 0) < 1800:
            return
        st['probe_ticket'] = ticket
        st['probe_ticket_at'] = time.time()
        LINK.save_state(st)
    except Exception:
        pass


def _start_queue():
    """幂等启动后台重试队列。失败入队 + 指数退避 + 成功写缓存。"""
    global _queue
    if RQ is None:
        return None
    if _queue is None:
        _queue = RQ.default()

    def solve_for_queue(ticket):
        # 单发、不递归入队：队列自己负责退避和次数
        return run_solves(ticket, enqueue=False, escalate=False, single=True)

    def on_success(_h, rec, key, solve_time):
        try:
            _CACHE.put(rec['ticket'], key, solve_time,
                       _stat_key(rec['ticket']))
            _remember_probe_ticket(rec['ticket'])
        except Exception:
            pass

    _queue.start(solve_for_queue, on_success=on_success)
    return _queue


def _stop_queue():
    if _queue is not None:
        try:
            _queue.stop()
        except Exception:
            pass


def _start_healer():
    """幂等启动。DELTA_HEAL=0 时只创建不启动（/heal 仍可手动触发）。"""
    global _healer, _healer_announced
    if HEAL is None:
        return None
    if _healer is None:
        _healer = HEAL.default()
    if not HEAL.enabled():
        if not _healer_announced:
            print('[server] 自愈线程已禁用 (DELTA_HEAL=0)，/heal 仍可手动触发',
                  flush=True)
            _healer_announced = True
        return _healer
    _healer.start()
    return _healer


def _stop_healer():
    if _healer is not None:
        try:
            _healer.stop()
        except Exception:
            pass


def _gate_from_solve(err, timer):
    """把一次求解失败翻译成链接门禁的类型。

    只翻译"自愈能对症下手"的那几类，其余一律不喂 —— 让自愈去处理
    "链接无效"这种没有修复动作的失败，只会白白打一轮上游。
    """
    if not err:
        return None
    reason = (getattr(timer, 'fail_reason', None) or '')
    low = str(err).lower()
    if reason == 'network-error' or 'network-error' in low \
            or '不可达' in str(err) or 'timeout' in low \
            or 'connection' in low or 'proxy' in low or 'refused' in low:
        return (LINK.G_OFFLINE if LINK else None)
    if any(k in low for k in ('captcha', 'turnstile', 'hcaptcha')):
        return (LINK.G_CAPTCHA if LINK else None)
    if any(k in low for k in ('outdated client', 'unauthorized', 'forbidden')):
        return (LINK.G_AUTH if LINK else None)
    if any(k in low for k in ('too fast', 'slow down', 'too many', '429',
                              'rate limit')):
        return (LINK.G_RATE if LINK else None)
    if any(k in low for k in ('required property', 'schema')):
        return (LINK.G_SCHEMA if LINK else None)
    return None


def _note_solve_failure(err, timer):
    if _healer is None or LINK is None:
        return
    try:
        gate = _gate_from_solve(err, timer)
        if gate:
            _healer.note_failure(gate, f'{getattr(timer, "fail_reason", "")} / {err}',
                                 source='solve')
    except Exception:
        pass


@asynccontextmanager
async def _lifespan(_app):
    """启动/退出钩子。

    【为什么不用 app.on_event / add_event_handler】FastAPI 0.165 已经把
    `add_event_handler` 从 FastAPI 对象上移除了（只剩 router 上还有），
    用老写法会抛 AttributeError —— 而外面套了 try/except，等于**静默没注册**，
    只有真正重启时才发现"刚出的 key 又没了"。lifespan 是长期稳定的写法。

    自愈线程也挂在这里：uvicorn server:app 和 python server.py 两种启动方式
    都会走 lifespan，放 __main__ 里的话被 import 拉起就不会启动。
    """
    _start_healer()
    _start_queue()
    if FL is not None:
        FL.prewarm(background=True)      # 首解不再现付 TLS 握手
    yield
    _stop_healer()
    _stop_queue()
    _flush_on_exit()


app = FastAPI(title="Delta Cardkey", default_response_class=JSONResponse,
              lifespan=_lifespan)
lock = threading.Lock()

# ---------------------------------------------------------------------------
# 缓存文件位置
#
# 原来写死在脚本目录。本机测试没问题，但服务器上通常是 /opt/delta 归 root、
# 服务用低权限用户跑 —— 打开文件就 PermissionError，被 except 吞掉，于是
# 每次重启都从零开始重解，缓存形同不存在，且日志里一个字都不说。
# 这里改成：环境变量 > 脚本目录 > /var/lib/delta > ~/.cache > /tmp，
# 选第一个真能写的，并把最终落点打到启动日志里。
# ---------------------------------------------------------------------------
# 缓存实现已抽到 key_cache.py（CLI 与 server 共用一份路径探测 / flock 合并写入）。
# 这里只做一层薄绑定，对外函数名保持不变，路由与 healthz 无需改动。
import key_cache as KC

_writable = KC._writable          # healthz 里要探测可写性，沿用同名

_CACHE = KC.default()
CACHE_FILE = _CACHE.path
CACHE_SOURCE = _CACHE.source
CACHE_TTL = _CACHE.ttl
CACHE_LOCK_FILE = _CACHE.lock_file

key_cache = _CACHE.data           # 稳定引用（KeyCache.load 原地更新，不脱钩）
SAVE_DEBOUNCE = 2.0               # 最多每 2s 落盘一次


def load_cache():
    return _CACHE.load()


def save_cache_now():
    return _CACHE.save_now()


def cache_get(ticket, h=None):
    return _CACHE.get(ticket, h)


def cache_put(ticket, key, solve_time, h=None):
    return _CACHE.put(ticket, key, solve_time, h)


_CACHE.start_writer(SAVE_DEBOUNCE)   # 后台 debounce 落盘线程

# 可选鉴权：设了 DELTA_TOKEN 就必须带 X-Token 头（或 ?token=）。
# 默认不设 = 不鉴权，保持原来的调用方式不变。
API_TOKEN = (os.environ.get('DELTA_TOKEN') or '').strip()

MADE_BY = "Hasl_Team"
QQ_GROUP = "277707901"
inflight = {}
START_TS = time.time()

# 模块级默认值，保证 `uvicorn server:app` 直接拉起也能跑（不经过 __main__ 时
# 下面那个按 --workers 重建的逻辑不会执行）
executor = ThreadPoolExecutor(max_workers=10, thread_name_prefix="solve")


def fmt_t(secs):
    # 以前是 round(x, 12)，输出 "1.023661375046s" 这种噪声，端口里毫无意义。
    # 3 位小数对"秒"这个量级已经足够（<1ms 显示 0.000s）。
    return f"{float(secs or 0.0):.3f}s"


def solo_pass(ticket, budget=None, tactic=0):
    t0 = time.time()
    solved_timer = None
    try:
        # 第二发换手牌：重建连接池（丢掉半死连接）+ 换 UA + 跨线程错峰。
        # 这三样都是本地动作，不碰上游语义，失败也只会慢一点。
        if tactic and FL is not None:
            FL.reset_pool()
            FL.rotate_ua()
            FL.jitter()
        key, solved_timer = solve_chain(ticket, False, 3, budget=budget)
        if key:
            err = None
        else:
            reason = getattr(solved_timer, 'invalid_reason', None)
            fr = getattr(solved_timer, 'fail_reason', None)
            # fail_reason（network-error / rate / step-failed…）是分类依据。
            # 以前一律报 "solve failed"，自愈和队列拿不到 gate，等于白报。
            if reason:
                err = f"无效链接: {reason}"
            elif fr:
                err = str(fr)
            else:
                err = "solve failed"
    except Exception as e:
        key, err = None, f"solve exception: {type(e).__name__}"
    if solved_timer is not None and solved_timer.total() > 0:
        st = solved_timer.total()
    else:
        st = time.time() - t0
    if not key:
        # 失败顺便通知自愈线程：它会在下一轮决定要不要换出网方式 / 换层。
        # 这里只做一次入队（非阻塞），绝不等着它修 —— 用户的请求不能被拖住。
        _note_solve_failure(err, solved_timer)
    return key, err, st


def run_solves(ticket, enqueue=True, escalate=True, single=False):
    """战术重试：首发原样、次发换手牌，仍失败就丢给后台队列继续磨。

    返回 (key, err, 耗时, 尝试次数, 队列job)。
    这里的"两次"是**同一条链接的两次战术尝试**，和 solve_chain 内部的
    checkpoint 轮次不是一回事：第 2 次重置全部状态，换一套指纹再来。
    """
    t0 = time.time()
    budget = FL.SOLVE_BUDGET if FL is not None else 26.0
    first_budget = budget if single else max(8.0, budget * 0.55)

    key, err, st = solo_pass(ticket, budget=first_budget)
    # 成功时 solo_pass 的 err 是 None —— 后面要用 startswith 分类，先兜成空串。
    # 漏掉这一行会让"第一发就成功"的请求反过来抛 AttributeError（实测踩过）。
    err = err or ''
    tries = 1
    dead = err.startswith('无效链接') or err.startswith('solve exception')
    if not key and escalate and not dead:
        # 换手牌再来一发，预算只给剩下的部分 —— 总时长不会因为重试翻倍。
        time.sleep(0.2)
        left = max(4.0, budget - first_budget)
        key, err, st = solo_pass(ticket, budget=left, tactic=1)
        err = err or ''
        tries = 2
        dead = err.startswith('无效链接') or err.startswith('solve exception')

    job = None
    if key:
        _remember_probe_ticket(ticket)
    elif enqueue and _queue is not None and not dead:
        job = _queue.add(ticket, gate=_gate_from_solve(err, None) or 'solve-failed',
                         err=err, src='api')
    return key, err, time.time() - t0, tries, job


def _token_ok(request, token):
    if not API_TOKEN:
        return True
    got = (request.headers.get('x-token') or token or '').strip()
    return got == API_TOKEN


@app.get("/healthz")
async def healthz():
    """给 systemd / 负载均衡 / 监控用：不碰上游，纯本地状态，永远秒回。

    注意这里**不主动探测**上游 —— 外部监控每 5 秒打一次 healthz，如果它去问
    /public/start，等于把健康检查变成压测。上游门禁状态读的是 link_source
    上一次真实求解时记下的结论，要主动探测请用 /gate。
    """
    with lock:
        n = len(key_cache)
        running = len(inflight)
    gate = None
    gate_human = gate_hint = None
    heal = None
    try:
        import link_source as LS
        st = LS.load_state()
        gate = st.get("gate")
        gate_human = LS.GATE_HUMAN.get(gate or "", None)
        gate_hint = LS.ADVICE.get(gate or "", None)
    except Exception:
        pass
    if _healer is not None:
        try:
            # 注意：这里只读快照，绝不在这里切换出网方式。healthz 会被监控
            # 高频轮询，任何副作用都会被放大成事故。
            heal = _healer.snapshot()
        except Exception:
            heal = None
    return {
        "ok": True,
        "uptime": fmt_t(time.time() - START_TS),
        "cache_entries": n,
        "solving": running,
        "client_version": AUTH.client_version(),
        "egress": AUTH.proxy_info(),
        "cache_file": CACHE_FILE,
        "cache_file_source": CACHE_SOURCE,
        "cache_writable": _writable(CACHE_FILE),
        "auth_required": bool(API_TOKEN),
        "link_gate": gate,
        "link_gate_human": gate_human,
        "link_gate_hint": gate_hint,
        "heal": heal,
        "retryq": (_queue.snapshot(limit=0) if _queue is not None else None),
        "fast": (FL.snapshot() if FL is not None else None),
    }


@app.get("/gate")
async def gate_probe(request: Request, token: str = None, service: int = 6):
    """主动探测链接通道。会真的向上游发一次请求，别挂到监控高频轮询上。"""
    if not _token_ok(request, token):
        return {"ok": False, "error": "unauthorized"}
    try:
        import link_source as LS
        rep = LS.diagnose(probe=True, service=service)
        return {"ok": rep.get("gate") in (LS.G_OPEN, None), **rep}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


@app.api_route("/heal", methods=["GET", "POST"])
async def heal_now(request: Request, token: str = None):
    """手动触发一轮自愈：探测 -> 修复 -> 复测。

    会真的向上游发 1~4 次请求，别挂到监控轮询上。后台线程本来也会按
    DELTA_HEAL_INTERVAL 自己跑，这个端点只是"现在就想知道/现在就修"。
    """
    if not _token_ok(request, token):
        return {"ok": False, "error": "unauthorized"}
    h = _start_healer()
    if h is None:
        return {"ok": False, "error": "自愈模块不可用（import 失败）"}
    loop = asyncio.get_running_loop()
    try:
        # source 传 'http'：日志里能区分"定时探的"和"人手动点的"，
        # 排查时有用（手动那次不通，不代表后台也一直不通）。
        r = await asyncio.wait_for(
            loop.run_in_executor(executor, lambda: h.run_once('http')),
            timeout=90)
    except asyncio.TimeoutError:
        return {"ok": False, "error": "自愈超时（90s）", "stage": "timeout"}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    return {"ok": bool(r.get("ok")), **r}


@app.get("/job/{job_id}")
async def job_status(job_id: str, request: Request, token: str = None):
    """查一条失败票的后台重试进度。job id 就是 /delta 失败时返回的 job。"""
    if not _token_ok(request, token):
        return {"ok": False, "error": "unauthorized"}
    if _queue is None:
        return {"ok": False, "error": "重试队列不可用"}
    rec = _queue.get(job_id)
    if not rec:
        return {"ok": True, "state": "done-or-unknown", "job": job_id}
    now = time.time()
    return {"ok": True, "state": "queued", "job": job_id,
            "tries": rec.get('tries'), "gate": rec.get('gate'),
            "last_err": rec.get('last_err'),
            "next_in": round(max(0, float(rec.get('next_ts') or 0) - now), 1),
            "age": round(now - float(rec.get('created') or 0), 1)}


@app.get("/queue")
async def queue_status(request: Request, token: str = None):
    if not _token_ok(request, token):
        return {"ok": False, "error": "unauthorized"}
    # 注意：RetryQueue 定义了 __len__，空队列时 bool(队列对象) 是 False，
    # 这里必须用 "is not None" 判断对象是否存在，否则空队列会被当成"模块不可用"。
    return {"ok": True, "queue": (_queue.snapshot(limit=30)
                                  if _queue is not None else None)}


@app.get("/stats")
async def stats(request: Request, token: str = None):
    """求解延迟分布 + 成功率。用来验证"提速"是不是真的生效。"""
    if not _token_ok(request, token):
        return {"ok": False, "error": "unauthorized"}
    out = {"ok": True, "solve": STATS.snapshot(),
           "fast": FL.snapshot() if FL is not None else None}
    if _healer is not None:
        try:
            out['heal'] = _healer.snapshot()
        except Exception:
            pass
    return out


@app.get("/delta")
async def delta(url: str, request: Request, token: str = None, wait: float = 0):
    t0 = time.time()

    if not _token_ok(request, token):
        return {"key": None, "error": "unauthorized", "cached": False,
                "made_by": MADE_BY, "qq_group": QQ_GROUP,
                "times": fmt_t(time.time() - t0)}

    try:
        ticket = extract_ticket(url)
    except Exception:
        return {"key": None, "error": "invalid url", "cached": False,
                "made_by": MADE_BY, "qq_group": QQ_GROUP,
                "times": fmt_t(time.time() - t0)}
    # 长度不够的在这里就挡掉,别进求解流程。
    #
    # 求解一趟要 5 秒以上,而这种链接注定失败 —— 早点说清楚比让人等着强,顺带也省掉
    # 一次没意义的自动重试和一个白烧的验证码 token。
    if len(ticket) < MIN_TICKET_LEN:
        return {"key": None, "error": "invalid url (no ticket)", "cached": False,
                "made_by": MADE_BY, "qq_group": QQ_GROUP,
                "times": fmt_t(time.time() - t0)}

    h = hashlib.md5(ticket.encode()).hexdigest()
    cached, found, cached_solve_time = cache_get(ticket, h)
    if found:
        # times 统一表示"本次请求真实耗时"，这样缓存命中就是毫秒级、客户端一眼
        # 看出没走上游；当初解这条 key 花了多久放在 solve_time 里，信息不丢。
        return {"key": cached, "cached": True, "error": None,
                "made_by": MADE_BY, "qq_group": QQ_GROUP,
                "times": fmt_t(time.time() - t0),
                "solve_time": fmt_t(cached_solve_time),
                "tries": 0, "queued": False, "job": None, "retry_hit": False}

    loop = asyncio.get_running_loop()
    with lock:
        fut = inflight.get(h)
        if fut is None:
            fut = loop.create_future()
            inflight[h] = fut
            spawn = True
        else:
            spawn = False
    if spawn:
        loop.create_task(drive(h, fut, ticket))

    try:
        key, err, solve_time, tries, job = await asyncio.shield(fut)
    except Exception as e:
        key, err, solve_time, tries, job = \
            None, f"internal: {type(e).__name__}", time.time() - t0, 0, None

    # 可选长等：现场没解出来，但票已经进了后台队列，客户端愿意多等一会儿
    # 就直接在这里把结果接走。默认 wait=0 —— 用户抱怨"等待太久"，所以
    # 默认立刻返回，后台继续磨，下次同一条链接就是缓存命中。
    retry_hit = False
    if not key and wait > 0 and _queue is not None and job:
        deadline = time.time() + min(float(wait), 120.0)
        while time.time() < deadline:
            await asyncio.sleep(0.4)
            k2, found, _ = cache_get(ticket, h)
            if found:
                key, err, retry_hit = k2, None, True
                STATS.total['retry_hit'] += 1
                break
            if not _queue.has(h):
                break

    queued = bool(job and _queue is not None and _queue.has(h))
    out = {"key": key, "cached": False, "error": err,
           "made_by": MADE_BY, "qq_group": QQ_GROUP,
           "times": fmt_t(time.time() - t0),
           "solve_time": fmt_t(solve_time),
           "tries": tries, "queued": queued, "job": job,
           "retry_hit": retry_hit}
    if job:
        rec = _queue.get(job)
        if rec:
            out['retry_in'] = round(max(0, float(rec.get('next_ts') or 0)
                                        - time.time()), 1)
            out['retry_tries'] = rec.get('tries')
    return out


async def drive(h, fut, ticket):
    try:
        key, err, st, tries, job = await asyncio.get_running_loop().run_in_executor(
            executor, run_solves, ticket)
    except Exception as e:
        key, err, st, tries, job = None, f"solve exception: {type(e).__name__}", 0.0, 0, None
    if key:
        cache_put(ticket, key, st, h)
    STATS.add(st, bool(key))
    with lock:
        inflight.pop(h, None)
    if not fut.done():
        fut.set_result((key, err, st, tries, job))


# 退出前把缓存刷一次。
#
# 落盘是 2s 防抖的，而 systemctl restart 发完 SIGTERM 就等进程退出 ——
# 不刷的话"刚出 key 就走"的那几条会丢，重启后又要重解，看起来像缓存没生效。
# 主路径是上面 lifespan 的 yield 之后；atexit 兜住"没走 ASGI 生命周期"的
# 启动方式（比如被人 import 后自己跑）。
def _flush_on_exit(*_a):
    try:
        save_cache_now()
    except Exception:
        pass


try:
    import atexit
    atexit.register(_flush_on_exit)
except Exception:
    pass


def run(host="0.0.0.0", port=2233, workers=10, healer_interval=None):
    """真正的启动逻辑。__main__ 和 delta.py 都走这里，避免两份启动代码分叉。"""
    global executor
    if healer_interval is not None:
        if int(healer_interval) <= 0:
            os.environ['DELTA_HEAL'] = '0'      # 关掉后台线程，/heal 仍可用
        else:
            os.environ['DELTA_HEAL_INTERVAL'] = str(int(healer_interval))
    # 后台盯上游客户端版本：启动刷一次，之后每小时一次。
    AUTH.start_version_watcher()
    try:
        import resource
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        want = min(hard, 65535)
        if soft < want:
            resource.setrlimit(resource.RLIMIT_NOFILE, (want, hard))
            print(f"[server] fd limit {soft} -> {want}", flush=True)
    except Exception:
        pass

    executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="solve")
    kw = {}
    try:
        import uvloop  # noqa: F401
        kw['loop'] = 'uvloop'
    except Exception:
        pass
    try:
        import httptools  # noqa: F401
        kw['http'] = 'httptools'
    except Exception:
        pass

    pi = AUTH.proxy_info()
    print(f"[server] 出网方式: {pi['note']}", flush=True)
    print(f"[server] 缓存文件: {CACHE_FILE} ({CACHE_SOURCE}, "
          f"writable={_writable(CACHE_FILE)}, {len(key_cache)} 条)", flush=True)
    print(f"[server] 求解线程: {workers}  监听: {host}:{port}"
          f"  鉴权: {'开' if API_TOKEN else '关'}", flush=True)
    print(f"[server] 客户端版本: {AUTH.client_version()}", flush=True)
    if HEAL is not None:
        print(f"[server] 自愈: {'开' if HEAL.enabled() else '关（只观测）'}"
              f"  间隔 {os.environ.get('DELTA_HEAL_INTERVAL') or 180}s"
              f"  模式 {os.environ.get('DELTA_HEAL_MODE') or 'full'}"
              f"  （一个进程内含探测+修复+学习，无需另起脚本）", flush=True)
    if FL is not None:
        print(f"[server] 提速: 单条预算 {FL.SOLVE_BUDGET:.0f}s"
              f"  自适应步间隔 {FL.step_gap():.2f}s"
              f"  对冲请求={'开' if not (os.environ.get('DELTA_HEDGE') or '').lower() in ('0', 'false', 'no', 'off') else '关'}"
              f"  TLS 预热=开", flush=True)
    if RQ is not None:
        print(f"[server] 失败重试队列: {RQ.default_file()}"
              f"  （失败的票后台继续解，成功即写缓存）", flush=True)

    uvicorn.run(app, host=host, port=port,
                backlog=4096, access_log=False, **kw)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", "-p", type=int, default=2233)
    ap.add_argument("--host", default="0.0.0.0")
    # 这是求解线程数，不是 uvicorn 的进程数。别开太大：上游对同一 IP 的
    # 并发有限制，10~16 是实测比较稳的区间，再高只会把 "--workers" 变成
    # 排队等待 + 限流退避。
    ap.add_argument("--workers", "-w", type=int, default=10)
    ap.add_argument("--heal-interval", type=int, default=None,
                    help="自愈探测间隔秒数（默认 180，0 表示不启动后台线程）")
    args = ap.parse_args()
    run(args.host, args.port, args.workers, args.heal_interval)