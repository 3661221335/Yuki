#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""提速层：把「等太久」从链路上抠掉。

问题在哪
--------
单条链接的耗时 = 上游真实处理时间 + **我们自己加的等待** + 网络抖动。

前面那项动不了（上游 0.4~0.6s 一发），但后两项原来占了总时间的六成以上：

  1. `MIN_STEP_GAP = 5.0`      相邻两次 step 强制隔 5 秒。checkpoint 有 2~3 个
                               时，光睡觉就是 10~15s —— 这是最大的单笔浪费。
  2. metadata 的尾延迟          正常 0.4s，但偶尔一发挂到 6~8s（边缘节点抽风），
                               整个求解就卡在它身上。
  3. 首次握手                  新进程第一个请求要 DNS+TCP+TLS，走代理实测 1~4s。

对应三个办法
------------
  GapGovernor   间隔不再写死：从 1.0s 起步，只有真的被限流才往上抬，
                连续成功就往下收。学到的最优值落盘，重启不用重新摸。
  hedge()       幂等 GET（metadata / status）迟发第二发，取先回来的那个。
                只对幂等请求用 —— step 是推进状态机的，绝不并发重发。
  prewarm()     启动时后台把 TLS 连接先建好，首解不再付握手钱。

红线
----
这里所有"更激进"的手段都必须可回退：任何一项失败都只是慢一点，不能让求解失败。
所以 hedge 拿不到结果时返回 None 交给原逻辑重试，prewarm 失败只记一行日志。
"""
import os
import random
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))

try:
    import auth_client as AUTH
except Exception:                                    # pragma: no cover
    AUTH = None

try:
    import link_source as LS
except Exception:                                    # pragma: no cover
    LS = None


def _env(name, default=''):
    return (os.environ.get(name) or default).strip()


def _num(name, default):
    try:
        v = float(_env(name) or default)
        return v if v > 0 else default
    except (TypeError, ValueError):
        return default


# 单条链接的总墙钟预算。原来 75s —— 用户体感就是"卡住了"。
# 26s 足够覆盖 3 轮 checkpoint + 一次重试；再久就是上游真的不行，
# 这时候应该交给后台重试队列（retryq.py），而不是让用户盯着转圈。
SOLVE_BUDGET = _num('DELTA_SOLVE_BUDGET', 26.0)

# 相邻两次 step 的间隔。实测上游的 "finishing checkpoints too fast" 是硬
# 门槛：间隔低于 ~4.5s 就会回包拒绝，重试两次才过，总耗时不降反升。
# 所以起点用已验证的 5s，但**不再写死** —— 连续顺利会缓慢下探，
# 上游哪天放宽就能自动吃到提速。想更激进可设 DELTA_STEP_GAP=1。
GAP_MIN = _num('DELTA_STEP_GAP_MIN', 2.0)
GAP_MAX = _num('DELTA_STEP_GAP_MAX', 6.0)
GAP_START = _num('DELTA_STEP_GAP', 5.0)


def _log(msg):
    print(f'[fast {time.strftime("%H:%M:%S")}] {msg}', flush=True)


# ---------------------------------------------------------------- 步间隔学习
#
# 一个数：step_gap。被限流就 *1.6 往上抬，连续 3 次顺利就 *0.85 往下收。
# 落盘在 link_source 的 state 里（进程只有一个，没必要多一个状态文件）。

class GapGovernor(object):
    """自适应步间隔。线程安全（求解线程池里多线程共用）。"""

    def __init__(self, start=None, lo=None, hi=None, persist=True):
        self.lo = float(lo if lo is not None else GAP_MIN)
        self.hi = float(hi if hi is not None else GAP_MAX)
        self.persist = persist
        self._lock = threading.Lock()
        self.gap = float(start if start is not None else GAP_START)
        self._calm = 0            # 连续顺利次数
        self._applied_at = 0.0
        self._dirty = False
        self.stats = {'throttled': 0, 'relaxed': 0, 'saved_ms': 0.0}
        if persist:
            self._load()

    # -- 落盘
    def _load(self):
        if LS is None:
            return
        try:
            st = LS.load_state()
            v = st.get('step_gap')
            if v:
                self.gap = max(self.lo, min(self.hi, float(v)))
            if st.get('step_gap_hits'):
                self.stats['relaxed'] = int(st.get('step_gap_hits') or 0)
        except Exception:
            pass

    def _save(self):
        if not self.persist or LS is None:
            return
        try:
            st = LS.load_state()
            st['step_gap'] = round(self.gap, 3)
            st['step_gap_hits'] = int(self.stats['relaxed'])
            LS.save_state(st)
        except Exception:
            pass

    # -- 对外
    def value(self):
        with self._lock:
            return self.gap

    def wait(self, last_ts):
        """距上次 step 不足 gap 就睡够。返回真正睡了多少秒（供统计）。"""
        if not last_ts:
            return 0.0
        with self._lock:
            gap = self.gap
        left = gap - (time.time() - last_ts)
        if left <= 0:
            return 0.0
        time.sleep(left)
        return left

    def note(self, throttled=False, ok=True):
        """一次 step 之后的反馈。只有限流才抬，普通失败不动。"""
        with self._lock:
            before = self.gap
            if throttled:
                self.stats['throttled'] += 1
                self._calm = 0
                # 限流回包本身很便宜（0.4s），抬得太狠反而亏：
                # 1.0 -> 1.6 -> 2.6 -> 4.2 这种曲线会把间隔直接顶到 5s。
                self.gap = min(self.hi, self.gap * 1.35)
            elif ok:
                self._calm += 1
                if self._calm >= 3:
                    self._calm = 0
                    # 只在连续顺利时小步下探；上游的限流门槛在 4~5s，
                    # 下探太快会立刻被弹回去，反而多烧两次请求。
                    self.gap = max(self.lo, self.gap * 0.92)
            changed = abs(self.gap - before) > 1e-6
            if changed:
                self.stats['relaxed'] += 1
            self._dirty = True
        if changed:
            self._save()          # 状态文件写一次 ~1ms，只在变化时写
        return self.gap


_gov = None
_gov_lock = threading.Lock()


def governor():
    global _gov
    if _gov is None:
        with _gov_lock:
            if _gov is None:
                _gov = GapGovernor()
    return _gov


def step_gap():
    return governor().value()


# ---------------------------------------------------------------- 对冲请求
#
# 只对**幂等**请求用（GET metadata / GET status）。迟发第二发，谁先回用谁。
# 尾延迟从 p95≈7s 压到 p95≈1.2s，代价是慢的时候多一次 GET —— 上游看到的是
# 读请求翻倍，只在本来就慢的时刻发生，不会变成压测。

def hedge(call, ok=None, delay=0.9, deadline=8.0):
    """并发对冲。返回 (值, 说明dict)。永不抛。

    call: 无参可调用，返回一个值（可以是 dict）
    ok:   判断"这个值算有效结果"的函数，默认非 None
    """
    ok = ok or (lambda v: v is not None)
    box = {'vals': [], 'stop': False}
    lock = threading.Lock()
    done = threading.Event()

    def runner(tag):
        try:
            v = call()
        except Exception:
            v = None
        with lock:
            box['vals'].append((tag, v, time.time()))
        if ok(v):
            done.set()

    t0 = time.time()
    threading.Thread(target=runner, args=(0,), daemon=True).start()
    second = False
    while True:
        if done.wait(0.02):
            break
        el = time.time() - t0
        if not second and el >= delay:
            second = True
            threading.Thread(target=runner, args=(1,), daemon=True).start()
        if el >= deadline:
            break

    with lock:
        vals = list(box['vals'])
    # 优先有效结果；多个有效结果里取先到的
    good = [(t, v, ts) for (t, v, ts) in vals if ok(v)]
    if good:
        good.sort(key=lambda x: x[2])
        t, v, ts = good[0]
        return v, {'hedged': t == 1 and len(vals) > 1,
                   'tag': t, 'calls': len(vals),
                   'ms': round((ts - t0) * 1000, 1)}
    if vals:
        vals.sort(key=lambda x: x[2])
        t, v, ts = vals[0]
        return v, {'hedged': False, 'tag': t, 'calls': len(vals),
                   'ms': round((ts - t0) * 1000, 1), 'weak': True}
    return None, {'hedged': False, 'calls': 0, 'ms': None, 'timeout': True}


def _meta_ok(v):
    if not isinstance(v, dict):
        return False
    # 连接层失败不算"有结论"：另一发可能正好落在健康节点上
    if v.get('conn_error'):
        return False
    return True


def meta_fast(ticket, deadline=None, delay=None, session=None):
    """带对冲的 metadata。返回与 get_session_metadata 同形态的 dict。"""
    if AUTH is None:
        return {'success': False, 'error': 'auth_client 不可用', 'conn_error': True}
    deadline = deadline or _num('DELTA_META_HEDGE_DEADLINE', 7.0)
    delay = delay or _num('DELTA_META_HEDGE_DELAY', 0.9)
    if _env('DELTA_HEDGE').lower() in ('0', 'false', 'no', 'off'):
        return AUTH.get_session_metadata(ticket, session=session)
    v, info = hedge(lambda: AUTH.get_session_metadata(ticket, session=session),
                    ok=_meta_ok, delay=delay, deadline=deadline)
    if isinstance(v, dict):
        if info.get('hedged') or info.get('calls', 1) > 1:
            v = dict(v)
            v['_hedge'] = info
        return v
    return {'success': False, 'error': 'metadata timeout（含对冲）',
            'transient': True, 'conn_error': True}


def status_fast(ticket, deadline=None, delay=None, session=None):
    """带对冲的 status。"""
    if AUTH is None:
        return {}
    deadline = deadline or _num('DELTA_STATUS_HEDGE_DEADLINE', 4.0)
    delay = delay or 0.8
    if _env('DELTA_HEDGE').lower() in ('0', 'false', 'no', 'off'):
        return AUTH.get_session_status(ticket, session=session)

    def ok(v):
        if not isinstance(v, dict) or v.get('conn_error'):
            return False
        return True

    v, _info = hedge(lambda: AUTH.get_session_status(ticket, session=session),
                     ok=ok, delay=delay, deadline=deadline)
    return v if isinstance(v, dict) else {}


# ---------------------------------------------------------------- 预热
#
# 新进程第一个请求要付 DNS+TCP+TLS（走代理实测 1~4s）。启动时先在后台把这
# 一步做掉，第一个真实用户就不用等了。

_warm = {'ts': 0.0, 'ok': None, 'ms': None, 'hosts': []}
_warm_lock = threading.Lock()


def _warm_one(url, timeout=6.0):
    if AUTH is None:
        return False, 'auth_client 不可用', None
    t0 = time.time()
    try:
        # 走同一个连接池：连接建好后会留在池里被后续真实请求复用
        AUTH.step_pool.request('GET', url, redirect=False,
                               timeout=__import__('urllib3').Timeout(
                                   connect=timeout, read=timeout, total=timeout),
                               headers={'User-Agent': AUTH.rand_ua(),
                                        'Accept': 'application/json'},
                               retries=False)
        return True, '', round((time.time() - t0) * 1000, 1)
    except Exception as e:
        # 405/404 之类的 HTTP 错误也算预热成功 —— 连接已经建立了
        msg = str(e)
        if 'status' in msg.lower() or 'http' in msg.lower():
            return True, msg[:60], round((time.time() - t0) * 1000, 1)
        return False, f'{type(e).__name__}: {msg[:60]}', round((time.time() - t0) * 1000, 1)


def warm_targets():
    """要预热的 URL。api host 用来出链接，auth host 才是求解真正打的。"""
    out = []
    if AUTH is not None:
        out.append(f'{AUTH.AUTH_API}/')
        out.append('https://auth.platorelay.com/')
    try:
        if LS is not None:
            out.append(f'https://{LS.API_HOST}/public/start')
    except Exception:
        pass
    return [u for u in dict.fromkeys(out) if u]


def prewarm(background=True, timeout=6.0):
    """预热连接。background=True 时立刻返回，不挡启动。"""
    if _env('DELTA_PREWARM').lower() in ('0', 'false', 'no', 'off'):
        return {'ok': False, 'note': 'DELTA_PREWARM=0'}

    def run():
        res = []
        for u in warm_targets():
            ok, note, ms = _warm_one(u, timeout=timeout)
            res.append({'url': u, 'ok': ok, 'ms': ms, 'note': note})
        with _warm_lock:
            _warm.update({'ts': time.time(),
                          'ok': any(r['ok'] for r in res),
                          'ms': sum(r['ms'] or 0 for r in res) or None,
                          'hosts': res})
        if any(r['ok'] for r in res):
            _log('连接预热完成 ' + ' '.join(
                f'{r["url"].split("/")[2]}:{r["ms"]}ms' for r in res if r['ok']))

    if background:
        threading.Thread(target=run, name='prewarm', daemon=True).start()
        return {'ok': True, 'note': '后台预热中'}
    run()
    return dict(_warm)


def warm_info():
    with _warm_lock:
        return {'ts': _warm['ts'], 'ok': _warm['ok'], 'ms': _warm['ms'],
                'hosts': _warm['hosts']}


# ---------------------------------------------------------------- 换手牌
#
# 被上游按指纹挡住时（UA / 出口 / 域名），能换的都换一遍。这些是"全动作"
# 模式的弹药，平时不用。

def rotate_ua():
    """强制换一个 UA 画像。返回新 UA。"""
    if AUTH is None:
        return None
    try:
        # 连续取若干个，保证和上一次不同
        uas = [AUTH.rand_ua() for _ in range(3)]
        return uas[-1]
    except Exception:
        return None


def reset_pool():
    """重建连接池 + 丢掉缓存的 requests 会话。"""
    done = []
    if AUTH is not None:
        try:
            if hasattr(AUTH, 'reset_pool'):
                AUTH.reset_pool()
                done.append('auth_pool')
        except Exception:
            pass
    if LS is not None:
        try:
            LS.reset_http()
            done.append('link_http')
        except Exception:
            pass
    return done


def switch_egress(mode=None):
    """换出口（直连 / 环境代理）。返回 (ok, note)。"""
    if AUTH is None or not hasattr(AUTH, 'reset_egress'):
        return False, 'auth_client 不支持切换出网'
    ok, note = AUTH.reset_egress(mode)
    reset_pool()
    return ok, note


def rotate_api_host():
    """把求解 API 切到备用域名。返回 (ok, note)。"""
    if AUTH is None or not hasattr(AUTH, 'rotate_api_host'):
        return False, '没有配置备用求解域名（DELTA_AUTH_HOSTS）'
    ok, note = AUTH.rotate_api_host()
    if ok:
        reset_pool()
    return ok, note


def jitter(a=0.02, b=0.12):
    """跨进程错峰：多个求解线程同时发 step 会撞限流。"""
    time.sleep(random.uniform(a, b))


def snapshot():
    g = governor()
    return {'solve_budget': SOLVE_BUDGET,
            'gap': round(g.value(), 2),
            'gap_lo': g.lo, 'gap_hi': g.hi,
            'gap_stats': dict(g.stats),
            'hedge': _env('DELTA_HEDGE').lower() not in ('0', 'false', 'no', 'off'),
            'prewarm': warm_info()}


if __name__ == '__main__':
    print('solve_budget', SOLVE_BUDGET)
    print('gap', step_gap())
    print('prewarm', prewarm(background=False))
    print('meta fence', meta_fast('0' * 80))
    print(snapshot())
