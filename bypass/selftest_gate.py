#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""门禁退路的打桩自检。

上游什么时候加门禁、加哪一种，没法预知，也不能等它真加了才知道代码对不对。
这里把各种门禁形态伪造出来，逐个验证：

  1 正常        200 + url                        -> public 层出链接
  2 CF 挑战     403 + "Just a moment..."         -> 判 G_CF，各层接不住时明确报错
  3 要凭据      401 + unauthorized               -> 判 G_AUTH
  4 缺字段      200 + required property 'xxx'    -> 学到字段名，重试自动带上并成功
  5 要验证码    200 + "invalid turnstile token"  -> 判 G_CAPTCHA
  6 限流        429                              -> 判 G_RATE
  7 sitekey     403 body 内含 0x4A...            -> 自动记住 sitekey
  8 外部队列    文件里有 3 条                     -> manual 层依次消费，空时报错

跑法：
    python selftest_gate.py
    python selftest_gate.py -v        # 打印每一层的尝试过程

退出码 0 = 全部符合预期。
"""
import io
import json
import os
import sys
import tempfile

import link_source as LS

OK = '\033[32mPASS\033[0m' if sys.stdout.isatty() else 'PASS'
NG = '\033[31mFAIL\033[0m' if sys.stdout.isatty() else 'FAIL'

_results = []


class FakeResp(object):
    def __init__(self, status, text, json_obj=None, ctype='application/json'):
        self.status_code = status
        self.text = text
        self.content = (text or '').encode()
        self.headers = {'content-type': ctype}
        self._json = json_obj
        self.cookies = []

    def json(self):
        if self._json is None:
            raise ValueError('no json')
        return self._json

    def __getattr__(self, _):
        raise AttributeError


class FakeSession(object):
    """按 scenario 的响应序列作答。序列用完后重复最后一个。"""

    def __init__(self, pub_seq, page=None, bundle=None):
        self.pub_seq = list(pub_seq)
        self.page = page
        self.bundle = bundle
        self.n_post = 0
        self.calls = []
        self.cookies = []          # 模拟 requests.Session 的 cookie jar
        self.headers = {}          # 模拟 Session.headers（cookie 层会 update 它）

    def post(self, url, **kw):
        self.n_post += 1
        self.calls.append(('POST', url, kw.get('json')))
        i = min(self.n_post - 1, len(self.pub_seq) - 1)
        return self.pub_seq[i]

    def get(self, url, **kw):
        self.calls.append(('GET', url, None))
        if 'auth' in url or url.rstrip('/').endswith(LS.AUTH_HOST):
            r = self.page or FakeResp(200, '<html></html>', None, 'text/html')
        else:
            r = self.bundle or FakeResp(200, 'console.log(1)', None,
                                        'application/javascript')
        # 真实 requests 会把响应 cookie 并进 jar；cookie 层依赖这一点
        for c in (getattr(r, 'cookies', None) or []):
            self.cookies.append(c)
        return r


class Ctx(object):
    """临时替换 state 文件与 HTTP 层，退出时还原。"""

    def __init__(self, scenario_session, retries='1'):
        self.sess = scenario_session
        self.retries = retries

    def __enter__(self):
        self._old_http = LS._http
        self._old_new_session = LS._new_session
        self._old_state = LS.STATE_FILE
        self._old_retries = os.environ.get('DELTA_LINK_RETRIES')
        self._old_key = os.environ.get('DELTA_API_KEY')

        d = tempfile.mkdtemp(prefix='gateselftest_')
        LS.STATE_FILE = os.path.join(d, 'state.json')
        os.environ['DELTA_LINK_RETRIES'] = self.retries
        # 清掉可能干扰的环境变量
        for k in ('DELTA_API_KEY', 'DELTA_API_TOKEN', 'DELTA_API_COOKIE',
                  'CAPSOLVER_KEY', 'TWOCAPTCHA_KEY', 'DELTA_TURNSTILE_SITEKEY',
                  'DELTA_API_HOSTS', 'DELTA_LINK_INBOX', 'DELTA_LINK_FEED'):
            os.environ.pop(k, None)
        LS._http = lambda: self.sess
        LS._new_session = lambda: self.sess
        return self

    def __exit__(self, *a):
        LS._http = self._old_http
        LS._new_session = self._old_new_session
        LS.STATE_FILE = self._old_state
        if self._old_retries is None:
            os.environ.pop('DELTA_LINK_RETRIES', None)
        else:
            os.environ['DELTA_LINK_RETRIES'] = self._old_retries
        if self._old_key is not None:
            os.environ['DELTA_API_KEY'] = self._old_key
        return False


def check(name, cond, detail=''):
    _results.append((name, bool(cond), detail))
    print(f'  [{OK if cond else NG}] {name}' + (f'  {detail}' if detail else ''))
    return bool(cond)


def ok_resp(url='https://auth.platorelay.com/a?d=fake_ticket_aaaaaaaaaaaa'):
    return FakeResp(200, json.dumps({'success': True, 'data': {'url': url}}),
                    {'success': True, 'data': {'url': url}})


def fail_resp(status, message, raw=None):
    body = raw if raw is not None else json.dumps({'success': False, 'message': message})
    jo = None if raw is not None else {'success': False, 'message': message}
    return FakeResp(status, body, jo,
                    'text/html' if raw is not None else 'application/json')


# ---------------------------------------------------------------- 场景

def s1_open():
    print('\n[1] 正常：200 + url')
    with Ctx(FakeSession([ok_resp()])):
        try:
            u = LS.fetch_url(6, 'id1', verbose=False)
            check('public 层返回链接', u.startswith('https://'))
            check('state.gate == open', LS.load_state().get('gate') == LS.G_OPEN,
                  str(LS.load_state().get('gate')))
        except Exception as e:
            check('public 层返回链接', False, f'{type(e).__name__}: {e}')


def s2_cf():
    print('\n[2] Cloudflare 交互挑战：403 + Just a moment')
    page = FakeResp(403, '<html><title>Just a moment...</title>'
                         '<div id="cf-chl-widget"></div></html>', None, 'text/html')
    pub = fail_resp(403, 'challenge', raw='<html>Just a moment...</html>')
    with Ctx(FakeSession([pub], page=page)):
        try:
            LS.fetch_url(6, 'id2')
            check('应该抛 GateError', False, '居然成功了')
        except LS.GateError as e:
            check('判为 cf-challenge', e.gate == LS.G_CF, e.gate)
            check('错误信息可读', '所有链接通道都失败' in str(e) or 'CF' in str(e),
                  str(e)[:70])


def s3_auth():
    print('\n[3] 要凭据：401 unauthorized')
    pub = fail_resp(401, 'unauthorized')
    with Ctx(FakeSession([pub])):
        try:
            LS.fetch_url(6, 'id3')
            check('应该抛 GateError', False)
        except LS.GateError as e:
            check('判为 auth-required', e.gate == LS.G_AUTH, e.gate)


def s4_schema():
    print('\n[4] 缺字段：required property \'turnstileToken\'（学字段 -> 重试带上 -> 成功）')
    seq = [fail_resp(200, "body must have required property 'turnstileToken'"),
           ok_resp()]
    sess = FakeSession(seq)
    with Ctx(sess, retries='3'):
        try:
            u = LS.fetch_url(6, 'id4')
            check('学到字段后重试成功', u.startswith('https://'))
            check('learned_fields 含 turnstileToken',
                  'turnstileToken' in (LS.load_state().get('learned_fields') or []),
                  str(LS.load_state().get('learned_fields')))
            second_body = [c[2] for c in sess.calls if c[0] == 'POST'][-1] or {}
            check('重试请求体已带上该字段', 'turnstileToken' in second_body,
                  str(list(second_body.keys()))[:80])
        except Exception as e:
            check('学到字段后重试成功', False, f'{type(e).__name__}: {e}')


def s5_captcha():
    print('\n[5] 要验证码：invalid turnstile token')
    pub = fail_resp(200, 'invalid turnstile token, please verify you are human')
    with Ctx(FakeSession([pub])):
        try:
            LS.fetch_url(6, 'id5')
            check('应该抛 GateError', False)
        except LS.GateError as e:
            check('判为 captcha-required', e.gate == LS.G_CAPTCHA, e.gate)


def s6_rate():
    print('\n[6] 限流：429')
    pub = FakeResp(429, json.dumps({'success': False, 'message': 'too many requests'}),
                   {'success': False, 'message': 'too many requests'})
    with Ctx(FakeSession([pub])):
        try:
            LS.fetch_url(6, 'id6')
            check('应该抛 GateError', False)
        except LS.GateError as e:
            check('判为 rate-limited', e.gate == LS.G_RATE, e.gate)


def s7_sitekey():
    print('\n[7] 响应里带 Turnstile sitekey -> 自动记住')
    sk = '0x4AAAAAAABkMYinukE8nzY'
    raw = (f'<html>verify <div class="cf-turnstile" data-sitekey="{sk}"></div>'
           f'</html>')
    pub = FakeResp(403, raw, None, 'text/html')
    with Ctx(FakeSession([pub])):
        try:
            LS.fetch_url(6, 'id7')
        except LS.GateError:
            pass
        got = LS.load_state().get('sitekey')
        check('自动记录了 sitekey', got == sk, str(got))


def s8_manual():
    print('\n[8] 外部队列：文件 3 条 -> 依次消费 -> 空时报错')
    d = tempfile.mkdtemp(prefix='inbox_')
    inbox = os.path.join(d, 'inbox.txt')
    with open(inbox, 'w', encoding='utf-8') as f:
        f.write('# 注释行\n'
                'https://auth.platorelay.com/a?d=aaa\n'
                'bbb\n'
                'https://auth.platorelay.com/a?d=ccc\n')
    os.environ['DELTA_LINK_INBOX'] = inbox
    # 让 public/cookie/mirror/captcha/browser 都不可用，直奔 manual
    with Ctx(FakeSession([fail_resp(401, 'unauthorized')])) as _c:
        os.environ['DELTA_LINK_INBOX'] = inbox    # Ctx 会清 env，这里补回来
        try:
            u1 = LS.fetch_url(6, 'x8', only='manual')
            u2 = LS.fetch_url(6, 'x8', only='manual')
            u3 = LS.fetch_url(6, 'x8', only='manual')
            check('第1条', u1.endswith('d=aaa'), u1[-12:])
            check('第2条补全为 URL', 'd=bbb' in u2, u2[-14:])
            check('第3条', u3.endswith('d=ccc'), u3[-12:])
            left = open(inbox, encoding='utf-8').read().strip()
            check('队列已清空', left == '' or left.startswith('#'), repr(left)[:40])
            try:
                LS.fetch_url(6, 'x8', only='manual')
                check('空队列应该报错', False)
            except LS.GateError as e:
                check('空队列明确报错', '空' in str(e) or '没有可用' in str(e),
                      str(e)[:60])
        finally:
            os.environ.pop('DELTA_LINK_INBOX', None)


def s9_fallback_chain():
    print('\n[9] 链路降级：public 403 -> cookie 成功')
    # 第一次 POST 403，第二次（cookie 层）成功
    page = FakeResp(200, '<html>ok</html>', None, 'text/html')
    page.cookies = []
    sess = FakeSession([fail_resp(403, 'forbidden', raw='<html>no</html>'), ok_resp()],
                       page=page)
    # cookie 层需要 auth 页面下发 cookie，否则会主动放弃
    class WithCookie(FakeResp):
        def __init__(self):
            FakeResp.__init__(self, 200, '<html>ok</html>', None, 'text/html')
            self.cookies = [type('C', (), {'name': 'cf_clearance', 'value': 'x'})()]
    sess.page = WithCookie()
    with Ctx(sess, retries='1'):
        try:
            u = LS.fetch_url(6, 'id9', only='public,cookie')
            check('cookie 层接管成功', u.startswith('https://'), u[:40])
            st = LS.load_state()
            check('last_ok 记录的是 cookie', (st.get('last_ok') or {}).get('provider') == 'cookie',
                  str(st.get('last_ok')))
        except Exception as e:
            check('cookie 层接管成功', False, f'{type(e).__name__}: {e}')


def main():
    print('=' * 66)
    print('门禁退路自检（全部为打桩，不访问真实上游）')
    print('=' * 66)
    s1_open()
    s2_cf()
    s3_auth()
    s4_schema()
    s5_captcha()
    s6_rate()
    s7_sitekey()
    s8_manual()
    s9_fallback_chain()

    total = len(_results)
    passed = sum(1 for _, ok, _ in _results if ok)
    print('\n' + '=' * 66)
    print(f'结果: {passed}/{total} 通过')
    bad = [r for r in _results if not r[1]]
    if bad:
        print('失败项:')
        for n, _, d in bad:
            print(f'  - {n}  {d}')
    print('=' * 66)
    return 0 if passed == total else 1


if __name__ == '__main__':
    sys.exit(main())
