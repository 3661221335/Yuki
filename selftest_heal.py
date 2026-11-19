#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""自愈引擎打桩自检。

上游什么时候加门禁没法预知，但"失败之后系统会不会自己学、自己修"现在就能测。
这个脚本把各种失败伪造出来，跑一遍完整的 探测 -> 修复 -> 复测 闭环，
断言结论对不对。

    python selftest_heal.py

退出码 0 全过 / 1 有失败。整个过程不连上游（CS 里的假实现接管了所有请求）。
"""
import os
import shutil
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import link_source as LS
import selfheal as SH

PASS, FAIL = [], []


def ok(name, cond, extra=''):
    (PASS if cond else FAIL).append(name)
    mark = 'PASS' if cond else 'FAIL'
    print(f'  [{mark}] {name}' + (f'   {extra}' if extra else ''))


# ---------------------------------------------------------------- 假上游

class FakeUpstream(object):
    """按脚本依次作答。脚本用完后重复最后一条。

    一条脚本可以是：url 字符串 / 异常实例 / 接收 extra 的函数。
    """

    def __init__(self, script):
        self.script = list(script)
        self.n = 0
        self.bodies = []

    def probe_start(self, service=6, extra=None, identifier=None):
        self.n += 1
        # 用真实的 _build_body 拼包：这样"学到字段名后下一次请求会带上"这件事
        # 是被真正验证的，而不是靠测试桩自己编一套逻辑。
        body = LS._build_body(service, identifier or 'test-hwid')
        if extra:
            body.update(extra)
        self.bodies.append(body)
        item = self.script[min(self.n - 1, len(self.script) - 1)]
        if callable(item):
            return item(body)
        if isinstance(item, BaseException):
            raise item
        return item


class FakeAuth(object):
    def __init__(self, mode='direct'):
        self.mode = mode

    def proxy_info(self):
        return {'mode': self.mode, 'proxy': None, 'note': 'fake'}

    def reset_egress(self, mode=None, timeout=None):
        self.mode = mode or self.mode
        return True, f'fake -> {self.mode}'


class Ctx(object):
    """替换掉所有会出网的东西，并给一个干净的 state 文件。"""

    def __init__(self, upstream, cookie=None, mirror=None, auth=None,
                 sitekey=''):
        self.upstream = upstream
        # 默认把 cookie / mirror 打成"不可用"，否则测试会真的去连上游
        self.cookie = cookie if cookie is not None else \
            LS.GateError(LS.G_UNKNOWN, '测试桩：cookie 层不可用')
        self.mirror = mirror if mirror is not None else \
            LS.GateError(LS.G_UNKNOWN, '测试桩：mirror 层不可用')
        self.auth = auth or FakeAuth()
        self.sitekey = sitekey

    def __enter__(self):
        self._old = {
            'probe_start': LS.probe_start,
            'CookieProvider': LS.CookieProvider,
            'MirrorProvider': LS.MirrorProvider,
            'hunt_sitekey': LS.hunt_sitekey,
            'reset_http': LS.reset_http,
            'STATE_FILE': LS.STATE_FILE,
            'AUTH': SH.AUTH,
        }
        d = tempfile.mkdtemp(prefix='healselftest_')
        LS.STATE_FILE = os.path.join(d, 'state.json')
        LS.probe_start = self.upstream.probe_start
        LS.reset_http = lambda: None
        SH.AUTH = self.auth
        LS.CookieProvider = _fake_provider('cookie', self.cookie)
        LS.MirrorProvider = _fake_provider('mirror', self.mirror)
        if self.sitekey:
            def _hunt(verbose=False):
                st = LS.load_state()
                st['sitekey'] = self.sitekey
                LS.save_state(st)
                return self.sitekey
            LS.hunt_sitekey = _hunt
        return self

    def __exit__(self, *a):
        LS.probe_start = self._old['probe_start']
        LS.CookieProvider = self._old['CookieProvider']
        LS.MirrorProvider = self._old['MirrorProvider']
        LS.hunt_sitekey = self._old['hunt_sitekey']
        LS.reset_http = self._old['reset_http']
        SH.AUTH = self._old['AUTH']
        d = os.path.dirname(LS.STATE_FILE)
        LS.STATE_FILE = self._old['STATE_FILE']
        shutil.rmtree(d, ignore_errors=True)
        return False


def _fake_provider(name, url):
    class P(object):
        pass

    def fetch(self, service, identifier):
        if isinstance(url, BaseException):
            raise url
        LS._record(name, True, gate=LS.G_OPEN)
        return url

    P.name = name
    P.fetch = fetch
    return P


def mk(**kw):
    kw.setdefault('interval', 60)
    kw.setdefault('verbose', False)
    kw.setdefault('min_repair_gap', 0)      # 测试里不等冷却
    return SH.Healer(**kw)


def gate_err(gate, msg):
    return LS.GateError(gate, msg)


# ---------------------------------------------------------------- 用例

def t_schema_learn():
    """上游加了必填字段：第一次报错 -> 学会字段名 -> 重试通过。"""
    print('\n[1] 缺字段自学习（最重要的一条）')

    def answer(body):
        # 只有带上 turnstileToken 这个字段才放行，模拟"字段必须存在"
        if 'turnstileToken' in body:
            return 'https://auth.platorelay.com/a?d=ok'
        raise gate_err(LS.G_SCHEMA, "body must have required property 'turnstileToken'")

    up = FakeUpstream([answer])
    with Ctx(up):
        h = mk()
        r = h.run_once()
        ok('最终状态 open', r['ok'] and r['gate'] == LS.G_OPEN, r['gate'])
        ok('走了 guess_fields', r['action'] == 'guess_fields', str(r['action']))
        ok('标记为已自愈', r['repaired'] is True)
        st = LS.load_state()
        ok('猜字段的结论已落盘', st.get('guess_fields') is True, str(st.get('guess_fields')))
        les = st.get('lessons') or []
        ok('经验表记录了成功', bool(les) and les[0]['action'] == 'guess_fields'
           and les[0]['ok'] is True, str(les[:1]))
        ok('复测请求确实带上了候选字段',
           'turnstileToken' in (up.bodies[-1] if up.bodies else {}),
           str(len(up.bodies[-1]) if up.bodies else 0) + ' 个字段')


def t_schema_from_message():
    """报错里明说了字段名：先学名再重放，也要能过。"""
    print('\n[2] 从报错原文里学字段名（learn_fields）')
    seen = {'n': 0}

    def answer(body):
        seen['n'] += 1
        if 'marker' in body:
            return 'https://ok'
        raise gate_err(LS.G_SCHEMA, "body must have required property 'marker'")

    up = FakeUpstream([answer])
    with Ctx(up):
        learned = LS.learn_fields("body must have required property 'marker'")
        ok('learn_fields 解析出字段名', learned == ['marker'], str(learned))
        h = mk()
        r = h.run_once()
        ok('重放后 open', r['ok'], r['gate'])


def t_cf_cookie():
    """Cloudflare 挡了 public 层：cookie 层接管。"""
    print('\n[3] Cloudflare 挑战 -> cookie 层接管')
    up = FakeUpstream([gate_err(LS.G_CF, 'Just a moment...')])
    with Ctx(up, cookie='https://auth.platorelay.com/a?d=cookie'):
        h = mk()
        r = h.run_once()
        ok('cookie 层救回', r['ok'] and r['action'] == 'refresh_cookie',
           f'{r["gate"]}/{r["action"]}')
        ok('标明是走别的层修好的', r.get('via') == 'refresh_cookie', str(r.get('via')))


def t_offline_switch():
    """网络不通：试两种出网方式，全不通就明确报失败、不挂住。"""
    print('\n[4] 网络不通 -> 试切换出网，全不通也不抛')
    up = FakeUpstream([gate_err(LS.G_OFFLINE, 'ConnectTimeout')])
    with Ctx(up):
        h = mk()
        t0 = time.time()
        r = h.run_once()
        dt = time.time() - t0
        ok('结论是失败而非异常', r['ok'] is False, r['gate'])
        ok('没有卡住（<5s）', dt < 5, f'{dt:.2f}s')
        ok('给了可读的原因', bool(r.get('advice')), str(r.get('advice'))[:40])


def t_rate_backoff():
    """被限流：退避而不是死敲。"""
    print('\n[5] 429 限流 -> 自动退避')
    up = FakeUpstream([gate_err(LS.G_RATE, 'too many requests')])
    with Ctx(up):
        h = mk(interval=10)
        before = h.interval
        r = h.run_once()
        ok('判定为限流', r['gate'] == LS.G_RATE, r['gate'])
        ok('间隔被拉长', h.interval > before, f'{before} -> {h.interval}')
        ok('不是当成修好了', r['repaired'] is False)


def t_cooldown():
    """同一失败 60s 内不重复修：别把探测变成压测。"""
    print('\n[6] 冷却：同类失败不重复打上游')
    up = FakeUpstream([gate_err(LS.G_OFFLINE, 'timeout')])
    with Ctx(up):
        h = mk(min_repair_gap=600)
        h.run_once()
        n1 = up.n
        h.run_once()
        ok('第二轮没有再发修复请求', up.n == n1 + 1, f'调用 {n1} -> {up.n}')
        ok('标记为跳过', h.stats['skipped'] >= 1, str(h.stats))


def t_wake():
    """求解链路报错时能被叫醒，而不是等下一个轮询周期。"""
    print('\n[7] 事件唤醒（求解失败立刻处理）')
    up = FakeUpstream(['https://auth.platorelay.com/a?d=wake'])
    with Ctx(up):
        h = mk()
        h.note_failure(LS.G_SCHEMA, 'solver saw schema error')
        r = h.run_once()
        ok('确实被唤醒处理了', r['source'] == 'wake', str(r['source']))
        ok('统计里记了唤醒', h.stats['woken'] == 1, str(h.stats))


def t_captcha():
    """加了 Turnstile：先挖 sitekey，挖到即认为能接 captcha 层。"""
    print('\n[8] Turnstile -> 挖 sitekey')
    sk = '0x4AAAAAAABkMYinukE8nzY'
    up = FakeUpstream([gate_err(LS.G_CAPTCHA, 'turnstile token required')])
    with Ctx(up, sitekey=sk):
        h = mk()
        r = h.run_once()
        tried = [t['action'] for t in (r.get('tried') or
                                       (LS.load_state().get('heal') or {}).get('tried') or [])]
        ok('尝试过 captcha_solve 与 hunt_sitekey',
           'hunt_sitekey' in tried, str(tried))
        ok('sitekey 落盘', LS.known_sitekey() == sk, LS.known_sitekey() or '(空)')
        ok('没拿到链接就不算修好', r['repaired'] is False and r['ok'] is False)


def t_auth_cred():
    """上游要凭据但没配：必须说清缺什么，不能含糊过去。"""
    print('\n[9] 要凭据但未配置 -> 明确说缺什么')
    for k in ('DELTA_API_KEY', 'DELTA_API_TOKEN', 'DELTA_API_COOKIE'):
        os.environ.pop(k, None)
    up = FakeUpstream([gate_err(LS.G_AUTH, 'unauthorized')])
    with Ctx(up):
        h = mk()
        r = h.run_once()
        ok('没修好也如实报', r['ok'] is False)
        les = LS.load_state().get('lessons') or []
        notes = ' '.join(l.get('note') or '' for l in les)
        ok('提示里点了缺失的变量名', 'DELTA_API_KEY' in notes, notes[:80])


def t_order_learning():
    """连续失败的层自动往后排（链路自学习）。"""
    print('\n[10] 连续失败的层自动降权')
    with Ctx(FakeUpstream(['x'])):
        LS._record('cookie', False, gate=LS.G_CF)
        LS._record('cookie', False, gate=LS.G_CF)
        LS._record('cookie', False, gate=LS.G_CF)
        order = [p.name for p in LS._ordered()]
        ok('cookie 被排到 public 之后',
           order.index('cookie') > order.index('public'), str(order))
        ok('没有把 cookie 踢出链条', 'cookie' in order)
        LS._record('cookie', True, gate=LS.G_OPEN)
        ok('成功一次即复位', LS._penalty('cookie') == 0)


def t_snapshot():
    """健康检查快照必须可序列化、且凭据不外泄。"""
    print('\n[11] /healthz 快照')
    up = FakeUpstream(['https://x'])
    with Ctx(up):
        h = mk()
        h.run_once()
        snap = h.snapshot()
        import json
        try:
            json.dumps(snap, ensure_ascii=False)
            serializable = True
        except Exception:
            serializable = False
        ok('快照可 JSON 序列化', serializable)
        ok('含 running/auto/stats', all(k in snap for k in
                                       ('running', 'auto', 'stats', 'lessons')))
        ok('未运行时 running=False', snap['running'] is False)


def t_thread_daemon():
    """线程必须是 daemon，否则 systemctl stop 之后进程退不掉。"""
    print('\n[12] 后台线程是 daemon')
    up = FakeUpstream(['https://x'])
    with Ctx(up):
        h = mk(interval=3600)
        h.start()
        try:
            time.sleep(0.6)
            t = h._thread
            ok('线程已起', t is not None and t.is_alive())
            ok('是 daemon', bool(t and t.daemon))
            ok('running() 为真', h.running() is True)
        finally:
            h.stop()
        ok('stop() 后退出', h.running() is False)


# ---------------------------------------------------------------- main

def main():
    print('=' * 68)
    print('自愈引擎打桩自检（全程不连上游）')
    print('=' * 68)
    for fn in (t_schema_learn, t_schema_from_message, t_cf_cookie,
               t_offline_switch, t_rate_backoff, t_cooldown, t_wake,
               t_captcha, t_auth_cred, t_order_learning, t_snapshot,
               t_thread_daemon):
        try:
            fn()
        except Exception as e:
            FAIL.append(fn.__name__)
            print(f'  [FAIL] {fn.__name__} 抛异常: {type(e).__name__}: {e}')

    print('\n' + '=' * 68)
    print(f'结果: {len(PASS)} 通过 / {len(FAIL)} 失败')
    if FAIL:
        for f in FAIL:
            print('  失败:', f)
        return 1
    print('全部通过')
    return 0


if __name__ == '__main__':
    sys.exit(main())
