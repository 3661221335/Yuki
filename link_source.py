#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""链接获取的多层退路（provider 链）。

为什么要这一层
--------------
上游随时可能给 `/public/start` 加门禁：API key、Turnstile、IP 白名单，
或者干脆要求 Discord OAuth 才发链接。这类改动一次就能让整条链路突然全断，
而且不会提前通知 —— 之前 `outdated client` 那次就是同一个模式（对方收紧
版本协商，我们全线报错）。

所以「拿一条链接」这件事不该只有一个实现。这里把它拆成可插拔的 provider，
按成本从低到高排成一条链，任一成功就返回，同时把上游当时的状态（Gate）
记录下来。上游真的加了门禁时，不需要改代码重发版：要么已有 provider 自动
接管，要么补一个 provider，其余照常工作。

链条（默认顺序，cost 越小越先试）
--------------------------------
  1 public   无凭据 POST /public/start        —— 现在可用，成本最低
  2 cookie   先拿 cf_clearance 再 POST        —— 上游只要 cookie 会话时接管
  3 mirror   换 API host（备用域名/IP）        —— 主域名被区域策略拦时接管
  4 captcha  打码拿 Turnstile token 注入       —— 上游加 Turnstile 时接管
  5 browser  真实浏览器里取链接               —— 上面全挂时的重装方案
  6 manual   外部供应（文件/HTTP 队列）        —— 最后的兜底：拿别人的链接

字段自适应
----------
已验证：`/public/start` 会忽略多余字段（带 captcha/turnstileToken/cfToken 都照常
返回 200）。所以 token 可以「全都塞进去」，不需要提前知道字段名。

更关键的是：上游是 NestJS 风格的校验，缺字段会报
`body must have required property 'xxx'`。我们从这个报错里**学**出字段名，
存进 state，下次直接带上。也就是说它加什么字段，我们自己就学会了，
不需要等人去改代码。

用法
----
    import link_source

    url = link_source.fetch_url(service=6)      # 自动挑能用的 provider
    url = link_source.fetch_url(only='public')  # 只用指定的一层

    print(link_source.diagnose())               # 当前卡在哪一层
"""
import json
import os
import random
import re
import sys
import time

try:
    import requests
except ImportError:                                     # pragma: no cover
    requests = None

try:
    import fcntl
except ImportError:                                      # Windows
    fcntl = None

HERE = os.path.dirname(os.path.abspath(__file__))

API_HOST = (os.environ.get('DELTA_API_HOST') or 'api.platoboost.net').strip()
AUTH_HOST = (os.environ.get('DELTA_AUTH_HOST') or 'auth.platorelay.com').strip()
CLIENT_UA = 'Platoboost Python Client/1.0'

# 打码平台：两个都支持，配哪个用哪个
CAPSOLVER_KEY = (os.environ.get('CAPSOLVER_KEY') or '').strip()
TWOCAPTCHA_KEY = (os.environ.get('TWOCAPTCHA_KEY') or '').strip()
# Turnstile 的 sitekey 拿不到时可以用环境变量显式指定（诊断会提示怎么取）
TURNSTILE_SITEKEY = (os.environ.get('DELTA_TURNSTILE_SITEKEY') or '').strip()
# 浏览器通道用哪个 channel（msedge / chrome / chromium）
BROWSER_CHANNEL = (os.environ.get('DELTA_BROWSER_CHANNEL') or 'msedge').strip()

STATE_FILE = (os.environ.get('DELTA_LINK_STATE')
              or os.path.join(HERE, '.link_source_state.json'))

# 候选字段名：上游加 Turnstile 时用到哪个都不确定，那就全带上。
# 已验证多余字段会被忽略，所以这里多带没有副作用。
FIELD_CANDIDATES = ('captcha', 'turnstileToken', 'cfToken', 'turnstile_token',
                    'token', 'recaptchaToken', 'captchaToken')

# 门禁状态。诊断和日志都用这几个词，便于 grep。
G_OPEN = 'open'                     # 正常发链接
G_CF = 'cf-challenge'               # Cloudflare 交互挑战（403/503 + 挑战页）
G_AUTH = 'auth-required'            # 401/403 且提示 token/key/unauthorized
G_SCHEMA = 'schema'                 # 缺字段（可学习，通常不是真门禁）
G_CAPTCHA = 'captcha-required'      # 明确要求验证码 token
G_RATE = 'rate-limited'             # 429 / too many requests
G_OFFLINE = 'offline'               # 网络层不通（DNS/TCP/TLS）
G_UNKNOWN = 'unknown'

GATE_HUMAN = {
    G_OPEN: '正常（无门禁）',
    G_CF: 'Cloudflare 交互挑战',
    G_AUTH: '需要凭据（token / API key）',
    G_SCHEMA: '字段校验未通过',
    G_CAPTCHA: '需要验证码 token（Turnstile / reCAPTCHA）',
    G_RATE: '被限流',
    G_OFFLINE: '网络不通',
    G_UNKNOWN: '未知状态',
}


class GateError(RuntimeError):
    """某个 provider 失败，附带上游状态，方便上层决定要不要换一层。"""

    def __init__(self, gate, message, provider='', status=None):
        super().__init__(message)
        self.gate = gate
        self.provider = provider
        self.status = status


# ---------------------------------------------------------------- 状态持久化

def _state_default():
    return {'gate': None, 'learned_fields': [], 'providers': {},
            'last_ok': None, 'updated': None, 'sitekey': None,
            # 下面三项给自愈引擎（selfheal.py）用，存在同一个文件里：
            # 服务器只跑一个进程，多一个状态文件就多一处要备份/清理的东西。
            'lessons': [],        # 失败->动作->结果的实验结果表
            'heal': {},           # 最近一次自愈的动作与结论
            'egress_best': None,  # 试出来的最优出网方式
            # 猜出来的字段组合（用空串填充）。和 learned_fields 分开记：
            # learned 是上游明说"这是必填字段"，可以用 null 发；
            # guess 是"不知道名字，全塞一遍空串试出来的"，必须继续用空串发。
            'guess_fields': False,
            # 下面几项给 fastlane / 求解链路用（同一个 state 文件，省一处备份）：
            'step_gap': None,        # 自学习出来的最优步间隔（秒）
            'step_gap_hits': 0,      # 间隔被调整过多少次
            'probe_ticket': None,    # 最近一次真实求解用的票据，自愈拿它探求解链路
            'probe_ticket_at': None,
            'solve_stats': {},       # 成功/失败/耗时分布（滑动窗口摘要）
            }


def load_state():
    try:
        with open(STATE_FILE, encoding='utf-8') as f:
            data = json.load(f)
        base = _state_default()
        if isinstance(data, dict):
            base.update({k: v for k, v in data.items() if k in base})
        return base
    except Exception:
        return _state_default()


def save_state(state, merge_fields=True):
    """带文件锁的原子写。多进程同时求解时不会互相覆盖。

    merge_fields: learned_fields 是否取并集。默认 True（多进程各学各的，
                  不能互相抹）。要真正清空请传 False —— 见 forget_fields()。
    """
    tmp = f'{STATE_FILE}.tmp.{os.getpid()}'
    lf = None
    try:
        d = os.path.dirname(os.path.abspath(STATE_FILE)) or '.'
        os.makedirs(d, exist_ok=True)
        lf = open(STATE_FILE + '.lock', 'a+')
        if fcntl is not None:
            fcntl.flock(lf.fileno(), fcntl.LOCK_EX)
        merged = load_state()
        if merge_fields:
            fields = list(dict.fromkeys(
                list(merged.get('learned_fields') or []) +
                list(state.get('learned_fields') or [])))
        else:
            fields = list(state.get('learned_fields') or [])
        merged.update(state)
        merged['learned_fields'] = fields
        merged['updated'] = time.time()
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(merged, f, ensure_ascii=False, separators=(',', ':'))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, STATE_FILE)
    except Exception:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except Exception:
            pass
    finally:
        if lf is not None:
            try:
                if fcntl is not None:
                    fcntl.flock(lf.fileno(), fcntl.LOCK_UN)
                lf.close()
            except Exception:
                pass


def _record(provider, ok, gate=None, detail=None, ms=None):
    st = load_state()
    p = st.setdefault('providers', {}).setdefault(
        provider, {'ok': 0, 'fail': 0, 'last_ms': None, 'last_gate': None})
    if ok:
        p['ok'] = int(p.get('ok') or 0) + 1
        # streak 是给 _ordered 排序用的：连续失败的层自动后退，成功一次即复位。
        # 只数连续次数，不用指数衰减 —— 状态文件要人能读懂。
        p['ok_streak'] = int(p.get('ok_streak') or 0) + 1
        p['fail_streak'] = 0
        if ms is not None:
            p['last_ms'] = round(ms, 3)
        st['last_ok'] = {'provider': provider, 'ts': time.time(), 'ms': ms and round(ms, 3)}
        if gate:
            st['gate'] = gate
    else:
        p['fail'] = int(p.get('fail') or 0) + 1
        p['fail_streak'] = int(p.get('fail_streak') or 0) + 1
        p['ok_streak'] = 0
        if gate:
            p['last_gate'] = gate
            st['gate'] = gate
    if detail:
        p['last_detail'] = str(detail)[:200]
    save_state(st)


def learn_fields(message):
    """从上游报错里学出必填字段名。

    NestJS 校验器会说 `body must have required property 'fieldName'`。
    上游加门禁时最可能就是加一个必填字段，这句话直接告诉我们叫什么。
    """
    if not message:
        return []
    found = re.findall(r"required property '([A-Za-z_][\w-]*)'", str(message))
    found += re.findall(r'required property "([A-Za-z_][\w-]*)"', str(message))
    found = [f for f in dict.fromkeys(found) if f not in ('service', 'identifier')]
    if not found:
        return []
    st = load_state()
    merged = list(dict.fromkeys(list(st.get('learned_fields') or []) + found))
    st['learned_fields'] = merged
    save_state(st)
    return found


# ---------------------------------------------------------------- 经验表
#
# "自己学习失败"这件事要能落地，靠的不是把报错翻译成人话，而是把
# 【失败类型 -> 试过的动作 -> 结果】记下来，下次遇到同一个失败先试上次有效的。
# 这就是一张很小的实验记录表，存在 state 里（服务器只跑一个进程，不做数据库）。

LESSON_MAX = 40


def record_lesson(gate, action, ok, note=''):
    st = load_state()
    ls = list(st.get('lessons') or [])
    ls.insert(0, {'gate': gate, 'action': action, 'ok': bool(ok),
                  'note': str(note)[:160], 'ts': time.time()})
    st['lessons'] = ls[:LESSON_MAX]
    save_state(st)


def best_action(gate):
    """上次对这个失败类型有效的动作。没有就返回 None。"""
    for l in load_state().get('lessons') or []:
        if l.get('gate') == gate and l.get('ok'):
            return l.get('action')
    return None


def forget_fields():
    """清空学到的字段名和猜出来的组合。

    什么时候用：上游把字段改名/去掉了，或者当初学错了（把某个业务字段
    当成门禁字段，导致每次请求都多带一个 null）。不清的话它会一直跟着，
    表现是"明明上游没要求，却总是在报另一个字段错"。
    """
    st = load_state()
    old = list(st.get('learned_fields') or [])
    st['learned_fields'] = []
    st['guess_fields'] = False
    save_state(st, merge_fields=False)      # 不取并集，否则删不掉
    return old


def lesson_summary(n=6):
    return [{'gate': l.get('gate'), 'action': l.get('action'),
             'ok': l.get('ok'), 'note': l.get('note'),
             'ago': round(time.time() - float(l.get('ts') or 0), 1)}
            for l in (load_state().get('lessons') or [])[:n]]


# Cloudflare Turnstile 的 sitekey 是固定前缀 0x4A + 长串。
# 上游一旦启用 Turnstile，必须把这个值交给调用方渲染组件，所以它一定会
# 出现在某个响应里（页面 HTML / 403 响应 body / config 接口）。
# 与其等人去挖，不如每次响应都顺手扫一遍，扫到就记住。
SITEKEY_RE = re.compile(r'0x4[A-Za-z0-9_-]{15,}')


def learn_sitekey(text):
    if not text:
        return None
    m = SITEKEY_RE.search(str(text))
    if not m:
        return None
    sk = m.group(0)
    st = load_state()
    if st.get('sitekey') != sk:
        st['sitekey'] = sk
        save_state(st)
    return sk


def known_sitekey():
    """优先用环境变量（人工确认过），其次用自学习到的。"""
    if TURNSTILE_SITEKEY:
        return TURNSTILE_SITEKEY
    return load_state().get('sitekey') or ''


def hunt_sitekey(verbose=False):
    """把 Turnstile sitekey 从三个地方挖出来：页面 HTML -> JS bundle -> 真实响应。

    上游启用 Turnstile 时，sitekey 必须交给前端渲染，所以必然出现在其中一处。
    找到就写进 state，captcha 层直接用。返回 '' 表示没找到。
    """
    found = known_sitekey()
    if found:
        if verbose:
            print(f'[+] 已有 sitekey（环境变量或历史学习）: {found}')
        return found

    s = _http()
    html = ''
    try:
        r = s.get(f'https://{AUTH_HOST}/', timeout=(8, 20),
                  headers={'User-Agent': ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                                          'AppleWebKit/537.36 (KHTML, like Gecko) '
                                          'Chrome/131.0.0.0 Safari/537.36'),
                           'Accept': 'text/html,*/*'})
        html = r.text or ''
        if verbose:
            print(f'[.] 页面 HTML {len(html)} 字符')
        sk = learn_sitekey(html)
        if sk:
            if verbose:
                print(f'[+] 页面 HTML 里有 sitekey: {sk}')
            return sk
    except Exception as e:
        if verbose:
            print(f'[-] 页面取不到: {type(e).__name__}: {str(e)[:80]}')

    srcs = re.findall(r'<script[^>]+src="([^"]+)"', html) or ['/assets/index-121bd418.js']
    for src in srcs[:5]:
        url = src if src.startswith('http') else f'https://{AUTH_HOST}{src}'
        try:
            rr = s.get(url, timeout=(10, 40))
            if verbose:
                print(f'[.] bundle {src[:60]} {len(rr.text)} 字符')
            sk = learn_sitekey(rr.text)
            if sk:
                if verbose:
                    print(f'[+] bundle {src[:40]} 里有 sitekey: {sk}')
                return sk
        except Exception as e:
            if verbose:
                print(f'[-] bundle 取不到 {src[:50]}: {type(e).__name__}')

    # 最后试一次真实请求：上游 403 时可能把 sitekey 放在挑战 body 里
    try:
        PublicProvider().fetch(6, hashlib_hwid())
    except Exception:
        pass
    return known_sitekey()


# ---------------------------------------------------------------- 状态判定

CF_BODY_MARKERS = ('just a moment', 'cf-chl', 'challenge-platform',
                   'attention required', 'enable javascript and cookies')
AUTH_MSG_MARKERS = ('unauthorized', 'invalid api key', 'invalid token',
                    'missing api key', 'authentication', 'forbidden',
                    'access denied', 'not allowed')
CAPTCHA_MSG_MARKERS = ('turnstile', 'captcha', 'verify you are human',
                       'challenge token', 'robot')
RATE_MSG_MARKERS = ('too many', 'rate limit', 'ratelimit', 'slow down', 'throttl')


def classify_response(status, body_text, json_obj=None):
    """把一次响应归类成 Gate。返回 (gate, message)。"""
    learn_sitekey(body_text)        # 顺手看看这次响应里有没有 Turnstile sitekey
    low = (body_text or '')[:2000].lower()
    msg = ''
    if isinstance(json_obj, dict):
        msg = str(json_obj.get('message') or json_obj.get('error') or '')

    if status in (429,):
        return G_RATE, msg or 'HTTP 429'
    if status in (403, 503) or any(m in low for m in CF_BODY_MARKERS):
        if any(m in low for m in CF_BODY_MARKERS):
            return G_CF, msg or 'Cloudflare 挑战页'
        if any(m in (msg or '').lower() for m in AUTH_MSG_MARKERS):
            return G_AUTH, msg or f'HTTP {status}'
        return G_CF, msg or f'HTTP {status}'
    if status in (401,):
        return G_AUTH, msg or 'HTTP 401'
    mlow = (msg or '').lower()
    # 缺字段必须在验证码之前判：报错原文是
    # `body must have required property 'turnstileToken'`，里面带 turnstile，
    # 先判 captcha 会把它误判成"要验证码"，从而错过"学字段名再重试"这条路。
    if 'required property' in mlow or 'required property' in low:
        return G_SCHEMA, msg
    if any(m in mlow for m in CAPTCHA_MSG_MARKERS):
        return G_CAPTCHA, msg
    if any(m in mlow for m in RATE_MSG_MARKERS):
        return G_RATE, msg
    if any(m in mlow for m in AUTH_MSG_MARKERS):
        return G_AUTH, msg
    if status >= 500:
        return G_UNKNOWN, msg or f'HTTP {status}'
    return G_UNKNOWN, msg or f'HTTP {status}'


# 严重度：越大越"值得报"。全链路失败时，最终抛出的 gate 取最严重的那个，
# 这样运维看到的是"上游要凭据"而不是笼统的 unknown。
# offline / unknown 排在负值：它们是"没测出结论"，不该盖过"测出来是门禁"。
SEVERITY = {G_OPEN: 0, G_OFFLINE: -2, G_UNKNOWN: -1, G_SCHEMA: 1,
            G_RATE: 2, G_CF: 3, G_CAPTCHA: 4, G_AUTH: 5}


def gate_severity(gate):
    return SEVERITY.get(gate, 99)


def classify_exception(exc):
    txt = f'{type(exc).__name__}: {exc}'.lower()
    for k in ('timeout', 'timed out', 'connection', 'resolve', 'ssl', 'tls',
              'proxy', 'reset by peer', 'unreachable'):
        if k in txt:
            return G_OFFLINE
    return G_UNKNOWN


# ---------------------------------------------------------------- HTTP 会话

_session = None


def _http():
    global _session
    if _session is None:
        if requests is None:
            raise RuntimeError('缺少 requests：pip install requests')
        s = requests.Session()
        try:
            import auth_client as _a
            s.trust_env = _a._TRUST_ENV     # 与求解链路保持同一种出网方式
        except Exception:
            pass
        s.headers.update({'User-Agent': CLIENT_UA,
                          'Content-Type': 'application/json',
                          'Accept': 'application/json'})
        _session = s
    return _session


def _auth_headers():
    """上游若要求凭据，用环境变量提供，四种常见形态一次带齐。

    不配就是空 dict，行为与现在完全一致。配了之后所有 provider 都会带上，
    不需要为"加 key"单独改代码。
    """
    key = (os.environ.get('DELTA_API_KEY') or '').strip()
    tok = (os.environ.get('DELTA_API_TOKEN') or '').strip()
    h = {}
    if key:
        h['X-API-Key'] = key
        h['X-Api-Key'] = key
        h['Authorization'] = f'Bearer {key}'
    if tok:
        h['Authorization'] = f'Bearer {tok}'
    cookie = (os.environ.get('DELTA_API_COOKIE') or '').strip()
    if cookie:
        h['Cookie'] = cookie
    return h


def _new_session():
    """带独立 cookie jar 的会话，用于需要"先访问页面再请求"的那一层。

    单独抽成函数是为了可注入 —— 自检（selftest_gate.py）需要打桩它，
    否则测 cookie 层就会真去连上游。
    """
    s = requests.Session()
    try:
        import auth_client as _a
        s.trust_env = _a._TRUST_ENV
    except Exception:
        pass
    return s


def _build_body(service, identifier, token=None, guess=False):
    body = {'service': int(service), 'identifier': identifier}
    st = load_state()
    learned = st.get('learned_fields') or []
    # guess_fields 是自愈试出来的结论：上游要的字段名不知道，但"全带空串"
    # 能过。这个结论必须一直生效，否则下次请求又回到只有 service/identifier，
    # 等于白学 —— 上游会重新报缺字段。
    guessed = guess or bool(st.get('guess_fields'))
    if token:
        # 全塞：已验证多余字段会被忽略，省得猜字段名
        for f in dict.fromkeys(list(learned) + list(FIELD_CANDIDATES)):
            body[f] = token
    elif guessed:
        for f in dict.fromkeys(list(learned) + list(FIELD_CANDIDATES)):
            body[f] = ''
    else:
        for f in learned:
            body[f] = None
    return body


def reset_http():
    """丢掉缓存的 requests 会话，下一层会重建（cookie 层需要干净 jar）。"""
    global _session
    _session = None


def probe_start(service=6, extra=None, identifier=None):
    """裸发一次 /public/start，只用于诊断/自愈验证。

    不重试、不落 state —— 调用方（selfheal）要的是"这一发到底通不通"，
    重试会把这个判断搅浑。
    """
    body = _build_body(service, identifier or hashlib_hwid())
    if extra:
        body.update(extra)
    r = _http().post(f'https://{API_HOST}/public/start', json=body,
                     headers=_auth_headers(), timeout=(6, 20))
    return _parse_url(r)


def _parse_url(r):
    """从响应里取链接。成功返回 url，失败抛 GateError。"""
    ctype = (r.headers.get('content-type') or '').lower()
    data = None
    if 'json' in ctype:
        try:
            data = r.json()
        except Exception:
            data = None
    if isinstance(data, dict):
        url = (data.get('data') or {}).get('url') if isinstance(data.get('data'), dict) else None
        if data.get('success') and url:
            return url
        gate, msg = classify_response(r.status_code, r.text, data)
        if gate == G_SCHEMA:
            learn_fields(msg)               # 顺手学一下字段名
        raise GateError(gate, msg or '上游拒绝', status=r.status_code)
    gate, msg = classify_response(r.status_code, r.text, None)
    raise GateError(gate, msg or f'非预期响应 HTTP {r.status_code}', status=r.status_code)


# ---------------------------------------------------------------- providers

class Provider(object):
    name = 'base'
    cost = 9
    note = ''

    def available(self):
        return True

    def why_not(self):
        return ''

    def fetch(self, service, identifier):
        raise NotImplementedError


class PublicProvider(Provider):
    """无凭据直接 POST /public/start。当前主路。

    自带退避重试：网络抖动（G_OFFLINE）值得重来，上游明确拒绝就不浪费次数。
    这比在外层无脑重试整条链更划算 —— 抖动时不会白白跑一遍浏览器层。
    """

    name = 'public'
    cost = 1
    note = '匿名 POST /public/start，无任何凭据'

    def fetch(self, service, identifier):
        retries = int(os.environ.get('DELTA_LINK_RETRIES') or 3)
        retries = max(1, min(retries, 6))
        last = None
        for attempt in range(retries):
            try:
                body = _build_body(service, identifier)
                r = _http().post(f'https://{API_HOST}/public/start', json=body,
                                 headers=_auth_headers(), timeout=(6, 20))
                return _parse_url(r)
            except GateError as e:
                last = e
                # 网络抖动值得重试；G_SCHEMA 也重试一次 —— 报错里已经学到了
                # 必填字段名，下一次请求会带上，多半直接过。
                # 门禁类失败（cf/auth/captcha/rate）重试是同一个结果，不浪费次数。
                if e.gate in (G_OFFLINE, G_UNKNOWN, G_SCHEMA) and attempt < retries - 1:
                    time.sleep(1.0 * (attempt + 1))
                    continue
                raise
            except Exception as e:
                last = GateError(classify_exception(e), str(e))
                if attempt < retries - 1:
                    time.sleep(1.0 * (attempt + 1))
                    continue
                raise last
        raise last or GateError(G_UNKNOWN, 'public 层未产出链接')


class CookieProvider(Provider):
    """先访问 auth 页面拿 cf_clearance，再带着 cookie 发请求。

    只对「上游要求普通 cookie 会话」生效。真遇到 Cloudflare 交互挑战
    （Turnstile/JS challenge）这一步拿不到 clearance，会抛 G_CF 交给下一层。
    """

    name = 'cookie'
    cost = 2
    note = '先取 auth 页面 cookie / cf_clearance，再发请求'

    def fetch(self, service, identifier):
        s = _new_session()
        s.headers.update({
            'User-Agent': ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                           'AppleWebKit/537.36 (KHTML, like Gecko) '
                           'Chrome/131.0.0.0 Safari/537.36'),
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.9',
        })
        try:
            g = s.get(f'https://{AUTH_HOST}/', timeout=(8, 20), allow_redirects=True)
        except Exception as e:
            raise GateError(classify_exception(e), f'auth 页面取 cookie 失败: {e}',
                            status=None)
        if g.status_code in (403, 503):
            gate, msg = classify_response(g.status_code, g.text)
            raise GateError(gate, f'auth 页面 {g.status_code}: {msg}', status=g.status_code)

        jar = {c.name: c.value for c in s.cookies}
        if not jar:
            raise GateError(G_UNKNOWN, 'auth 页面没下发任何 cookie，这一层没有增量',
                            status=g.status_code)

        s.headers.update({'Content-Type': 'application/json',
                          'Accept': 'application/json',
                          'Origin': f'https://{AUTH_HOST}',
                          'Referer': f'https://{AUTH_HOST}/'})
        body = _build_body(service, identifier)
        r = s.post(f'https://{API_HOST}/public/start', json=body,
                   headers=_auth_headers(), timeout=(6, 20))
        return _parse_url(r)


class MirrorProvider(Provider):
    """换 API host 重试。主域名被区域策略拦、或有备用入口时接管。

    DELTA_API_HOSTS=api.platoboost.net,api2.example.com
    """

    name = 'mirror'
    cost = 3
    note = '轮换备用 API host'

    def hosts(self):
        raw = (os.environ.get('DELTA_API_HOSTS') or '').strip()
        cands = [h.strip() for h in raw.split(',') if h.strip()]
        # 主域名放最后：它是 public 那层已经试过的，这里只当"再确认一次"
        cands = [h for h in cands if h != API_HOST]
        return cands

    def available(self):
        return bool(self.hosts())

    def why_not(self):
        return '未配置 DELTA_API_HOSTS'

    def fetch(self, service, identifier):
        errs = []
        for host in self.hosts():
            try:
                body = _build_body(service, identifier)
                r = _http().post(f'https://{host}/public/start', json=body,
                                 headers=_auth_headers(), timeout=(6, 18))
                return _parse_url(r)
            except GateError as e:
                errs.append(f'{host}:{e.gate}')
            except Exception as e:
                errs.append(f'{host}:{classify_exception(e)}')
        raise GateError(G_UNKNOWN, '备用 host 全部失败: ' + ', '.join(errs))


class CaptchaProvider(Provider):
    """打码拿 Turnstile token，注入后请求。

    上游若给 /public/start 挂上 Turnstile，这一层接管。需要：
      CAPSOLVER_KEY 或 TWOCAPTCHA_KEY   打码平台密钥
      DELTA_TURNSTILE_SITEKEY           sitekey（诊断会给提取方法）
    """

    name = 'captcha'
    cost = 4
    note = '打码平台取 Turnstile token 后注入'

    def available(self):
        return bool(CAPSOLVER_KEY or TWOCAPTCHA_KEY)

    def why_not(self):
        return '未配置 CAPSOLVER_KEY / TWOCAPTCHA_KEY'

    # --- CapSolver
    def _capsolver(self, sitekey, page_url):
        s = _http()
        r = s.post('https://api.capsolver.com/createTask', json={
            'clientKey': CAPSOLVER_KEY,
            'task': {
                'type': 'AntiTurnstileTaskProxyLess',
                'websiteURL': page_url,
                'websiteKey': sitekey,
            },
        }, timeout=(8, 25))
        d = r.json()
        if d.get('errorId'):
            raise GateError(G_CAPTCHA, f'CapSolver createTask: {d.get("errorDescription")}')
        task_id = d.get('taskId')
        for _ in range(40):
            time.sleep(2.0)
            rr = s.post('https://api.capsolver.com/getTaskResult', json={
                'clientKey': CAPSOLVER_KEY, 'taskId': task_id}, timeout=(8, 25))
            dd = rr.json()
            if dd.get('status') == 'ready':
                tok = (dd.get('solution') or {}).get('token')
                if tok:
                    return tok
                raise GateError(G_CAPTCHA, 'CapSolver 返回 ready 但没有 token')
            if dd.get('errorId'):
                raise GateError(G_CAPTCHA, f'CapSolver: {dd.get("errorDescription")}')
        raise GateError(G_CAPTCHA, 'CapSolver 超时（80s）')

    # --- 2Captcha
    def _twocaptcha(self, sitekey, page_url):
        s = _http()
        r = s.post('https://2captcha.com/in.php', data={
            'key': TWOCAPTCHA_KEY, 'method': 'turnstile',
            'sitekey': sitekey, 'pageurl': page_url, 'json': 1}, timeout=(8, 25))
        d = r.json()
        if d.get('status') != 1:
            raise GateError(G_CAPTCHA, f'2Captcha in.php: {d.get("request")}')
        rid = d.get('request')
        for _ in range(40):
            time.sleep(2.5)
            rr = s.get('https://2captcha.com/res.php', params={
                'key': TWOCAPTCHA_KEY, 'action': 'get', 'id': rid, 'json': 1},
                timeout=(8, 25))
            dd = rr.json()
            if dd.get('status') == 1:
                return dd.get('request')
            if dd.get('request') not in ('CAPCHA_NOT_READY', 'CAPTCHA_NOT_READY'):
                raise GateError(G_CAPTCHA, f'2Captcha: {dd.get("request")}')
        raise GateError(G_CAPTCHA, '2Captcha 超时（100s）')

    def fetch(self, service, identifier):
        sitekey = known_sitekey()
        if not sitekey:
            raise GateError(
                G_CAPTCHA,
                '拿不到 Turnstile sitekey。上游启用 Turnstile 后它一定出现在某个'
                '响应里，程序会自动记住；也可以手动设 DELTA_TURNSTILE_SITEKEY，'
                '或用 python probe_gate.py --find-sitekey 从页面/接口里扫一遍')
        page_url = f'https://{AUTH_HOST}/'
        token = (self._capsolver(TURNSTILE_SITEKEY, page_url) if CAPSOLVER_KEY
                 else self._twocaptcha(TURNSTILE_SITEKEY, page_url))
        body = _build_body(service, identifier, token=token)
        r = _http().post(f'https://{API_HOST}/public/start', json=body,
                         headers=_auth_headers(), timeout=(6, 22))
        return _parse_url(r)


class BrowserProvider(Provider):
    """在真实浏览器里走一遍页面，抓最终链接或渲染后的 key。

    最重、最慢，但最像人。用于前面几层全挂、且上游已上 OAuth 的情况。
    实际实现委托给 browser_solver（它已有 profile 持久化 + Discord 授权点击）。
    """

    name = 'browser'
    cost = 5
    note = '真实浏览器走页面（会消耗 Discord 授权配额）'

    def available(self):
        try:
            import importlib
            importlib.import_module('playwright')
            return True
        except Exception:
            return False

    def why_not(self):
        return '未安装 playwright（pip install playwright）'

    def fetch(self, service, identifier):
        try:
            import browser_solver
        except Exception as e:
            raise GateError(G_UNKNOWN, f'无法载入 browser_solver: {e}')
        fn = getattr(browser_solver, 'fetch_url_via_browser', None)
        if fn is None:
            raise GateError(G_UNKNOWN,
                            'browser_solver 缺少 fetch_url_via_browser()，'
                            '浏览器通道只能直接出 key，不能出链接')
        url = fn(service=service, channel=BROWSER_CHANNEL)
        if not url:
            raise GateError(G_UNKNOWN, '浏览器通道没有取到链接')
        return url


class ManualProvider(Provider):
    """外部供应：从文件或 HTTP 端点取别人产的链接。

    这是最朴素、也最不会被上游改动影响的退路 —— 上游怎么改，都改不到
    别人手动发出来的链接上。

      DELTA_LINK_INBOX=/path/to/inbox.txt    每行一条 url 或 ticket，取走即删
      DELTA_LINK_FEED=https://.../links       GET 返回文本或 {"urls":[...]}
    """

    name = 'manual'
    cost = 6
    note = '外部供应：文件/HTTP 队列'

    def _inbox(self):
        return (os.environ.get('DELTA_LINK_INBOX') or '').strip()

    def _feed(self):
        return (os.environ.get('DELTA_LINK_FEED') or '').strip()

    def available(self):
        return bool(self._inbox() or self._feed())

    def why_not(self):
        return '未配置 DELTA_LINK_INBOX / DELTA_LINK_FEED'

    def _pop_file(self):
        path = self._inbox()
        lf = None
        try:
            lf = open(path + '.lock', 'a+')
            if fcntl is not None:
                fcntl.flock(lf.fileno(), fcntl.LOCK_EX)
            with open(path, encoding='utf-8') as f:
                lines = [l.strip() for l in f if l.strip() and not l.startswith('#')]
            if not lines:
                return None
            head, rest = lines[0], lines[1:]
            tmp = f'{path}.tmp.{os.getpid()}'
            with open(tmp, 'w', encoding='utf-8') as f:
                f.write('\n'.join(rest) + ('\n' if rest else ''))
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
            return head
        finally:
            if lf is not None:
                try:
                    if fcntl is not None:
                        fcntl.flock(lf.fileno(), fcntl.LOCK_UN)
                    lf.close()
                except Exception:
                    pass

    def _pop_feed(self):
        r = _http().get(self._feed(), timeout=(8, 20))
        txt = (r.text or '').strip()
        try:
            d = r.json()
            if isinstance(d, dict):
                for k in ('urls', 'links', 'data'):
                    v = d.get(k)
                    if isinstance(v, list) and v:
                        return str(v[0])
                for k in ('url', 'link'):
                    if d.get(k):
                        return str(d[k])
            if isinstance(d, list) and d:
                return str(d[0])
        except Exception:
            pass
        for line in txt.splitlines():
            line = line.strip()
            if line and not line.startswith('#'):
                return line
        return None

    def fetch(self, service, identifier):
        if self._inbox():
            u = self._pop_file()
            if u:
                return u if u.startswith('http') else \
                    f'https://{AUTH_HOST}/a?d={u}'
            if not self._feed():
                raise GateError(G_UNKNOWN, f'收件箱为空: {self._inbox()}')
        if self._feed():
            u = self._pop_feed()
            if u:
                return u if u.startswith('http') else \
                    f'https://{AUTH_HOST}/a?d={u}'
            raise GateError(G_UNKNOWN, f'外部 feed 没有可用链接: {self._feed()}')
        raise GateError(G_UNKNOWN, '外部供应未配置')


# 默认链：cost 升序
PROVIDERS = [PublicProvider(), CookieProvider(), MirrorProvider(),
             CaptchaProvider(), BrowserProvider(), ManualProvider()]

ORDER = [p.name for p in PROVIDERS]


def _penalty(name, state=None):
    """连续失败给出的排序惩罚。

    这是"自己学习失败"最直接的一条：某层刚连着失败 3 次，就不该每条链接都
    先在它身上烧一轮超时 —— 自动往后排，等它哪天成功了自然回到前排。
    上限 6，保证它不会被永久踢出链条（上游恢复时还要靠它）。
    """
    if state is None:
        state = load_state()
    p = (state.get('providers') or {}).get(name) or {}
    return min(int(p.get('fail_streak') or 0), 3) * 2


def _ordered(only=None, state=None):
    st = load_state() if state is None else state
    if only:
        ones = [s.strip() for s in str(only).split(',') if s.strip()]
        # 显式点名就按点名的顺序来，不参与自学习重排（调用方有明确意图）
        return [p for p in PROVIDERS if p.name in ones]
    else:
        custom = (os.environ.get('DELTA_LINK_ORDER') or '').strip()
        if custom:
            want = [s.strip() for s in custom.split(',') if s.strip()]
            picked = [p for n in want for p in PROVIDERS if p.name == n]
            picked += [p for p in PROVIDERS if p not in picked]
        else:
            picked = list(PROVIDERS)
    # sorted 是稳定的：权重相同的层保持原定义顺序
    return sorted(picked, key=lambda p: p.cost + _penalty(p.name, st))


# ---------------------------------------------------------------- 对外接口

def fetch_url(service=6, identifier=None, only=None, verbose=False, timeout=None):
    """拿一条 auth 链接。

    依次尝试各层，第一个成功即返回。全失败抛 GateError（带最后一条原因）。
    """
    if identifier is None:
        identifier = hashlib_hwid()

    deadline = (time.time() + timeout) if timeout else None
    errs = []
    best_gate = None
    best_msg = ''
    for p in _ordered(only):
        if deadline and time.time() >= deadline:
            errs.append('timeout')
            break
        if not p.available():
            if verbose:
                print(f'  [link] 跳过 {p.name}（{p.why_not()}）', flush=True)
            continue
        t0 = time.time()
        try:
            url = p.fetch(service, identifier)
            ms = (time.time() - t0) * 1000
            _record(p.name, True, gate=G_OPEN, ms=ms)
            if verbose:
                print(f'  [link] {p.name} 成功 {ms:.0f}ms', flush=True)
            return url
        except GateError as e:
            ms = (time.time() - t0) * 1000
            _record(p.name, False, gate=e.gate, detail=str(e), ms=ms)
            errs.append(f'{p.name}:{e.gate}({str(e)[:60]})')
            if best_gate is None or gate_severity(e.gate) > gate_severity(best_gate):
                best_gate, best_msg = e.gate, str(e)
            if verbose:
                print(f'  [link] {p.name} 失败 [{e.gate}] {str(e)[:80]}', flush=True)
        except Exception as e:
            gate = classify_exception(e)
            _record(p.name, False, gate=gate, detail=str(e))
            errs.append(f'{p.name}:{gate}')
            if best_gate is None or gate_severity(gate) > gate_severity(best_gate):
                best_gate, best_msg = gate, str(e)
            if verbose:
                print(f'  [link] {p.name} 异常 {type(e).__name__}: {str(e)[:70]}',
                      flush=True)
    # 把最有信息量的那个 gate 抛出去：上层据此决定告警等级与建议动作
    raise GateError(best_gate or G_UNKNOWN,
                    ('所有链接通道都失败 -> ' + ' | '.join(errs)) if errs
                    else '没有可用的链接通道（检查环境变量配置）')


def hashlib_hwid():
    import hashlib
    return hashlib.sha256(
        (str(random.randint(0, 2 ** 32)) + str(time.time())).encode()
    ).hexdigest()


def available_providers():
    out = []
    for p in PROVIDERS:
        out.append({'name': p.name, 'available': p.available(),
                    'note': p.note, 'why_not': p.why_not() if not p.available() else ''})
    return out


def diagnose(probe=True, service=6):
    """跑一遍诊断，返回可直接 print 的结构。"""
    st = load_state()
    report = {
        'api_host': API_HOST,
        'auth_host': AUTH_HOST,
        'gate': st.get('gate'),
        'gate_human': GATE_HUMAN.get(st.get('gate') or '', '未探测'),
        'learned_fields': st.get('learned_fields') or [],
        'sitekey': known_sitekey() or None,
        'providers': available_providers(),
        'stats': st.get('providers') or {},
        'last_ok': st.get('last_ok'),
        'probe': None,
    }
    if probe:
        t0 = time.time()
        try:
            url = PublicProvider().fetch(service, hashlib_hwid())
            report['probe'] = {'ok': True, 'ms': round((time.time() - t0) * 1000, 1),
                               'url_head': url[:60] + '...'}
            report['gate'] = G_OPEN
            report['gate_human'] = GATE_HUMAN[G_OPEN]
        except GateError as e:
            report['probe'] = {'ok': False, 'gate': e.gate, 'status': e.status,
                               'msg': str(e)[:200],
                               'ms': round((time.time() - t0) * 1000, 1)}
            report['gate'] = e.gate
            report['gate_human'] = GATE_HUMAN.get(e.gate, e.gate)
        except Exception as e:
            report['probe'] = {'ok': False, 'gate': classify_exception(e),
                               'msg': f'{type(e).__name__}: {e}'[:200],
                               'ms': round((time.time() - t0) * 1000, 1)}
            report['gate'] = report['probe']['gate']
            report['gate_human'] = GATE_HUMAN.get(report['gate'], report['gate'])
    return report


# 建议表：gate -> 该启用哪一层
ADVICE = {
    G_OPEN: '无需动作。这是正常状态。',
    G_CF: ('先试 cookie 层（会自动带 cf_clearance）；仍不行说明是交互挑战，'
           '改用 browser 层或换出口 IP。'),
    G_AUTH: ('上游要凭据了。设 DELTA_API_KEY 之后由 public 层自动带上；'
             '或用 manual 层接外部链接。'),
    G_SCHEMA: ('缺字段。已自动学习字段名，下一次请求会带上；'
               '若报的是验证码字段，启用 captcha 层。'),
    G_CAPTCHA: ('上游加了 Turnstile。设 CAPSOLVER_KEY + DELTA_TURNSTILE_SITEKEY，'
                'captcha 层会接管。'),
    G_RATE: '被限流。降低生成频率，或换出口 IP / 错峰。',
    G_OFFLINE: '网络层不通。先解决出网（DELTA_PROXY / DELTA_NO_PROXY），与门禁无关。',
    G_UNKNOWN: '看 probe.msg 原文；必要时开 verbose 抓一次完整响应。',
}


def main():
    import argparse
    ap = argparse.ArgumentParser(description='链接通道诊断')
    ap.add_argument('--no-probe', action='store_true', help='不实际发请求')
    ap.add_argument('--json', action='store_true', help='输出 JSON')
    ap.add_argument('--fetch', action='store_true', help='真的取一条链接出来')
    ap.add_argument('--service', type=int, default=6)
    a = ap.parse_args()

    if a.fetch:
        try:
            u = fetch_url(service=a.service, verbose=True)
            print('\n[+] ' + u)
            return 0
        except GateError as e:
            print('[-] ' + str(e))
            return 1

    rep = diagnose(probe=not a.no_probe, service=a.service)
    if a.json:
        print(json.dumps(rep, ensure_ascii=False, indent=2))
        return 0

    print('=' * 64)
    print('链接通道诊断')
    print('=' * 64)
    print(f'API  {rep["api_host"]}')
    print(f'AUTH {rep["auth_host"]}')
    print(f'\n当前门禁状态: {rep["gate"]}  ({rep["gate_human"]})')
    pr = rep['probe']
    if pr:
        if pr.get('ok'):
            print(f'  实测: 正常出链接  {pr["ms"]}ms  {pr["url_head"]}')
        else:
            print(f'  实测: 失败 [{pr["gate"]}] status={pr.get("status")} {pr["ms"]}ms')
            print(f'  原文: {pr.get("msg")}')
    if rep['learned_fields']:
        print(f'  已学到的必填字段: {", ".join(rep["learned_fields"])}')

    print('\n--- 各层可用性 ---')
    for p in rep['providers']:
        mark = '可用' if p['available'] else '不可用'
        extra = '' if p['available'] else f'  ({p["why_not"]})'
        stt = rep['stats'].get(p['name']) or {}
        s = ''
        if stt:
            s = f'  成功{stt.get("ok",0)}/失败{stt.get("fail",0)}'
        print(f'  [{mark:4s}] {p["name"]:8s} {p["note"]}{extra}{s}')

    print('\n--- 建议 ---')
    print('  ' + ADVICE.get(rep['gate'] or G_UNKNOWN, ADVICE[G_UNKNOWN]))
    print()
    return 0


if __name__ == '__main__':
    sys.exit(main())
