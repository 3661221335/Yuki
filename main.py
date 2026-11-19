#!/usr/bin/env python3
# -*- coding: utf-8 -*-

# 用法: python main.py "<auth_url_or_ticket>" | ticket.txt | --generate N
import sys
import os
import time
import json
import argparse
import threading

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import auth_client as AUTH
import link_generator as LG
import key_cache as KC
import fastlane as FL

# 本地 key 缓存（与 server.py 共用同一个文件）。
# 复跑同一条链接时，metadata/step/poll 全程跳过 —— 那三段加起来 6~16s，
# 其中大头还是每次进程启动重做的 DNS+TCP+TLS 握手（走代理实测 ~4s）。
CACHE = KC.default()

FALLBACK_SERVICES = [3]
MAX_ROUNDS = 3
# 单条链接总墙钟上限（秒）。以前 75s —— 用户体感就是"卡住了"。
# 26s 覆盖 3 轮 checkpoint + 一次重试绰绰有余；再久说明上游真不行，
# 那种情况交给后台重试队列（retryq.py）继续磨，不能占着请求不放。
SOLVE_BUDGET = FL.SOLVE_BUDGET
STEP_TASK_DEADLINE = 20.0    # 一轮 step（含 gap 等待/多次重试）的墙钟上限
MAX_ROUNDS_HARD_CAP = 12     # 12 轮，防死循环
POLL_MAX_ATTEMPTS = 12       # about:blank 后轮询 key 的次数
POLL_INTERVAL = 0.08         # 每次轮询间隔(秒)
POLL_OVERLAP_DELAY = 0.05    # step 发出后多久开始并发轮询 key(重叠掉一个 RTT)
STEP_THROTTLE_RETRIES = 2    # 遇到限流时重试次数
STEP_THROTTLE_SLEEP = 0.9    # 限流退避休眠(秒)
# 相邻两次 step 的最小间隔。**不再是常数** —— 交给 fastlane.GapGovernor
# 自学习：1s 起步，被限流才往上抬。写死 5s 时，3 个 checkpoint 光睡觉就 10s。
MIN_STEP_GAP = FL.GAP_MIN


# 计时器
class Timer:
    # 累计各阶段耗时
    def __init__(self):
        self.phases = {}  # 阶段名 -> 总秒数
        self.t0 = None
        self.current_phase = None
        self.invalid_reason = None   # 若求解因"无效/过期链接"终止,记录上游原因
        self.fail_reason = None      # 失败分类: network-error / step-failed / poll-timeout ...

    def start(self, phase):
        # 开始计时一个阶段
        if self.current_phase is not None:
            self.stop()
        self.current_phase = phase
        self.t0 = time.time()

    def stop(self):
        # 停止当前阶段计时
        if self.current_phase is not None and self.t0 is not None:
            dt = time.time() - self.t0
            self.phases[self.current_phase] = self.phases.get(self.current_phase, 0.0) + dt
            self.current_phase = None
            self.t0 = None

    def add(self, name, seconds):
        # 直接添加耗时
        self.phases[name] = self.phases.get(name, 0.0) + seconds

    def total(self):
        return sum(self.phases.values())

    def summary(self):
        parts = []
        for name, secs in sorted(self.phases.items(), key=lambda x: -x[1]):
            pct = secs / self.total() * 100 if self.total() > 0 else 0
            parts.append(f"    {name}: {secs:.1f}s ({pct:.0f}%)")
        return '\n'.join(parts)

    def __repr__(self):
        return f"Timer({self.total():.1f}s total, {len(self.phases)} phases)"


#验证码
# 上游已取消图形验证码环节:step 的 captcha 字段不校验(null/任意值均可),
# 旧版 captcha 服务已换成 orbit 类型、旧识别器失效,这里不再有识别步骤。
# The far end dropped the picture captcha (the step captcha field is not
# checked), so there is no recognition step any more.

# 失败原因 -> 人话。排查时一眼能分清"是链接的问题还是网络的问题"。
FAIL_HINTS = {
    'network-error': '上游网络不可达，请检查本地代理 / 网络后重试',
    'invalid-link': '链接无效或已过期（上游 metadata 两次均明确拒绝）',
    'step-failed': 'step 全部失败（上游拒绝或网络中断）',
    'no-url': 'step 响应里没有跳转地址',
    'poll-timeout': '轮询超时，上游未在预期次数内下发 key',
    'key-exists-unreadable': '该标识已出过 key，但读不回来',
    'no-callback-no-key': '既没有回调也没有 key',
    'round-exhausted': '轮次耗尽仍未出 key',
    'budget-exhausted': '超过单条链接时长上限，上游响应过慢',
}


#Metadata->Service解析
def resolve_service(ticket, session=None, verbose=True):
    #从metadata获取service及checkpointCount（决定需要多少轮/步），返回 (service, checkpointCount)
    svc, cp, valid, reason = resolve_meta(ticket, session=session, verbose=verbose)
    return svc, cp


def meta_invalid(meta):
    # metadata 是否明确判定"票无效/过期"
    if not isinstance(meta, dict):
        return False
    if meta.get('success') is not False or meta.get('transient'):
        return False
    msg = str(meta.get('message') or meta.get('error') or '').lower()
    return any(m in msg for m in AUTH.INVALID_MARKERS)


def resolve_meta(ticket, session=None, verbose=True, mute=None):
    """一次 metadata 调用同时得到 (service, checkpointCount, valid, invalid_reason)

    mute: 可选的 {'on': bool} 共享开关。solve_chain 提前返回后，后台 metadata
    线程可能还在跑，它的日志会插到结果之后，看起来像出错。所有打印都过 log()，
    返回前置位即静音。
    """
    def log(msg):
        if verbose and not (mute and mute.get('on')):
            print(msg, flush=True)

    cp = None
    try:
        # 带对冲的 metadata：正常 0.4s，偶尔一发挂到 7s 也不再拖住整条链
        # （迟发第二发，取先回的；只对幂等 GET 这么做，step 绝不并发重发）。
        meta = FL.meta_fast(ticket, session=session)
        # 网络层失败：说清楚是网络，别让上层误判成"票无效"
        if isinstance(meta, dict) and meta.get('conn_error'):
            log(f'  [meta] 网络异常: {str(meta.get("error", ""))[:80]}')
            return None, cp, True, None
        # 单次 invalid payload 不可信，必须复议一次再下结论。
        #
        # 实测同一张票：第一发回 {"success":false,"message":"invalid payload."}，
        # 三分钟后再查却是正常 metadata（Delta Android Keysystem / checkpointCount=1）
        # 并顺利出了 key。上游边缘节点/会话复制存在窗口，把一次 200 里的 invalid payload
        # 当死刑会直接误杀有效链接（且不重试、1.2s 就返回），这才是"偶发解不了"的来源。
        # 复议只多花一次 ~0.4s 的 metadata，且只在失败路径上发生，成功路径零开销。
        if meta_invalid(meta):
            time.sleep(0.2)
            confirm = FL.meta_fast(ticket, session=session)
            if meta_invalid(confirm):
                reason = str(confirm.get('message') or confirm.get('error')
                             or meta.get('message') or 'invalid link')
                log(f'  [meta] 无效链接（已复议）: {reason}')
                return None, cp, False, reason
            log('  [meta] 首次判定无效，复议通过，继续求解')
            meta = confirm
        if isinstance(meta, dict):
            data = meta.get('data', meta)
            if isinstance(data, dict):
                profile = data.get('activeRevenueProfile', {})
                if isinstance(profile, dict) and 'service' in profile:
                    svc = int(profile['service'])
                    try:
                        cpn = profile.get('checkpointCount')
                        cp = int(cpn) if cpn else None
                    except (TypeError, ValueError):
                        cp = None
                    dur = data.get('duration', '?')
                    log(f'  [meta] service={svc} checkpointCount={cp} duration={dur}h')
                    return svc, cp, True, None
    except Exception as e:
        log(f'  [meta] 获取失败: {e}')
    return None, cp, True, None


#Step推进
def throttled(r):
    # 判断 step 是否被服务器限流（"finishing checkpoints too fast" 等）
    if not isinstance(r, dict):
        return False
    msg = ' '.join(str(r.get(k, '')) for k in ('message', 'error', 'detail')).lower()
    return ('too fast' in msg) or ('slow down' in msg) or ('too many' in msg)


def do_step_with_retry(ticket, service=None, session=None, verbose=True, timer=None,
                       gap_state=None, overlap_poll=False, poll_session=None):
    #执行step失败时尝试回退 遇到限流则退避后直接重试
    services_to_try = []
    if service is not None:
        services_to_try.append(service)
    for svc in FALLBACK_SERVICES:
        if svc not in services_to_try:
            services_to_try.append(svc)

    if timer:
        timer.start('step')

    # 主动保证本链相邻 step 的间隔，避免触发限流。
    # 间隔是自学习出来的（fastlane.GapGovernor）：1s 起步，真的被限流才往上抬，
    # 连续顺利就往下收。以前写死 5s，3 个 checkpoint 光等就 10s。
    if gap_state is not None:
        slept = FL.governor().wait(gap_state.get('ts'))
        if slept > 0.05 and verbose:
            print(f'  [step] 步间隔 {slept:.2f}s（自适应当前 {FL.step_gap():.2f}s）',
                  flush=True)

    overlap_box = {}
    overlap_thread = None

    def overlap_poll_worker():
        #step发出后稍等一下再开始轮询,避免过早的无谓请求
        time.sleep(POLL_OVERLAP_DELAY)
        sess = poll_session if poll_session is not None else session
        for _ in range(POLL_MAX_ATTEMPTS):
            if overlap_box.get('stop'):
                return
            try:
                st = AUTH.get_session_status(ticket, session=sess)
                data = st.get('data', st) if isinstance(st, dict) else {}
                k = data.get('key', '')
                if k and k != 'KEY_NOT_FOUND':
                    overlap_box['key'] = k
                    return
            except Exception:
                pass
            time.sleep(POLL_INTERVAL)

    for svc in services_to_try:
        for attempt in range(STEP_THROTTLE_RETRIES + 1):
            try:
                t0 = time.time()
                if overlap_poll and overlap_thread is None:
                    overlap_thread = threading.Thread(target=overlap_poll_worker, daemon=True)
                    overlap_thread.start()
                r = AUTH.do_step(ticket, service=svc, session=session)
                dt = time.time() - t0
                if gap_state is not None:
                    gap_state['ts'] = time.time()
                if isinstance(r, dict) and r.get('success'):
                    FL.governor().note(ok=True)
                    if timer:
                        timer.stop()
                    if verbose:
                        print(f'  [step] service={svc} -> 成功 ({(dt * 1000):.0f}ms)', flush=True)
                    return svc, r, overlap_box
                # 这张票已经出过 key 了（重复求解 / 用户二次提交）。
                # 以前它会走"step 失败 -> 换 service -> 3 轮重试"，实测白烧 57s 才在最后
                # 的 status 里捡到 key。这里直接标记，交给主循环立刻去读。
                if isinstance(r, dict):
                    msg = str(r.get('message') or r.get('error') or '').lower()
                    if 'already exists' in msg:
                        if timer:
                            timer.stop()
                        r = dict(r)
                        r['_key_exists'] = True
                        if verbose:
                            print(f'  [step] 该标识已生成过 key（{msg[:48]}），直接读取', flush=True)
                        return None, r, overlap_box
                if throttled(r):
                    FL.governor().note(throttled=True)
                    if attempt < STEP_THROTTLE_RETRIES:
                        if verbose:
                            print(f'  [step] service={svc}: 限流，退避 {STEP_THROTTLE_SLEEP}s 后重试'
                                  f' ({attempt + 1}/{STEP_THROTTLE_RETRIES})', flush=True)
                        time.sleep(STEP_THROTTLE_SLEEP)
                        continue
                if verbose:
                    err = json.dumps(r)[:200] if isinstance(r, dict) else str(r)[:200]
                    print(f'  [step] service={svc}: {err} ({(dt * 1000):.0f}ms)', flush=True)
                break
            except Exception as e:
                if verbose:
                    print(f'  [step] service={svc} 异常: {e}', flush=True)
                break

    if timer:
        timer.stop()
    overlap_box['stop'] = True
    return None, {'success': False, 'error': 'all services failed'}, overlap_box


#Key提取
def check_key_in_response(ticket, session=None, verbose=True, timer=None, hedge=True):
    #检查会话中是否已有key
    # hedge=True 用于"单独查一次"（对尾延迟敏感）；轮询循环里传 False，
    # 因为那里本来就每 80ms 打一发，再对冲等于把请求数翻倍。
    try:
        if timer:
            timer.start('poll')
        if hedge:
            st = FL.status_fast(ticket, session=session)
        else:
            st = AUTH.get_session_status(ticket, session=session)
        if timer:
            timer.stop()
        st_data = st.get('data', st) if isinstance(st, dict) else {}
        key = st_data.get('key', '')
        if key and key != 'KEY_NOT_FOUND':
            if verbose:
                print(f'  [key] 发现 KEY: {key}', flush=True)
            return key
        if verbose and key:
            print(f'  [key] 尚未就绪: {key}', flush=True)
    except Exception as e:
        if verbose:
            print(f'  [key] 检查异常: {e}', flush=True)
    return None


def poll_for_key(ticket, session=None, max_attempts=3, interval=0, verbose=True, timer=None):
    #轮询等待key：首查立即，之后每次间隔 interval 秒
    for i in range(max_attempts):
        key = check_key_in_response(ticket, session=session, verbose=verbose,
                                    timer=timer, hedge=False)
        if key:
            return key
        if interval > 0 and i < max_attempts - 1:
            time.sleep(interval)
    return None


class _Task:
    """daemon 线程版的 Future。

    为什么不用 ThreadPoolExecutor —— 这是个会让进程"假死"的坑：
    concurrent.futures 的 worker 是 non-daemon 线程，且该模块在 atexit 注册了
    _python_exit()，退出时 join 所有 worker。所以 shutdown(wait=False) 只是让
    主线程不阻塞，**进程退出照样要等**：一条卡住的 metadata 请求就能让进程
    在打印完结果后继续挂着（实测逻辑 7.3s 跑完，进程耗到 120s 被 SIGTERM）。
    daemon 线程没有这个问题，解释器退出时直接丢弃。
    """

    def __init__(self, fn, *args, **kwargs):
        self._box = {}
        self._done = threading.Event()
        threading.Thread(target=self._run, args=(fn, args, kwargs),
                         daemon=True).start()

    def _run(self, fn, args, kwargs):
        try:
            self._box['v'] = fn(*args, **kwargs)
        except BaseException as e:      # noqa: BLE001 - 要连 KeyboardInterrupt 一起兜住
            self._box['e'] = e
        finally:
            self._done.set()

    def result(self, timeout=None):
        if not self._done.wait(timeout):
            raise TimeoutError('task timeout')
        if 'e' in self._box:
            raise self._box['e']
        return self._box.get('v')

    def done(self):
        return self._done.is_set()


#主循环
def solve_chain(ticket, verbose=True, max_rounds=MAX_ROUNDS, session=None,
                budget=None):
    #完整链路ticket->captcha->step->decode->repeat->key
    #
    # budget: 本条链接的墙钟预算（秒）。服务端会按"第几次尝试"给不同的值 ——
    # 首发给足、二发收紧，总时长才不会因为重试而翻倍。
    budget = float(budget or SOLVE_BUDGET)
    if session is None:
        session = AUTH.create_session()
    current_ticket = ticket
    current_service = None
    timer = Timer()
    invalid_reason = [None]
    round_cap = max(max_rounds, 1)
    round_idx = 0
    last_exit = ['round-exhausted']
    gap_state = {'ts': 0.0}
    meta_miss_streak = [0]      # 连续多少轮拿不到 metadata（判定网络异常用）
    t_start = time.time()       # 墙钟起点，配合 SOLVE_BUDGET 兜底

    while round_idx < round_cap:
        # 每轮一份静音开关：超时的线程还活着时置位，免得它的日志插到后面的输出里
        # （实测会看到"[!] metadata 不可用"之后又冒出一条"[meta] 网络异常"）。
        mute = {'on': False}
        # 总时长硬上限：单条链接无论上游怎么抽，都不会超过这个数。
        # 没有这道闸，一轮轮叠加起来能跑到 90s+（实测就是"卡死"的来源之一）。
        if time.time() - t_start > budget:
            last_exit[0] = 'budget-exhausted'
            if verbose:
                print(f'  [!] 已达单条链接时长上限 {budget:.0f}s，停止重试', flush=True)
            mute["on"] = True
            timer.fail_reason = last_exit[0]
            return None, timer
        if verbose:
            print(f'  [{round_idx + 1}/{round_cap}]', flush=True)
        if timer:
            timer.start('meta')
        meta_session = AUTH.create_session()
        stat_session = None
        cpc = None
        mres = None
        stat_task = None
        meta_task = None

        # 并发：第 1 轮同时查 status（已完成链接能秒回）和 metadata
        if round_idx == 0:
            stat_session = AUTH.create_session()
            stat_task = _Task(check_key_in_response, current_ticket,
                              stat_session, verbose, None)
        meta_task = _Task(resolve_meta, current_ticket, meta_session, verbose, mute)

        if stat_task is not None:
            # 竞争等待：status 和 metadata 谁先给结论就用谁。
            # 以前是先死等 status 6s、再等 metadata —— status 一旦卡住，
            # 明明已经回来的 metadata 也只能陪着干等，白烧 6 秒。
            early = None
            wait_budget = 9.0 if round_idx == 0 else 7.0
            t_wait = time.time()
            while True:
                if stat_task is not None and stat_task.done():
                    try:
                        early = stat_task.result(0)
                    except Exception:
                        early = None
                    if early:
                        break
                    stat_task = None      # 有结论了（没有 key），不必再等它
                if meta_task.done():
                    break
                if time.time() - t_wait >= wait_budget:
                    break
                time.sleep(0.02)
            if early:
                if verbose:
                    print(f'  [early] 该链接已完成，直接返回已有 KEY', flush=True)
                mute["on"] = True
                timer.fail_reason = last_exit[0]
                return early, timer

        # metadata 那一发的等待：首轮上面的竞争循环已经在等它了（0.05s 只是
        # 收结果）；后续轮次没有竞争循环，必须给足正常的等待时间。
        # 这里曾经写成一律 0.05s，结果第 2 轮开始 50ms 就判定 metadata 超时，
        # 明明链路是通的却被当成"网络异常"，整条链直接白跑。
        meta_wait = 9.0 if round_idx == 0 else 7.0
        if stat_task is not None:
            meta_wait = 0.05
        timed_out = False
        try:
            mres = meta_task.result(timeout=meta_wait)
        except TimeoutError:
            timed_out = True
            mute['on'] = True      # 线程还在跑，先静音再放手
            mres = None
        except Exception:
            mres = None

        meta_session.close()
        try:
            if stat_session is not None:
                stat_session.close()
        except Exception:
            pass

        # metadata 没拿到：先区分"网络不通"和"票有问题"，别急着猜 service。
        # 以前这里拿 service=None 去撞 FALLBACK_SERVICES，等于用一次 12s 的 step
        # 超时去复核一件 metadata 已经说清楚的事 —— 纯浪费，而且网络挂了 step 也是挂。
        #
        # 同步重问只在两种情况下做：
        #   1) 首轮超时 —— 很可能是首次建连慢，值得再来一次；
        #   2) 请求快速失败（拿到了返回值但没有 service）—— 说明链路通，重问有意义。
        # 非首轮超时说明这一轮链路就是卡住了，再同步问一次只会把一轮拖成 20s+。
        need_retry = (mres is None and (round_idx == 0 or not timed_out)) or \
                     (mres is not None and mres[0] is None and mres[2] is True)
        if need_retry:
            retry_session = AUTH.create_session()
            # 同步重问也必须带墙钟上限：这一发是主线程在等，一旦上游把连接挂住，
            # 它会连着主线程一起卡死（实测：打桩让 metadata 永久阻塞，整条链路
            # 直接挂到被 SIGTERM，前面所有超时保护全白做）。
            retry_task = _Task(resolve_meta, current_ticket, retry_session, verbose, None)
            try:
                mres = retry_task.result(timeout=10)
            except Exception:
                mres = (None, None, True, None)
            finally:
                retry_session.close()

        if timer:
            timer.stop()

        # 非首轮超时、又没有重问时 mres 会是 None —— 这里必须兜成四元组，
        # 否则下面一解包就是 TypeError（实测踩过：网络抖动第二轮直接崩栈）。
        if mres is None:
            mres = (None, None, True, None)

        svc, cpc, mvalid, mreason = mres

        # service 始终拿不到 = 上游 metadata 不可用（网络层），不是"票无效"。
        # 连续两轮都这样就直接收手，给明确原因，而不是继续猜 service 烧下去。
        if svc is None and mvalid:
            meta_miss_streak[0] += 1
            if verbose:
                print(f'  [!] metadata 不可用（第 {meta_miss_streak[0]} 次，网络层）', flush=True)
            if meta_miss_streak[0] >= 3:
                last_exit[0] = 'network-error'
                if verbose:
                    print('  [!] 连续 3 轮拿不到 metadata，判定为上游网络异常，停止重试', flush=True)
                mute["on"] = True
                timer.fail_reason = last_exit[0]
                return None, timer
            round_idx += 1
            # 退避递增：链路抖动往往是几秒级的。0.4s 起步而不是 1s ——
            # 有对冲在，metadata 挂住时已经自动换过一发，这里只需短暂错峰。
            time.sleep(0.4 * meta_miss_streak[0])
            continue
        meta_miss_streak[0] = 0

        current_service = svc
        if not mvalid:
            # 明确无效/过期链接
            invalid_reason[0] = mreason
            last_exit[0] = 'invalid-link'
            timer.invalid_reason = mreason
            mute["on"] = True
            timer.fail_reason = last_exit[0]
            return None, timer

        # 按 checkpointCount 动态延长轮数
        if cpc is not None:
            need = cpc + 1
            if need > round_cap:
                round_cap = min(need, MAX_ROUNDS_HARD_CAP)
                if verbose:
                    print(f'  [rounds] checkpointCount={cpc} -> 需 {need} 轮 (当前 cap={round_cap})', flush=True)

        if current_service is not None and verbose:
            print(f'  [service] metadata: {current_service}', flush=True)

        # 对最后一步开启step/poll 重叠 两个串行RTT压成一个
        last_step = round_idx > 0
        # step 整段也要带墙钟上限。do_step 内部虽然有 call_with_deadline，
        # 但那只能管住"单次 HTTP"，管不住多个 service × 多次重试叠起来的总时长，
        # 更管不住内部任何一处意外阻塞 —— 实测打桩让 do_step 永久 sleep，
        # 整条链路直接卡到被 SIGTERM。这里再兜一层，到点就按失败放行。
        step_task = _Task(
            do_step_with_retry,
            current_ticket,
            service=current_service,
            session=session,
            verbose=verbose,
            timer=timer,
            gap_state=gap_state,
            overlap_poll=last_step,
            poll_session=None
        )
        step_timed_out = False
        # 预算要动态收紧：只在轮次开头检查是不够的 —— 一轮 step 自己就能吃 35s，
        # 3 轮下来会冲到 105s+（实测），把"单条最多 75s"的承诺打穿。
        _remain = budget - (time.time() - t_start)
        if _remain <= 5:
            last_exit[0] = 'budget-exhausted'
            if verbose:
                print(f'  [!] 剩余预算 {_remain:.0f}s 不足以再走一步，停止', flush=True)
            mute["on"] = True
            timer.fail_reason = last_exit[0]
            return None, timer
        _step_deadline = min(STEP_TASK_DEADLINE, _remain)
        try:
            service, resp, overlap = step_task.result(timeout=_step_deadline)
        except TimeoutError:
            step_timed_out = True
            service, resp, overlap = None, {}, {}
            if verbose:
                print(f'  [!] step 超过 {_step_deadline:.0f}s 未返回，按失败处理', flush=True)
        except Exception as e:
            service, resp, overlap = None, {}, {}
            if verbose:
                print(f'  [!] step 异常: {e}', flush=True)
        if overlap.get('key'):
            if verbose:
                print(f'  [key] 发现 KEY (重叠轮询): {overlap["key"]}', flush=True)
            mute["on"] = True
            return overlap['key'], timer
        # 上游说这张票已经有 key 了：立刻去读，别把它当 step 失败去换 service 重试
        if isinstance(resp, dict) and resp.get('_key_exists'):
            if verbose:
                print(f'  [poll] 该链接已出过 key，轮询读取...', flush=True)
            key = poll_for_key(current_ticket, session=session, verbose=verbose, timer=timer,
                               max_attempts=20, interval=0.25)
            if key:
                mute["on"] = True
                return key, timer
            last_exit[0] = 'key-exists-unreadable'
            break
        if service is None:
            if verbose:
                print(f'  [-] 第 {round_idx + 1} round step 全部失败, 跳过', flush=True)
            last_exit[0] = 'step-failed'
            round_idx += 1
            continue

        current_service = service
        #提取URL
        url = (resp.get('data') or {}).get('url', '')
        if not url:
            if verbose:
                print(f'  [-] 响应中没有 URL', flush=True)
            last_exit[0] = 'no-url'
            round_idx += 1
            continue

        if verbose:
            url_short = url[:80] + '...' if len(url) > 80 else url
            print(f'  [url] {url_short}', flush=True)

        if url == 'about:blank':
            if verbose:
                print(f'  [poll] (about:blank) 轮询 key, 最多 {POLL_MAX_ATTEMPTS} 次/每次{POLL_INTERVAL}s...', flush=True)
            key = poll_for_key(current_ticket, session=session, verbose=verbose, timer=timer,
                               max_attempts=POLL_MAX_ATTEMPTS, interval=POLL_INTERVAL)
            if key:
                mute["on"] = True
                return key, timer
            if verbose:
                print(f'  [-] 轮询 {POLL_MAX_ATTEMPTS} 次仍未拿到 key', flush=True)
            last_exit[0] = 'poll-timeout'
            round_idx += 1
            continue

        #解码r=参数->下一张ticket
        callback = AUTH.decode_callback_url(url)
        if callback:
            next_ticket = AUTH.extract_ticket_from_callback(callback)
            if next_ticket and len(next_ticket) > 50:
                if verbose:
                    print(f'  [next] 新 ticket: {next_ticket[:24]}... ({len(next_ticket)} chars)', flush=True)
                current_ticket = next_ticket
                round_idx += 1
                continue

        #无r=回调->lootlabs 链接轮询 key
        if verbose:
            print(f'  [info] 无 r= 回调, 尝试轮询 key...', flush=True)
        key = check_key_in_response(current_ticket, session=session, verbose=verbose, timer=timer)
        if key:
            mute["on"] = True
            return key, timer
        last_exit[0] = 'no-callback-no-key'
        break

    #最后检查
    key = check_key_in_response(current_ticket, session=session, verbose=verbose, timer=timer)
    if key:
        mute["on"] = True
        return key, timer

    # 失败详情统一交给 main() 用 FAIL_HINTS 讲人话，这里不再重复打印
    mute["on"] = True
    timer.fail_reason = last_exit[0]
    return None, timer


# CLI
def read_tickets_from_arg(arg):
    """把「一条 URL / 一个裸 ticket / 一个 txt 文件（一行一条）」统一展开成列表。

    以前直接调 extract_ticket_from_arg，它把**整个文件内容**当一个参数读 ——
    多行文件会被拼成一条超长垃圾 ticket，表现是"明明给了 5 条，只解了 1 条，
    而且失败"。这里按行展开，跳过空行和 # 注释。
    """
    t = (arg or '').strip()
    if not t:
        return []
    # 看着像文件路径、又不是 URL：按行读
    if not t.startswith('http') and (t.endswith('.txt') or '/' in t or '\\' in t):
        try:
            with open(t, encoding='utf-8', errors='replace') as f:
                lines = [ln.strip() for ln in f]
        except OSError:
            return [AUTH.extract_ticket_from_arg(t)]   # 不是文件，当裸 ticket
        out = []
        for ln in lines:
            if not ln or ln.startswith('#'):
                continue
            tk = AUTH.extract_ticket(ln)
            if tk:
                out.append(tk)
        return out
    return [AUTH.extract_ticket_from_arg(t)]


def main():
    ap = argparse.ArgumentParser(
        description='Delta自动求解器',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='''示例:
  %(prog)s "https://auth.platorelay.com/a?d=<ticket>"
  %(prog)s "<raw_ticket>"
  %(prog)s ticket.txt
  %(prog)s --generate 3
        '''
    )
    ap.add_argument('target', nargs='?', help='auth URL / ticket / 文件路径')
    ap.add_argument('--generate', '-g', type=int, default=0,
                    help='通过 Platoboost API 生成 N 条测试链接')
    ap.add_argument('--quiet', '-q', action='store_true',
                    help='静默模式 (只输出结果)')
    ap.add_argument('--max-rounds', type=int, default=MAX_ROUNDS,
                    help=f'最大 round 数 (默认 {MAX_ROUNDS})')
    ap.add_argument('--no-auto', action='store_true',
                    help='只生成链接, 不求解')
    ap.add_argument('--no-cache', action='store_true',
                    help='忽略本地 key 缓存, 强制重新求解')
    args = ap.parse_args()

    verbose = not args.quiet

    # 后台盯上游客户端版本：启动刷一次，之后每小时一次。
    # Watch the far end's client version in the background: once at startup,
    # then hourly.
    AUTH.start_version_watcher()
    # 预热 TLS 连接：第一个请求不必现付握手钱（走代理时那是 1~4s）
    FL.prewarm(background=True)

    tickets = []
    gen_start = time.time()

    if args.generate > 0:
        if verbose:
            print(f'[*] 生成 {args.generate} 条链接...', flush=True)
        try:
            urls = LG.batch_links(args.generate)
            tickets = [AUTH.extract_ticket(u) for u in urls]
            if verbose:
                print(f'[*] 成功获取 {len(tickets)} 条 ticket', flush=True)
        except Exception as e:
            print(f'[-] 生成链接失败: {e}', file=sys.stderr, flush=True)
            sys.exit(1)
    elif args.target:
        # 命令行参数才允许从文件读 ticket（一行一个那种）。
        # HTTP 接口走的是 extract_ticket，不读文件。
        tickets.extend(read_tickets_from_arg(args.target))
    else:
        ap.print_help()
        sys.exit(1)

    if args.no_auto:
        for t in tickets:
            print(f'https://auth.platorelay.com/a?d={t}')
        return

    if not tickets:
        # 一条都没生成出来：直接说清楚，别打一个"0/0 成功"的汇总糊弄人
        print('[-] 没有可求解的链接（生成阶段全部失败，通常是上游/代理不可达）',
              file=sys.stderr, flush=True)
        sys.exit(2)

    #逐条求解
    results = []
    cache_hits = 0
    for i, ticket in enumerate(tickets):
        t0 = time.time()

        # 先查本地缓存。命中时 metadata / step / poll 全部跳过，连进程启动都要
        # 重做的 DNS+TCP+TLS 握手也一并省掉 —— 这才是复跑能进亚秒的唯一办法。
        # 上游对同一 identifier 本来就只回 "already exists"，再去问一遍纯属白付。
        key, hit, cached_solve = None, False, 0.0
        if not args.no_cache:
            key, hit, cached_solve = CACHE.get(ticket)

        if hit:
            timer = Timer()
            dt = time.time() - t0
            cache_hits += 1
            if verbose:
                print(f'  [cache] 命中本地缓存（原始求解 {cached_solve:.1f}s，本次未出网）',
                      flush=True)
        else:
            key, timer = solve_chain(ticket, verbose=verbose,
                                     max_rounds=args.max_rounds, session=None)
            dt = time.time() - t0
            if key:
                CACHE.put(ticket, key, dt)
                # CLI 进程马上退出，不能等 debounce 线程 —— 不落盘等于白解
                CACHE.save_now()

        results.append((i, key, timer, dt))

        if key:
            print(f'\n{"=" * 60}', flush=True)
            print(f'[+] DELTA KEY #{i + 1}: {key}', flush=True)
            print(f'[+] 耗时: {dt:.2f}s', flush=True)
            if hit:
                print('[+] 来源: 本地缓存（未出网）')
            else:
                print('[+] 阶段明细:')
                print(timer.summary())
            print(f'{"=" * 60}', flush=True)
        elif verbose:
            reason = FAIL_HINTS.get(timer.fail_reason, timer.fail_reason or 'unknown')
            print(f'\n[-] 链接 {i + 1}: 未获取到 key（{reason}）', flush=True)
            if timer.invalid_reason:
                print(f'[-] 上游原因: {timer.invalid_reason}', flush=True)
            if timer.total() > 0:
                print(f'[-] 耗时: {dt:.1f}s')
                print(f'[-] 阶段明细:')
                print(timer.summary())

    #汇总
    total_elapsed = time.time() - gen_start
    success_count = sum(1 for _, key, _, _ in results if key)
    total_timer = Timer()
    for _, _, timer, _ in results:
        for name, secs in timer.phases.items():
            total_timer.add(name, secs)

    print(f'\n{"=" * 60}', flush=True)
    print(f'[+] 汇总: {success_count}/{len(tickets)} 成功', flush=True)
    if cache_hits:
        print(f'[+] 其中 {cache_hits} 条来自本地缓存（未出网）', flush=True)
    print(f'[+] 总耗时: {total_elapsed:.2f}s', flush=True)
    if len(tickets) > 0:
        print(f'[+] 平均每链接: {total_elapsed / len(tickets):.2f}s', flush=True)
    if total_timer.total() > 0:
        print(f'[+] 总阶段明细:')
        print(total_timer.summary())
    print(f'{"=" * 60}', flush=True)


if __name__ == '__main__':
    main()
