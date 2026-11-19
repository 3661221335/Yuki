#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""自愈引擎：把「探测 -> 失败 -> 学习 -> 修复 -> 复测」跑成闭环。

为什么要有这个文件
------------------
原来这套东西是分开的两块：

    python server.py                 # 服务
    python probe_gate.py --watch 300 # 监控

服务器上只让跑一个脚本，于是监控这一块直接没了 —— 上游哪天加了门禁，
只能等用户来报"解不了了"才知道。这个文件把那块收进服务进程内部，
变成一个后台线程，一个进程就是全部。

它比原来的 --watch 多做三件事：

1. **会修，不只报**。探测到失败后按失败类型执行修复动作（学字段名、换
   出网方式、刷 cookie、挖 sitekey……），修完立刻复测，不是只打一行日志。
2. **会记**。每次「失败类型 -> 动作 -> 结果」都写进 state 的 lessons 表，
   下次遇到同一个失败先试上次有效的那个。上游改了、又改回来，系统自己认得。
3. **会被求解链路叫醒**。用户请求失败时直接通知它，不用等下一个轮询周期；
   同一类型重复上报会自动去重，不会变成对上游的压测。

线程模型
--------
单个 daemon 线程 + 两个 Event。必须是 daemon：非 daemon 的线程会让进程在
收到 SIGTERM 后卡住不退（这个坑之前在求解线程上踩过一次，直接表现为
"systemctl restart 之后新进程起不来，旧进程赖着不走"）。

暂停/关闭：设 DELTA_HEAL_AUTO=0 只观测不修；设 DELTA_HEAL=0 整个线程不启动。
"""
import os
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import link_source as LS

try:
    import auth_client as AUTH
except Exception:                                    # pragma: no cover
    AUTH = None

try:
    import fastlane as FL
except Exception:                                    # pragma: no cover
    FL = None

# stdout 在服务器上可能是 ASCII（LANG 没设），中文日志会 UnicodeEncodeError。
# auth_client 导入时已经 reconfigure 过一次，这里兜一层：selfheal 也可能被
# 单独 import（selftest / 手动排查），那时不能假设别人已经修好了。
try:
    for _s in (sys.stdout, sys.stderr):
        if hasattr(_s, 'reconfigure'):
            _s.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass


def _env(name, default=''):
    return (os.environ.get(name) or default).strip()


def _flag(name, default=True):
    v = _env(name)
    if not v:
        return default
    return v.lower() not in ('0', 'false', 'no', 'off')


def _log(msg):
    print(f'[heal {time.strftime("%H:%M:%S")}] {msg}', flush=True)


def _mask(v, keep=4):
    """凭据只露头尾，日志/健康检查里不能出现完整值。"""
    v = str(v or '')
    if len(v) <= keep * 2:
        return '*' * len(v)
    return v[:keep] + '...' + v[-keep:]


class Healer(object):
    """探测 + 修复 + 学习。一个实例 = 一个后台线程。"""

    def __init__(self, interval=180, service=6, min_repair_gap=None,
                 max_repairs_per_hour=None, auto=None, verbose=True, mode=None):
        self.interval = max(10, int(interval))
        self.service = int(service)
        # 模式：full = "什么都可以动"（默认），standard = 原来的保守参数。
        # 显式传进来的值永远优先 —— 单测里传 min_repair_gap=600 就是要验冷却。
        self.mode = (mode or _env('DELTA_HEAL_MODE') or 'full').lower()
        self.full = self.mode in ('full', 'all', 'aggressive', 'max')
        if min_repair_gap is None:
            min_repair_gap = _env('DELTA_HEAL_COOLDOWN') or (8 if self.full else 60)
        if max_repairs_per_hour is None:
            max_repairs_per_hour = _env('DELTA_HEAL_HOURLY') or (240 if self.full else 30)
        self.min_repair_gap = float(min_repair_gap)
        self.max_repairs_per_hour = int(max_repairs_per_hour)
        self.auto = _flag('DELTA_HEAL_AUTO', True) if auto is None else bool(auto)
        self.verbose = verbose

        self._thread = None
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._lock = threading.Lock()
        # 后台线程和 /heal 手动触发可能撞在一起 → 同一时刻只允许跑一轮，
        # 否则两轮各修一遍，等于把探测翻倍。
        self._run_lock = threading.Lock()
        self._pending = {}          # gate -> {gate, detail, ts}，天然去重
        self._repair_ts = {}        # gate -> 上次修复时间
        self._recent = []           # 最近一小时的修复时间戳
        self._rate_streak = 0

        self.stats = {'probe': 0, 'repair': 0, 'fixed': 0, 'failed': 0,
                      'skipped': 0, 'woken': 0, 'solve_probe': 0}
        self.last = None            # 最近一次 run_once 的结论
        self.last_probe = None

    # ------------------------------------------------------------ 生命周期

    def start(self):
        """幂等启动。重复调用不会起第二个线程。"""
        if self._thread is not None and self._thread.is_alive():
            return self
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name='selfheal',
                                        daemon=True)
        self._thread.start()
        self._log_start()
        return self

    def _log_start(self):
        if not self.verbose:
            return
        _log(f'已启动：每 {self.interval}s 探一次，模式={self.mode}，'
             f'自动修复={"开" if self.auto else "关（只观测）"}，'
             f'冷却 {self.min_repair_gap:.0f}s，上限 {self.max_repairs_per_hour} 次/小时')
        best = LS.load_state().get('egress_best')
        if best:
            _log(f'上次试出的最优出网方式: {best}')

    def stop(self, timeout=5.0):
        self._stop.set()
        self._wake.set()
        t = self._thread
        if t is not None and t.is_alive():
            t.join(timeout)

    def running(self):
        return self._thread is not None and self._thread.is_alive()

    def _loop(self):
        # 启动先立刻探一次：重启后 state 里的 gate 可能是昨天的结论，
        # 不能让 /healthz 一直显示过时信息。
        self._safe_run()
        while not self._stop.is_set():
            self._wake.wait(timeout=self.interval)
            if self._stop.is_set():
                break
            self._wake.clear()
            self._safe_run()

    def _safe_run(self):
        try:
            self.run_once()
        except Exception as e:                       # 自愈自己绝不能把服务搞挂
            self.stats['failed'] += 1
            _log(f'本轮异常（已忽略）: {type(e).__name__}: {e}')

    # ------------------------------------------------------------ 外部喂事件

    def note_failure(self, gate, detail='', source='solve'):
        """求解链路失败时调用。唤醒线程并立即处理一次。

        同一个 gate 只保留最后一条：连续 20 个请求都因同一个原因失败时，
        没有理由修 20 次 —— 上游面对的是同一堵墙。
        """
        if not gate or gate == LS.G_OPEN:
            return
        with self._lock:
            self._pending[gate] = {'gate': gate, 'detail': str(detail)[:200],
                                   'source': source, 'ts': time.time()}
            self.stats['woken'] += 1
        self._wake.set()

    def _drain(self):
        with self._lock:
            evs = list(self._pending.values())
            self._pending.clear()
        if not evs:
            return None
        # 取最严重的一条：cf/auth/captcha 比 offline 更值得先处理
        evs.sort(key=lambda e: LS.gate_severity(e.get('gate')), reverse=True)
        return evs[0]

    # ------------------------------------------------------------ 探测

    def probe(self):
        """真的向上游要一条链接。返回结构化结果，永不抛。"""
        t0 = time.time()
        try:
            url = LS.probe_start(self.service)
            ms = (time.time() - t0) * 1000
            LS._record('heal-probe', True, gate=LS.G_OPEN, ms=ms)
            self._rate_streak = 0
            res = {'ok': True, 'gate': LS.G_OPEN, 'ms': round(ms, 1),
                   'url_head': url[:56] + '...'}
        except LS.GateError as e:
            ms = (time.time() - t0) * 1000
            LS._record('heal-probe', False, gate=e.gate, detail=str(e), ms=ms)
            res = {'ok': False, 'gate': e.gate, 'ms': round(ms, 1),
                   'detail': str(e)[:200], 'status': e.status}
        except Exception as e:
            ms = (time.time() - t0) * 1000
            gate = LS.classify_exception(e)
            LS._record('heal-probe', False, gate=gate, detail=str(e), ms=ms)
            res = {'ok': False, 'gate': gate, 'ms': round(ms, 1),
                   'detail': f'{type(e).__name__}: {e}'[:200]}
        res['at'] = time.time()
        res['gate_human'] = LS.GATE_HUMAN.get(res['gate'], res['gate'])
        self.stats['probe'] += 1
        self.last_probe = res
        return res

    def probe_solve(self):
        """用最近一条真实票据探「求解链路」（metadata）。

        /public/start 只回答"能不能拿到链接"，回答不了"拿到链接之后能不能解出
        key"。以前上游单独动 metadata 接口时，自愈一路显示 open，用户却全线
        解不出来 —— 这个探针就是补那块盲区。没记过 probe_ticket 时自动跳过。
        """
        try:
            ticket = (LS.load_state().get('probe_ticket') or '')
        except Exception:
            return None
        getter = getattr(AUTH, 'get_session_metadata', None)
        if not ticket or not callable(getter):
            return None

        t0 = time.time()
        try:
            r = getter(ticket)
        except Exception as e:
            r = {'success': False, 'error': str(e), 'conn_error': True}
        ms = round((time.time() - t0) * 1000, 1)
        if not isinstance(r, dict):
            r = {'success': False, 'error': 'non-dict response', 'conn_error': True}

        ok = True
        gate = LS.G_OPEN
        detail = ''
        if r.get('conn_error'):
            ok, gate = False, LS.G_OFFLINE
            detail = str(r.get('error') or 'connection error')[:120]
        elif r.get('success') is False:
            msg = str(r.get('message') or r.get('error') or '')
            low = msg.lower()
            detail = msg[:120]
            if any(k in low for k in ('captcha', 'turnstile', 'hcaptcha')):
                gate = LS.G_CAPTCHA
            elif any(k in low for k in ('too many', 'rate limit', '429')):
                gate = LS.G_RATE
            elif any(k in low for k in ('unauthorized', 'forbidden', 'api key')):
                gate = LS.G_AUTH
            else:
                markers = getattr(AUTH, 'INVALID_MARKERS', ())
                if any(m in low for m in markers):
                    # 探针票本身过期，不是系统故障，不能拿它触发修复
                    return {'ok': True, 'gate': LS.G_OPEN, 'ms': ms,
                            'detail': f'探针票已失效（非系统故障）: {detail[:60]}',
                            'solve_probe': True}
                gate = LS.G_UNKNOWN
            ok = False
        res = {'ok': ok, 'gate': gate, 'ms': ms, 'detail': detail,
               'solve_probe': True}
        res['gate_human'] = LS.GATE_HUMAN.get(gate, gate)
        return res

    # ------------------------------------------------------------ 修复动作
    #
    # 每个动作返回 (ok, note)。ok=True 表示"上游又通了"，不只是"命令执行了"。
    # 这一点很关键：如果动作本身成功但上游仍拒绝，就不算修好，要继续试下一个。

    def _act_replay(self):
        url = LS.probe_start(self.service)
        return True, f'原样重放成功 {url[:40]}'

    def _act_guess_fields(self):
        """把候选字段名全填空串再发一次。

        上游只是"要求字段存在"时，空串能过。这是学不到字段名时的第二手：
        报错里没写 required property 也不至于卡死。

        成功的结论记到 guess_fields，而不是 learned_fields —— 后者代表
        "上游明说了字段名"，会用 null 发送；猜出来的组合必须继续用空串发，
        两者混在一起会导致下一次请求又被拒。
        """
        extra = {f: '' for f in LS.FIELD_CANDIDATES}
        url = LS.probe_start(self.service, extra=extra)
        st = LS.load_state()
        st['guess_fields'] = True
        LS.save_state(st)
        return True, (f'按候选字段（空串）重放成功，已记住这个组合 '
                      f'({len(LS.FIELD_CANDIDATES)} 个) {url[:32]}')

    def _act_refresh_cookie(self):
        LS.reset_http()
        url = LS.CookieProvider().fetch(self.service, LS.hashlib_hwid())
        LS._record('cookie', True, gate=LS.G_OPEN)
        return True, f'cookie 层取到链接 {url[:40]}'

    def _act_mirror(self):
        url = LS.MirrorProvider().fetch(self.service, LS.hashlib_hwid())
        LS._record('mirror', True, gate=LS.G_OPEN)
        return True, f'mirror 层取到链接 {url[:40]}'

    def _act_hunt_sitekey(self):
        """挖 sitekey。

        注意这里返回 False 是故意的：挖到 sitekey 只是"让 captcha 层变得可用"，
        并不等于上游放行了。把它算成"修好"会让日志骗人 —— 明明还拿不到链接，
        健康检查却显示 open。真正的修好只有一条标准：能拿到链接。
        """
        sk = LS.hunt_sitekey(verbose=self.verbose)
        if sk:
            return False, f'已拿到 sitekey={sk}，但还需 captcha 层真正出链接'
        return False, '页面/bundle/响应里都没有 sitekey'

    def _act_captcha_solve(self):
        if not (LS.CAPSOLVER_KEY or LS.TWOCAPTCHA_KEY):
            return False, '没有打码 key（CAPSOLVER_KEY / TWOCAPTCHA_KEY）'
        if not LS.known_sitekey():
            return False, '有打码 key 但没有 sitekey，先跑 hunt_sitekey'
        url = LS.CaptchaProvider().fetch(self.service, LS.hashlib_hwid())
        return True, f'打码后取到链接 {url[:40]}'

    def _act_check_credentials(self):
        have = [k for k in ('DELTA_API_KEY', 'DELTA_API_TOKEN', 'DELTA_API_COOKIE')
                if _env(k)]
        if not have:
            return False, ('上游开始要凭据，但没配 DELTA_API_KEY / '
                           'DELTA_API_TOKEN / DELTA_API_COOKIE')
        url = LS.probe_start(self.service)         # _auth_headers 会自动带上
        return True, f'带 {",".join(have)} 重放成功'

    def _act_switch_egress(self):
        """直连 / 跟随环境代理 各试一发，把通的记住。

        服务器上最常见的两类事故：机器配了全局代理但直连更快；或者反过来
        直连被墙只有代理能出。两种情况从"连不上"这个现象上完全一样，
        只能实测区分。切换会重建连接池（见 auth_client.reset_egress）。
        """
        if AUTH is None:
            return False, 'auth_client 不可用，无法切换出网'
        before = AUTH.proxy_info().get('mode') or 'direct'
        tried = []
        for mode in ('direct', 'env'):
            ok, note = AUTH.reset_egress(mode)
            if not ok:
                tried.append(f'{mode}:{note}')
                continue
            LS.reset_http()
            try:
                url = LS.probe_start(self.service)
            except LS.GateError as e:
                tried.append(f'{mode}:{e.gate}')
                continue
            except Exception as e:
                tried.append(f'{mode}:{type(e).__name__}')
                continue
            st = LS.load_state()
            st['egress_best'] = mode
            LS.save_state(st)
            return True, f'{mode} 通（{note}）{url[:32]}'
        # 两种都不通：恢复到原来的方式，别让进程留在"试错中间态"
        AUTH.reset_egress('direct' if before == 'direct' else 'env')
        LS.reset_http()
        return False, f'两种出网都不通: {" | ".join(tried)}'

    def _act_backoff(self):
        """限流不是故障，是"别敲了"。退避不算修好，但能防止把限流打成封禁。"""
        self._rate_streak += 1
        delay = min(30 * (2 ** (self._rate_streak - 1)), 900)
        self.interval = max(self.interval, delay)
        return False, f'被限流，间隔自动拉长到 {delay}s（连续第 {self._rate_streak} 次）'

    def _act_manual(self):
        if not _env('DELTA_LINK_INBOX') and not _env('DELTA_LINK_FEED'):
            return False, '没有配置外部链接来源（DELTA_LINK_INBOX / DELTA_LINK_FEED）'
        url = LS.ManualProvider().fetch(self.service, LS.hashlib_hwid())
        return True, f'外部供应取到链接 {url[:40]}'

    # ---- 全动作模式新增的牌。都是本地动作 + 一次真实探测，失败不抛。

    def _act_reset_pool(self):
        """重建连接池：专治"每条请求都卡满读超时才失败"的半死连接。"""
        done = FL.reset_pool() if FL is not None else []
        return False, f'连接池已重建（{", ".join(done) or "无可用接口"}）'

    def _act_rotate_ua(self):
        """换 UA 画像后重放。上游按指纹挡人时，这是代价最低的一发。"""
        if FL is None:
            return False, 'fastlane 不可用，无法换 UA'
        ua = FL.rotate_ua()
        if not ua:
            return False, 'UA 池不可用'
        url = LS.probe_start(self.service)
        return True, f'换 UA 后取到链接 {url[:36]} ({ua[:28]}...)'

    def _act_alt_host(self):
        """切到备用求解域名。没配 DELTA_AUTH_HOSTS 时直接返回 False。"""
        if FL is None:
            return False, 'fastlane 不可用，无法换域名'
        ok, note = FL.rotate_api_host()
        if not ok:
            return False, note
        url = LS.probe_start(self.service)
        return True, f'切到备用域名 {note} 后取到链接 {url[:32]}'

    def _act_short_backoff(self):
        """限流的短退避：等 3s 立刻复测，不是把轮询间隔拉长就算了。"""
        time.sleep(3.0)
        url = LS.probe_start(self.service)
        return True, f'短退避后取到链接 {url[:36]}'

    def _act_prewarm(self):
        """重连预热：修完出口/连接池后把 TLS 抢先建好，省掉下一发的握手。"""
        if FL is None:
            return False, 'fastlane 不可用'
        r = FL.prewarm(background=False, timeout=6)
        return False, f'预热完成: {str(r.get("note") or r.get("ok"))[:80]}'

    # ------------------------------------------------------------ 决策

    def _plan(self, gate):
        """该失败类型对应的动作序列。上次有效的动作提到最前。"""
        table = {
            # 缺字段：先猜空串（能过非空校验前的存在性检查），再原样重放
            LS.G_SCHEMA: [('guess_fields', self._act_guess_fields),
                          ('replay', self._act_replay)],
            # Cloudflare：刷 cookie -> 换出口 -> 换域名
            LS.G_CF: [('refresh_cookie', self._act_refresh_cookie),
                      ('switch_egress', self._act_switch_egress),
                      ('mirror', self._act_mirror)],
            # 要凭据：先看有没有配，再退到 cookie / 外部供应
            LS.G_AUTH: [('check_credentials', self._act_check_credentials),
                        ('cookie', self._act_refresh_cookie),
                        ('manual', self._act_manual)],
            # 验证码：先真的打码出链接（唯一算修好的），再退而求其次挖 sitekey
            LS.G_CAPTCHA: [('captcha_solve', self._act_captcha_solve),
                           ('hunt_sitekey', self._act_hunt_sitekey)],
            # 网络不通：换出口是最有效的，别的都是猜
            LS.G_OFFLINE: [('switch_egress', self._act_switch_egress),
                           ('mirror', self._act_mirror)],
            LS.G_RATE: [('backoff', self._act_backoff),
                        ('short_backoff', self._act_short_backoff)],
            LS.G_UNKNOWN: [('replay', self._act_replay),
                           ('switch_egress', self._act_switch_egress)],
        }
        plan = list(table.get(gate) or [])

        # 全动作模式：每种失败都把"还能动的牌"打完，不再因为一个 gate
        # 只映射了一两个动作就早早放弃。reset_pool / prewarm 结果恒为 False
        # （它们只是准备条件，不等于上游放行），但会给后面的动作铺路。
        if self.full:
            tail = [
                ('reset_pool', self._act_reset_pool),
                ('rotate_ua', self._act_rotate_ua),
                ('alt_host', self._act_alt_host),
            ]
            # 启动预热已经在服务里做了；仅在显式要求时让自愈也重播一次，
            # 否则每轮失败都会多打几个无害但没必要的连接请求。
            if _flag('DELTA_HEAL_PREWARM', False):
                tail.append(('prewarm', self._act_prewarm))
            tail.append(('replay', self._act_replay))
            have = {n for n, _ in plan}
            plan += [(n, f) for n, f in tail if n not in have]

        # 上次有效的动作提到最前（但 reset_pool/prewarm 这种"准备动作"
        # 不能因为上次记过就被提到前面 —— 它们永远不会让服务变好）
        best = LS.best_action(gate)
        if best and best not in ('reset_pool', 'prewarm'):
            plan.sort(key=lambda x: 0 if x[0] == best else 1)
        return plan

    def _may_repair(self, gate):
        now = time.time()
        gap = now - float(self._repair_ts.get(gate) or 0)
        if gap < self.min_repair_gap:
            return False, f'同类失败 {gap:.0f}s 前刚修过，冷却中'
        self._recent = [t for t in self._recent if now - t < 3600]
        if len(self._recent) >= self.max_repairs_per_hour:
            return False, f'一小时内已修 {len(self._recent)} 次，到上限'
        return True, ''

    def repair(self, gate, detail=''):
        """按 gate 执行修复序列。返回结论 dict（永不抛）。"""
        plan = self._plan(gate)
        if not plan:
            return {'ok': False, 'gate': gate, 'action': None, 'tried': [],
                    'note': f'{gate} 没有可自动执行的动作，需要人工介入'}

        ok, why = self._may_repair(gate)
        if not ok:
            self.stats['skipped'] += 1
            return {'ok': False, 'gate': gate, 'action': None, 'tried': [],
                    'note': why, 'skipped': True}

        tried = []
        for name, fn in plan:
            try:
                good, note = fn()
            except LS.GateError as e:
                good, note = False, f'{e.gate}: {str(e)[:120]}'
            except Exception as e:
                good, note = False, f'{type(e).__name__}: {str(e)[:120]}'
            LS.record_lesson(gate, name, good, note)
            tried.append({'action': name, 'ok': good, 'note': note})
            self.stats['repair'] += 1
            if self.verbose:
                _log(f'  动作 {name}: {"成功" if good else "失败"} — {note[:100]}')
            if good:
                self.stats['fixed'] += 1
                self._repair_ts[gate] = time.time()
                self._recent.append(time.time())
                st = LS.load_state()
                # 合并而不是替换：heal 里还存着 prev_gate，覆盖掉会让下一轮
                # 误判成"状态变了"，于是每轮都重复告警一遍。
                h = dict(st.get('heal') or {})
                h.update({'gate': gate, 'action': name, 'ok': True,
                          'note': note[:200], 'ts': time.time(), 'tried': tried})
                st['heal'] = h
                st['gate'] = LS.G_OPEN
                LS.save_state(st)
                return {'ok': True, 'gate': gate, 'action': name,
                        'note': note, 'tried': tried}

        self.stats['failed'] += 1
        self._repair_ts[gate] = time.time()
        st = LS.load_state()
        h = dict(st.get('heal') or {})
        h.update({'gate': gate, 'action': None, 'ok': False,
                  'note': (tried[-1]['note'] if tried else '无动作'),
                  'ts': time.time(), 'tried': tried})
        st['heal'] = h
        LS.save_state(st)
        return {'ok': False, 'gate': gate, 'action': None,
                'note': (tried[-1]['note'] if tried else '无动作'), 'tried': tried}

    # ------------------------------------------------------------ 一轮

    def run_once(self, source='timer'):
        """跑一轮。已经被别的线程占着就返回上一轮结论，不排队。"""
        if not self._run_lock.acquire(blocking=False):
            r = dict(self.last or {'ok': False, 'gate': LS.G_UNKNOWN})
            r['busy'] = True
            return r
        try:
            return self._run_once(source)
        finally:
            self._run_lock.release()

    def _run_once(self, source='timer'):
        """探测 -> 必要时修复 -> 复测。返回本轮结论。"""
        ev = self._drain()

        # 1) 有求解链路上报的失败：先按它给的 gate 修（比轮询更及时）
        wake_result = None
        if ev and self.auto:
            gate = ev['gate']
            ok, why = self._may_repair(gate)
            if ok:
                if self.verbose:
                    _log(f'求解链路报 {gate}（{ev.get("detail") or "-"}），开始修复')
                wake_result = self.repair(gate, ev.get('detail'))
                if not wake_result.get('ok') and self.verbose:
                    _log(f'  未能修好: {wake_result.get("note")}')

        # 2) 常规探测（链接通道）
        p = self.probe()

        # 链接通不代表求解通。有 probe_ticket 时再探一次 metadata：
        # 上游若只动求解接口，这里就是唯一能发现的地方。
        sp = self.probe_solve()
        if sp is not None:
            self.stats['solve_probe'] = self.stats.get('solve_probe', 0) + 1
            if p['ok'] and not sp['ok']:
                link_ms = float(p.get('ms') or 0)
                detail = p.get('detail') or ''
                p = dict(sp)
                p['ms'] = round(link_ms + float(sp['ms']), 1)
                p['detail'] = f'求解链路 {sp["gate"]}: {sp.get("detail") or detail}'[:200]
                p['link_gate'] = LS.G_OPEN
                if self.verbose:
                    _log(f'链接通道正常，但求解链路报 {p["gate"]}（{p["detail"][:70]}）')

        p['prev_gate'] = self._prev_gate()
        changed = p['prev_gate'] != p['gate']

        # 3) 探到失败 -> 修 -> 复测
        #
        # 复测只用 public 层，它回答的是"最优路径恢复了吗"。而修复动作可能是在
        # 别的层成功的（cookie / mirror / 打码），那种情况下 public 仍不通，
        # 但服务其实是好的 —— 所以"修好"这个结论以动作为准，复测只用来更新
        # 展示用的 gate 和 changed 判定。
        rep = None
        via = None
        if not p['ok'] and self.auto:
            rep = wake_result or self.repair(p['gate'], p.get('detail', ''))
            if rep.get('ok'):
                p2 = self.probe()
                p2['after_repair'] = True
                if p2['ok']:
                    p = p2
                else:
                    via = rep.get('action')
                changed = True

        solved = bool(p['ok']) or bool(rep and rep.get('ok'))
        self.last = {
            'ts': time.time(),
            'ok': solved,
            'gate': p['gate'],
            'gate_human': p['gate_human'],
            'ms': p['ms'],
            'detail': p.get('detail'),
            'changed': changed,
            'repaired': bool(rep and rep.get('ok')),
            'action': (rep or {}).get('action'),
            'via': via,
            'source': 'wake' if wake_result else source,
            'advice': LS.ADVICE.get(p['gate'], ''),
        }
        self._set_prev_gate(p['gate'])

        if changed:
            self._alert(p, rep)
        elif self.verbose:
            tag = '' if source == 'timer' else f'（{source} 手动触发）'
            _log(f'状态 {p["gate"]}（{p["ms"]}ms）{tag}' +
                 ('，已自愈' if self.last['repaired'] else ''))
        return self.last

    # 上一次的 gate 存在 state 里而不是内存里：进程重启后仍能判断"是否变化"，
    # 否则每次重启都会重新告警一遍同样的坏消息。
    def _prev_gate(self):
        return (LS.load_state().get('heal') or {}).get('prev_gate')

    def _set_prev_gate(self, gate):
        st = LS.load_state()
        h = dict(st.get('heal') or {})
        h['prev_gate'] = gate
        st['heal'] = h
        LS.save_state(st)

    # ------------------------------------------------------------ 告警

    def _alert(self, probe_res, rep):
        gate = probe_res['gate']
        human = probe_res['gate_human']
        if gate == LS.G_OPEN:
            msg = '上游恢复，可正常发链接'
        elif rep and rep.get('ok'):
            msg = f'检测到 {gate}（{human}），已自动修复（{rep.get("action")}）'
        else:
            msg = f'上游状态变为 {gate}（{human}）：{probe_res.get("detail") or ""}'
        _log(msg)
        self._notify(gate, human, probe_res.get('detail') or '',
                     action=(rep or {}).get('action'),
                     repaired=bool(rep and rep.get('ok')))

    def _notify(self, gate, human, detail='', action=None, repaired=False):
        """写告警日志 / 发 webhook。失败不抛 —— 通知不能让自愈线程死掉。"""
        log = _env('DELTA_ALERT_LOG')
        line = (f'{time.strftime("%Y-%m-%d %H:%M:%S")}\t{gate}\t{human}\t'
                f'{"fixed:" + str(action) if repaired else "open"}\t{str(detail)[:300]}')
        if log:
            try:
                d = os.path.dirname(os.path.abspath(log))
                if d:
                    os.makedirs(d, exist_ok=True)
                with open(log, 'a', encoding='utf-8') as f:
                    f.write(line + '\n')
            except Exception:
                pass
        hook = _env('DELTA_ALERT_WEBHOOK')
        if hook:
            try:
                LS._http().post(hook, json={
                    'event': 'delta_gate_change', 'gate': gate,
                    'gate_human': human, 'detail': str(detail)[:500],
                    'action': action, 'repaired': repaired,
                    'at': time.strftime('%Y-%m-%d %H:%M:%S'),
                }, timeout=(6, 12))
            except Exception:
                pass

    # ------------------------------------------------------------ 快照

    def snapshot(self):
        """给 /healthz 用：纯内存 + 一次 state 读，不发请求。

        /healthz 会被监控每几秒打一次，所以这里刻意只读一次 state
        （每次 load 都是一次文件读 + json 解析，叠起来会变成无谓的 IO）。
        """
        st = LS.load_state()
        last = self.last or {}
        now = time.time()
        lessons = [{'gate': l.get('gate'), 'action': l.get('action'),
                    'ok': l.get('ok'), 'note': l.get('note'),
                    'ago': round(now - float(l.get('ts') or 0), 1)}
                   for l in (st.get('lessons') or [])[:5]]
        return {
            'running': self.running(),
            'auto': self.auto,
            'mode': self.mode,
            'interval': self.interval,
            'cooldown': self.min_repair_gap,
            'hourly_cap': self.max_repairs_per_hour,
            'service': self.service,
            'last': ({k: last.get(k) for k in
                      ('ts', 'ok', 'gate', 'gate_human', 'ms', 'repaired',
                       'action', 'via', 'changed', 'detail', 'link_gate')} if last else None),
            'last_ago': (round(now - last['ts'], 1) if last.get('ts') else None),
            'stats': dict(self.stats),
            'lessons': lessons,
            'egress_best': st.get('egress_best'),
            'heal': st.get('heal') or {},
        }


# ---------------------------------------------------------------- 单例

_default = None
_default_lock = threading.Lock()


def default():
    global _default
    with _default_lock:
        if _default is None:
            _default = Healer(
                interval=int(_env('DELTA_HEAL_INTERVAL') or 180),
                service=int(_env('DELTA_SERVICE') or 6),
                min_repair_gap=(float(_env('DELTA_HEAL_COOLDOWN'))
                                if _env('DELTA_HEAL_COOLDOWN') else None),
                max_repairs_per_hour=(int(_env('DELTA_HEAL_HOURLY'))
                                      if _env('DELTA_HEAL_HOURLY') else None),
            )
        return _default


def enabled():
    return _flag('DELTA_HEAL', True)


if __name__ == '__main__':
    import argparse
    import json
    ap = argparse.ArgumentParser(description='自愈引擎（单轮 / 常驻）')
    ap.add_argument('--once', action='store_true', help='只跑一轮然后退出')
    ap.add_argument('--interval', type=int, default=int(_env('DELTA_HEAL_INTERVAL') or 180))
    ap.add_argument('--service', type=int, default=int(_env('DELTA_SERVICE') or 6))
    ap.add_argument('--json', action='store_true')
    ap.add_argument('--no-repair', action='store_true', help='只观测不修')
    a = ap.parse_args()

    h = Healer(interval=a.interval, service=a.service,
               auto=not a.no_repair)
    if a.once:
        r = h.run_once(source='cli')
        print(json.dumps(r, ensure_ascii=False, indent=2) if a.json
              else '\n'.join(f'{k}: {v}' for k, v in r.items()))
        sys.exit(0 if r['ok'] else 1)

    h.start()
    _log('常驻模式，Ctrl+C 退出')
    try:
        while h.running():
            time.sleep(1)
    except KeyboardInterrupt:
        h.stop()
        _log('已停止')
