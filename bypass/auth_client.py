#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import json
import base64
import os
import subprocess
import sys
import itertools
import random
import string
import threading
import time
import urllib.parse
import requests
import urllib3


def enable_utf8_stdio():
    """把 stdout/stderr 强制改成 UTF-8，避免服务器 locale 不是 UTF-8 时崩。

    这台机器上跑没事，是因为 Windows 控制台默认能编中文。但服务器上完全是另一种
    情况：systemd 直接拉起的进程、Docker 里 `LANG` 没设、`locale` 为 C/POSIX 时，
    sys.stdout.encoding 会退化成 ANSI_X3.4-1968（ASCII）。此时任何一次带中文的
    print() 都抛 UnicodeEncodeError —— 而本项目的日志全是中文，等于每条请求都会
    在打印日志时炸掉，表现为 "solve exception: UnicodeEncodeError"，看起来像
    上游/算法出错，实际只是编码。

    errors='replace' 是第二道保险：即便 reconfigure 被某些环境拒绝，也只会丢字符，
    不会抛异常。这个函数在模块导入时自执行，任何入口（main / server / 探针）都覆盖到。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            enc = (getattr(stream, 'encoding', None) or '').lower().replace('-', '')
            if enc in ('utf8', 'utf8mb4'):
                continue
            stream.reconfigure(encoding='utf-8', errors='replace')
        except Exception:
            pass


enable_utf8_stdio()
from Crypto.Cipher import AES as AES
# fake_useragent 是可选依赖:没有它就用下面这条固定的 iPhone UA,功能不受影响。
# fake_useragent is optional: without it we fall back to the fixed iPhone UA below.
try:
    from fake_useragent import UserAgent

    UA_SOURCES = (
        UserAgent(browsers=['Mobile Safari'], platforms='mobile'),
        UserAgent(browsers=['Chrome Mobile'], platforms='mobile', min_version=100.0),
    )
except Exception:  # ImportError 及其内部初始化异常都按缺失处理
    UA_SOURCES = ()

AUTH_API = "https://auth.platorelay.com/api"

# 备用求解域名。主域名被区域策略拦 / 单节点抽风时，自愈和重试队列会切过来。
#   DELTA_AUTH_HOSTS=auth.platorelay.com,auth2.example.com
_AUTH_HOSTS = [h.strip() for h in
               (os.environ.get('DELTA_AUTH_HOSTS') or '').split(',') if h.strip()]
_auth_host_idx = 0


def rotate_api_host():
    """切到下一个求解域名（写进 AUTH_API，后续请求立即生效）。

    只在配了 DELTA_AUTH_HOSTS 且至少两个域名时才有意义，否则返回 False ——
    不静默"切到同一个地址"，那会让上层误以为换手牌了。
    """
    global AUTH_API, _auth_host_idx
    hosts = _AUTH_HOSTS
    if len(hosts) < 2:
        return False, f'备用求解域名不足（当前 {len(hosts)} 个，需 DELTA_AUTH_HOSTS）'
    _auth_host_idx = (_auth_host_idx + 1) % len(hosts)
    AUTH_API = f'https://{hosts[_auth_host_idx]}/api'
    return True, AUTH_API


def api_base():
    return AUTH_API

# ---------------------------------------------------------------------------
# 硬墙钟超时执行器
#
# 为什么必须有这个东西：requests / urllib3 的 timeout 是 **socket 级**的，
# connect 和 read 各自计时；read timeout 在连接上有零星数据时会被不断重置，
# 而 urllib3 2.x 的 Timeout(total=) 只用来算 connect/read 各自的上限，
# **不是墙钟上限**。
#
# 实测（本机走本地代理）：设了 connect=4 / read=10 / total=12，单次 metadata
# 请求能卡到 90s+ 不返回，连 print 都出不来，整条链路假死（进程被 SIGTERM 才停）。
# 唯一可靠的办法是在外层用 daemon 线程 + Event.wait 做墙钟中断：
# 到点就返回，卡住的线程自生自灭（daemon，不阻止解释器退出）。
# ---------------------------------------------------------------------------
_LEAK_LOCK = threading.Lock()
_LEAK = {'n': 0}
_LEAK_MAX = 64          # 同时挂着的请求上限，防极端情况下线程无限堆积


def call_with_deadline(fn, timeout, *args, **kwargs):
    """在 daemon 线程里跑 fn，墙钟超过 timeout 秒就放弃。

    返回 (value, None) 或 (None, 错误字符串)。
    """
    with _LEAK_LOCK:
        if _LEAK['n'] >= _LEAK_MAX:
            return None, 'request budget exhausted (too many hung connections)'

    box = {}
    done = threading.Event()
    late = threading.Event()

    def runner():
        try:
            box['v'] = fn(*args, **kwargs)
        except BaseException as e:   # noqa: BLE001
            box['e'] = e
        finally:
            done.set()
            if late.is_set():
                with _LEAK_LOCK:
                    _LEAK['n'] = max(0, _LEAK['n'] - 1)

    threading.Thread(target=runner, daemon=True).start()
    if not done.wait(timeout):
        late.set()
        with _LEAK_LOCK:
            _LEAK['n'] += 1
        return None, f'timeout after {timeout:.0f}s'
    if 'e' in box:
        return None, str(box['e'])
    return box.get('v'), None


# ---------------------------------------------------------------------------
# 客户端版本号：自动跟上上游
#
# 提交时带的 x-client-version 得跟上游网页客户端一致，旧了会被直接拒绝
# （"outdated client"）。写死在代码里，上游一升级就得手动跟着改 —— 漏改一次
# 服务就全线停摆。所以自动拿：
#
# 1. 抓入口页 HTML（约 500B），抠出 /assets/index-<hash>.js 的地址
# 2. Range 请求只取脚本前 256KB（版本号实测在前 128KB 里，2MB 全量省掉 87%）
# 3. 从引号包裹的字符串里抠出所有形如 x.y.z 的候选
# 4. 逐个用假 ticket 探测：版本太旧回 "outdated client"，版本可用回别的错
#    （假 ticket 本身无效的那种）。探测零成本：不碰真实会话、不占求解名额
#
# 脚本内容用完即弃：候选一提取出来就释放，不落盘，内存里只留几字节的版本号。
# 全部候选失败时退回 FALLBACK_VERSION。缓存 1 小时，刷新只发生在后台线程。
# ---------------------------------------------------------------------------
APP_URL = "https://auth.platorelay.com/a"
# 实测 2026-11 上游最低要求 8.2.3（8.2.2 及以下一律 outdated client）。
# 兜底值只要不低于真实最低版本即可：服务器只做 ">= 最小值" 判定，
# 实测 9.9.9 / 99.99.99 同样放行，所以宁高勿低。
FALLBACK_VERSION = "8.2.3"
VER_TTL = 3600.0  # 版本缓存秒数

_client_version = FALLBACK_VERSION
_client_version_at = time.time()  # 启动即视为新鲜:首请求用兜底值,刷新交给后台线程
_ver_lock = threading.Lock()


def _http_get(url, headers=None, timeout=8):
    # 紧超时：版本提取挂了不能拖慢提交路径
    try:
        r = requests.get(url, headers=headers or {}, timeout=timeout)
        if r.status_code != 200:
            return None
        return r.text
    except Exception:
        return None


def _version_candidates(text):
    # 从引号包裹的字符串里抠 x.y.z 形状的候选（三段纯数字）
    # 单引号/双引号/反引号都收：混淆器换引号风格时不会整段失效
    if not text:
        return []
    found = []
    i = 0
    data = text
    n = len(data)
    while i < n:
        c = data[i]
        if c in ("'", '"', '`'):
            j = data.find(c, i + 1)
            if j < 0:
                break
            token = data[i + 1:j]
            parts = token.split('.')
            if (len(parts) == 3
                    and all(p.isdigit() for p in parts)
                    and token not in found):
                found.append(token)
            i = j + 1
            continue
        i += 1
    return found


def _version_works(version, ua, retries=1):
    # 假 ticket 探测：格式合法但不指向任何真会话。
    # 服务器先查版本后查票 —— outdated = 版本不行，invalid payload = 版本过了。
    #
    # 三态返回，这点很关键：
    #   True  = 版本可用
    #   False = 上游明确说 outdated（可以放心往上跳）
    #   None  = 网络抖动/非 200，判断不了（绝不能被当成 False，否则会白白跳版本）
    letters = string.ascii_letters + string.digits
    fake = ''.join(random.choice(letters) for _ in range(64))
    built = build_meta_stream(fake, user_agent=ua)
    if built is None:
        return None
    meta, stream = built
    url = f"{AUTH_API}/session/step?ticket={urllib.parse.quote(fake)}&service=3"
    body = json.dumps({"captcha": None, "meta": meta, "stream": stream, "resolved": True}).encode()
    for attempt in range(retries + 1):
        try:
            r = probe_pool.request('PUT', url, body=body, redirect=False, headers={
                'User-Agent': ua,
                'Content-Type': 'application/json',
                'Accept': 'application/json',
                'x-client-name': 'platoboost webclient',
                'x-client-version': version,
            })
            if r.status != 200:
                if attempt < retries:
                    time.sleep(0.4)
                    continue
                return None
            return b'outdated client' not in r.data
        except Exception:
            # 网络抖动不能当成"版本不行"，否则会白白往上跳版本
            if attempt < retries:
                time.sleep(0.4)
                continue
            return None
    return None


def _rank_candidates(tokens):
    """把 JS 里抠到的 x.y.z 过滤成可信的版本号，并按数值降序。

    bundle 里混着 '127.0.0'(IP)、'01.0.05'(带前导零的时间戳/尺寸) 这类假阳性，
    直接按文件顺序试会把探针预算全烧在垃圾上（实测能在前 200KB 里抠出十几个）。
    真实客户端版本是其中数值最大的那个合理值，所以过滤后降序、从大到小试。
    """
    out = []
    for t in tokens:
        parts = t.split('.')
        if len(parts) != 3:
            continue
        if any(len(p) > 1 and p[0] == '0' for p in parts):   # 01.0.05
            continue
        try:
            a, b, c = (int(p) for p in parts)
        except ValueError:
            continue
        if not (1 <= a <= 30 and b <= 90 and c <= 90):        # 127.0.0
            continue
        out.append((a, b, c, t))
    out.sort(reverse=True)
    return [t for *_, t in out]


def _scan_stream_for_versions(url, max_bytes=4 * 1024 * 1024):
    """流式下载 bundle，边收边找候选，找到能用的立刻断开。

    上游 bundle 已经涨到 ~1.9MB，而版本字符串躺在字符串表里、位置随机
    （2026-11 实测在 81.8% 处，偏移 1578904）。固定取前 256KB 的方案必然扫空，
    一次性全量 GET 又容易在慢链路/代理上被中断（实测 10s 就被掐断，0 字节）。
    所以改成流式：读到哪儿算到哪儿，候选一验证通过就 close()，不浪费带宽。
    """
    found = []
    try:
        r = requests.get(url, headers={'User-Agent': FALLBACK_UA},
                         stream=True, timeout=(5, 20))
        if r.status_code != 200:
            try:
                r.close()
            except Exception:
                pass
            return found
        buf = ''
        total = 0
        for chunk in r.iter_content(chunk_size=65536):
            if not chunk:
                continue
            if isinstance(chunk, bytes):
                chunk = chunk.decode('utf-8', 'replace')
            buf += chunk
            total += len(chunk)
            for c in _version_candidates(buf):
                if c not in found:
                    found.append(c)
                    # 新候选马上验一次，能用就不必把整包拉完
                    if _version_works(c, FALLBACK_UA) is True:
                        try:
                            r.close()
                        except Exception:
                            pass
                        return [c]
            # 只留尾部一小段，避免跨块把 '8.2' + '.3' 截断，也避免 buf 无限膨胀
            if len(buf) > 8192:
                buf = buf[-4096:]
            if total >= max_bytes:
                break
        try:
            r.close()
        except Exception:
            pass
    except Exception:
        pass
    return found


def _escalate_version(base=None, max_probes=24):
    """抓不到 JS 时的兜底：按数值向上试探。

    上游只做 ">= 最小值" 判定（实测 9.9.9 / 99.99.99 都放行），所以从当前值
    往上试，第一个通过的即可用。典型一个月跳一次小版本，2~3 个探针就命中；
    major 跳档时由大跨步兜住。每次探测 ~0.35s，且不碰真实会话、不占求解名额。
    """
    if base is None:
        base = _client_version
    try:
        maj, mi, pa = (int(x) for x in base.split('.'))
    except Exception:
        maj, mi, pa = 8, 2, 3

    cands = []
    for k in range(1, 9):                       # 8.2.4 ... 8.2.11
        cands.append(f'{maj}.{mi}.{pa + k}')
    for k in range(1, 5):                       # 8.3.0 ... 8.6.0
        cands.append(f'{maj}.{mi + k}.0')
    for k in range(1, 6):                       # 9.0.0 ... 13.0.0
        cands.append(f'{maj + k}.0.0')

    seen = set()
    n = 0
    unknown_streak = 0
    for c in cands:
        if c in seen:
            continue
        seen.add(c)
        n += 1
        if n > max_probes:
            break
        ok = _version_works(c, FALLBACK_UA)
        if ok is True:
            return c
        if ok is None:
            # 连续判断不了就收手，别在断网时把探针预算烧光
            unknown_streak += 1
            if unknown_streak >= 3:
                return None
        else:
            unknown_streak = 0
    return None


def _refresh_client_version():
    global _client_version, _client_version_at
    updated = False

    # 1) 现用版本还够用？一次探针（~0.5s）就能确认，绝大多数刷新到这里就结束。
    #    先问这一句的意义：服务器是 ">= 最小值" 判定，不先确认就往上跳会白白漂移。
    ok = _version_works(_client_version, FALLBACK_UA, retries=2)
    if ok is True:
        _client_version_at = time.time()
        return True
    if ok is None:
        # 网络抖，判断不了 —— 保持现状。宁可不升，也不要因为一次超时把版本号乱跳。
        _client_version_at = time.time()
        return False

    # 2) 明确 outdated -> 数值爬升。不依赖任何解析，只要能算出 ">= 最小值" 就必能自愈。
    v = _escalate_version(_client_version)
    if v:
        global_set(v)
        _client_version_at = time.time()
        return True

    # 3) 爬升全失败（上游改判定语义了）-> 再去 bundle 里抠真实版本号
    try:
        html = _http_get(APP_URL, {'User-Agent': FALLBACK_UA}, timeout=8)
        if html:
            i = html.find('/assets/index-')
            j = html.find('"', i) if i >= 0 else -1
            if i >= 0 and j > i:
                script_url = 'https://auth.platorelay.com' + html[i:j]
                for cand in _rank_candidates(_scan_stream_for_versions(script_url)):
                    if _version_works(cand, FALLBACK_UA) is True:
                        global_set(cand)
                        updated = True
                        break
    except Exception:
        pass

    _client_version_at = time.time()
    return updated


def force_refresh_version():
    """step 被回 outdated client 时当场触发一次强制刷新，返回新版本号。"""
    global _client_version_at
    with _ver_lock:
        _client_version_at = 0.0
        try:
            _refresh_client_version()
        except Exception:
            _client_version_at = time.time()
    return _client_version


def global_set(version):
    global _client_version, _client_version_at
    if version != _client_version:
        print(f'[版本] 客户端版本更新: {_client_version} -> {version}', flush=True)
    _client_version = version
    _client_version_at = time.time()


def client_version():
    # 纯读缓存。刷新只发生在后台线程，提交路径绝不被版本提取拖慢。
    global _client_version_at
    if time.time() - _client_version_at > VER_TTL:
        # 过期了就在本线程刷一次（带锁防并发重复刷）
        if _ver_lock.acquire(blocking=False):
            try:
                if time.time() - _client_version_at > VER_TTL:
                    _refresh_client_version()
                    _client_version_at = time.time()
            finally:
                _ver_lock.release()
    return _client_version


def start_version_watcher():
    # 后台线程：启动刷一次，之后每小时一次
    def worker():
        while True:
            try:
                _refresh_client_version()
                _client_version_at = time.time()
            except Exception:
                _client_version_at = time.time()
            time.sleep(VER_TTL)
    t = threading.Thread(target=worker, daemon=True)
    t.start()

# ticket 至少要这么长才能切出两组 AES 钥匙。
#
# 第二组从第 2 个字符起算、取到第 33 个（ticket[1:17] 和 ticket[17:33]），所以 33 是
# 硬下限。低于这个长度 Python 的切片不会报错，只会给出短的甚至空的钥匙，等到 AES 那
# 一步才炸 ValueError —— 而那时候验证码 token 已经拿到了，白烧一个。
MIN_TICKET_LEN = 33


# fake_useragent 每次取用约 16ms(内部重新筛选数据集),高并发下是显著开销:
# 启动时预生成一批,之后 O(1) 轮转取用 —— 对外表现(UA 多样性)不变。
UA_POOL = []
UA_IDX = itertools.count()
UA_SCREEN = {}

SCREENS_IPHONE = ('390x844', '393x852', '375x812', '414x896', '430x932', '428x926', '360x780')
SCREENS_IPAD = ('820x1180', '834x1194', '768x1024', '744x1133', '1024x1366')
SCREENS_ANDROID = ('360x800', '412x915', '393x873', '384x854', '360x780', '412x892', '432x960')

FALLBACK_UA = ('Mozilla/5.0 (iPhone; CPU iPhone OS 18_3_2 like Mac OS X) '
               'AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.3.1 Mobile/15E148 Safari/604.1')


def screens_for(platform, os_name):
    p = (platform or '').lower()
    o = (os_name or '').lower()
    if 'ipad' in p or 'tablet' in p:
        return SCREENS_IPAD
    if 'iphone' in p or 'ipod' in p or 'ios' in o:
        return SCREENS_IPHONE
    return SCREENS_ANDROID


def build_ua_pool(size=32):
    #预生成 UA 池:从各来源均匀取用,并为每个 UA 绑定一个匹配其平台的屏幕分辨率
    # fake_useragent 缺失时 UA_SOURCES 为空,直接用固定 UA 兜底。
    pool = []
    if not UA_SOURCES:
        return [FALLBACK_UA]
    per = max(1, size // len(UA_SOURCES))
    for src in UA_SOURCES:
        got = 0
        tried = 0
        while got < per and tried < per * 8:
            tried += 1
            try:
                rec = src.getRandom
            except Exception:
                break
            if not isinstance(rec, dict):
                break
            s = rec.get('useragent')
            if not s or s in UA_SCREEN:
                continue
            cands = screens_for(rec.get('platform'), rec.get('os'))
            # 用 UA 字符串哈希取模,保证同一 UA 恒定拿到同一分辨率
            UA_SCREEN[s] = cands[hash(s) % len(cands)]
            pool.append(s)
            got += 1
    if not pool:
        pool = [FALLBACK_UA]
        UA_SCREEN.setdefault(FALLBACK_UA, SCREENS_IPHONE[0])
    return pool


def rand_ua():
    global UA_POOL
    if not UA_POOL:
        UA_POOL = build_ua_pool()
    return UA_POOL[next(UA_IDX) % len(UA_POOL)]


def pick_screen(user_agent):
    #返回与该 UA 绑定的屏幕分辨率;未知 UA 则按其字符串特征推断
    s = UA_SCREEN.get(user_agent)
    if s:
        return s
    low = (user_agent or '').lower()
    if 'ipad' in low:
        cands = SCREENS_IPAD
    elif 'iphone' in low or 'ipod' in low:
        cands = SCREENS_IPHONE
    else:
        cands = SCREENS_ANDROID
    return cands[hash(user_agent or '') % len(cands)]

#AES-CTR

def aes_ctr_encrypt(plaintext, key_bytes, iv_bytes):
    #对当前计数器进行加密
    key = bytearray(key_bytes) if isinstance(key_bytes, (bytes, bytearray)) else bytearray(key_bytes)
    iv = bytearray(iv_bytes) if isinstance(iv_bytes, (bytes, bytearray)) else bytearray(iv_bytes)
    data = plaintext.encode() if isinstance(plaintext, str) else plaintext
    out = bytearray()
    for i in range(0, len(data), 16):
        blk = AES.new(bytes(key), AES.MODE_ECB).encrypt(bytes(iv))
        out += bytes(a ^ b for a, b in zip(data[i:i + 16], blk))
        #递增计数器
        j = 15
        while True:
            iv[j] = (iv[j] + 1) & 0xFF
            if iv[j] != 0:
                break
            j -= 1
            if j < 0:
                break
    return bytes(out)


def build_meta_stream(ticket, now_ms=None, user_agent=None, screen=None):
    #从ticket构建AES-CTR字段。ticket 太短返回 None
    # user_agent: 加密体内声明的 UA。应与本次请求 HTTP 头的 User-Agent 保持一致,
    #   否则"头里是随机某机型、体内固定写死另一机型"会形成自相矛盾的指纹。
    # screen: 屏幕尺寸,默认按 UA 平台自动挑一个匹配值。
    #
    # 返回 None 而不是让它炸：Python 切片越界不报错,ticket 只有 8 个字符时
    # ticket[16:32] 直接给空串,一路带到 AES 才抛 ValueError。那时候验证码已经解完、
    # token 已经拿到了,白烧一个。所以在这里就挡住,让调用方早点知道。
    if len(ticket) < MIN_TICKET_LEN:
        return None

    if now_ms is None:
        now_ms = int(time.time() * 1000)
    if user_agent is None:
        user_agent = rand_ua()
    if screen is None:
        screen = pick_screen(user_agent)

    key_meta = ticket[:16]
    ctr_meta = ticket[16:32]
    key_stream = ticket[1:17]
    ctr_stream = ticket[17:33]

    meta_plain = json.dumps({
        "browserInfo": [{
            "screen": screen,
            "ua": user_agent,
            "time": now_ms
        }]
    }, separators=(',', ':'))

    stream_plain = json.dumps({
        "events": [{"event": 1, "data": {"time": now_ms}}]
    }, separators=(',', ':'))

    meta = aes_ctr_encrypt(
        meta_plain,
        [ord(c) for c in key_meta],
        [ord(c) for c in ctr_meta]
    ).hex()

    stream = aes_ctr_encrypt(
        stream_plain,
        [ord(c) for c in key_stream],
        [ord(c) for c in ctr_stream]
    ).hex()

    return meta, stream


#获取ticket

def extract_ticket(arg):
    #从 auth URL 或原始 ticket 字符串取出 ticket。不读任何文件
    #
    # HTTP 接口一律用这个。外面传进来的东西不能拿去碰服务器上的文件 —— 以前这里看到
    # 含斜杠的输入就当路径去 open(),传 /etc/passwd 进来会把文件内容读出来当 ticket,
    # 然后 URL 编码后发到上游去。要从文件读的走 extract_ticket_from_arg。
    t = arg.strip()
    if t.startswith('http'):
        parsed = urllib.parse.urlparse(t)
        qs = urllib.parse.parse_qs(parsed.query)
        if 'd' in qs:
            return qs['d'][0]
        return t
    return t


def extract_ticket_from_arg(arg):
    #命令行专用：跟 extract_ticket 一样,但看着像路径时会当文件读
    #
    # 只给命令行参数用（python main.py tickets.txt 那种）。HTTP 接口绝对不能调这个,
    # 那等于让任何能访问接口的人指定服务器上的文件路径。
    t = arg.strip()
    if t.startswith('http'):
        return extract_ticket(t)
    if t.endswith('.txt') or '/' in t or '\\' in t:
        try:
            with open(t) as f:
                content = f.read().strip()
                if content:
                    return extract_ticket_from_arg(content)
        except (IOError, OSError):
            pass
    return t


def decode_callback_url(loot_url):
    # 从loot URL的r=参数解码回调URL
    qs = urllib.parse.parse_qs(urllib.parse.urlparse(loot_url).query)
    r_param = qs.get('r', [''])[0]
    if not r_param:
        return None
    b64 = r_param.replace('-', '+').replace('_', '/')
    padding = (4 - len(b64) % 4) % 4
    try:
        dec = base64.b64decode(b64 + '=' * padding).decode('utf-8')
        if dec.startswith('http'):
            return dec
    except Exception:
        pass
    return None


def extract_ticket_from_callback(callback_url):
    # 从回调URL获取下一个ticket
    if not callback_url:
        return None
    qs = urllib.parse.parse_qs(urllib.parse.urlparse(callback_url).query)
    return qs.get('d', [None])[0]


#步骤

#urllib3连接池
#
# 【必须走代理】urllib3.PoolManager **不读** HTTP(S)_PROXY 环境变量，只有
# requests / curl 会读。所以在"必须靠代理出网"的机器上，PoolManager 会傻乎乎
# 去直连上游，然后一个地址一个地址地超时 —— 实测每次都精确卡 8.00s 失败
# （getaddrinfo 返回 IPv4+IPv6 两条，各 4s connect 超时），而同一时刻 curl
# 走代理 2/3 次是成功的。这是"明明能上网但程序连不上"的真正原因。
#
# 这里按环境变量决定 PoolManager 还是 ProxyManager，和 requests 行为对齐。
#
# 【服务器上要注意】只认 HTTPS_PROXY/HTTP_PROXY 是不够的：
#   - 机房机器常为 apt/yum 配了全局代理，直连本来更快，却会白绕一圈；
#   - 反过来，某些云主机只给 ALL_PROXY（socks5），urllib3 的 ProxyManager
#     不支持 socks，硬用会直接连不上；
#   - NO_PROXY 是标准变量，requests/curl 都认，PoolManager 不认。
# 所以这里做一层统一解析，并支持三个显式开关，优先级从高到低：
#   DELTA_NO_PROXY=1            强制直连（服务器最常用）
#   DELTA_PROXY=http://...      强制走指定代理（覆盖环境变量）
#   NO_PROXY=*|host,domain      按标准语义跳过代理
_TARGET_HOSTS = ('auth.platorelay.com', 'api.platoboost.net')


def _no_proxy_match(host, no_proxy):
    """按 requests/curl 的语义判断 host 是否命中 NO_PROXY。"""
    if not no_proxy:
        return False
    host = (host or '').lower().strip('.')
    for raw in no_proxy.replace(';', ',').split(','):
        pat = raw.strip().lower()
        if not pat:
            continue
        if pat == '*':
            return True
        pat = pat.lstrip('.').split(':')[0]      # 去掉 .example.com 和 :port
        if host == pat or host.endswith('.' + pat):
            return True
    return False


def _resolve_proxy():
    """返回 (proxy_url_or_None, 说明, requests 是否该读环境代理)。

    只在模块导入时算一次。环境变量改了要重启进程 —— 这里刻意不做热更新，
    免得运行中途出网方式突变，排查问题时更难判断。

    第三个返回值的意义：urllib3 和 requests 是两套出网逻辑，必须对齐。
    比如 DELTA_NO_PROXY=1 时 urllib3 走直连，但 requests 仍会读环境代理，
    于是 metadata（requests）走代理、step（urllib3）走直连，一半成功一半失败，
    看起来像"随机抖动"，极难查。所以这里把结论一并带出去。
    """
    if os.environ.get('DELTA_NO_PROXY', '').strip() not in ('', '0', 'false', 'False'):
        return None, 'DELTA_NO_PROXY 强制直连', False
    forced = (os.environ.get('DELTA_PROXY') or '').strip()
    if forced:
        return forced, f'DELTA_PROXY 指定 {forced}', True
    no_proxy = os.environ.get('NO_PROXY') or os.environ.get('no_proxy') or ''
    if all(_no_proxy_match(h, no_proxy) for h in _TARGET_HOSTS):
        # requests 自己也认 NO_PROXY，两边结论一致，不用关 trust_env
        return None, 'NO_PROXY 命中上游域名，直连', True
    for key in ('HTTPS_PROXY', 'https_proxy', 'HTTP_PROXY', 'http_proxy'):
        val = (os.environ.get(key) or '').strip()
        if val:
            if val.lower().startswith('socks'):
                # urllib3 的 ProxyManager 不支持 socks，硬用等于全部失败。
                return None, f'{key}={val} 是 socks（不支持），已忽略并直连', False
            return val, f'使用 {key}={val}', True
    all_proxy = (os.environ.get('ALL_PROXY') or os.environ.get('all_proxy') or '').strip()
    if all_proxy:
        return None, f'只有 ALL_PROXY={all_proxy}，urllib3 不支持，已直连', False
    return None, '环境无代理变量，直连', True


_PROXY, _PROXY_NOTE, _TRUST_ENV = _resolve_proxy()


def proxy_info():
    """给启动日志 / 健康检查用：现在的出网方式是什么。"""
    return {'proxy': _PROXY, 'mode': 'proxy' if _PROXY else 'direct',
            'note': _PROXY_NOTE, 'trust_env': _TRUST_ENV}


def _make_pool(num_pools, maxsize, timeout):
    kw = dict(num_pools=num_pools, maxsize=maxsize, block=False, retries=False,
              timeout=timeout)
    if _PROXY:
        return urllib3.ProxyManager(_PROXY, **kw)
    return urllib3.PoolManager(**kw)


# total 不是"connect + read"的软叠加 —— 这点必须写清楚：
# TCP 连到代理是一瞬间，真正耗时在 CONNECT 隧道建立和 TLS 握手，这两段既不算
# connect 也不算 read。另外 urllib3 的 total 只是给 connect/read 各自封顶，
# **不是墙钟上限**（见 call_with_deadline 的说明），真正的墙钟兜底在那一层。
#
# 高并发：单主机连接池要够大，否则请求排队等连接（原 maxsize=1 是并发瓶颈）
# 读超时 12s -> 8s：上游正常应答是 0.4s（metadata）/ 0.6s（step），8s 已是 20 倍
# 余量；而一旦链路抽风，12s 会让"一次抖动"直接吃掉 12~15s，把一次本该十几秒的
# 求解拖成 20s+。8s 既不会误杀慢响应，又能让抖动早一点触发重试。
STEP_TIMEOUT = urllib3.Timeout(connect=6.0, read=8.0, total=10.0)
step_pool = _make_pool(num_pools=8, maxsize=64, timeout=STEP_TIMEOUT)

# 版本探针专用连接池：首包偶发 4~6s（TLS 握手被拖），给更宽的预算，
# 否则会把"版本可用"误判成网络错误。
probe_pool = _make_pool(
    num_pools=2, maxsize=8,
    timeout=urllib3.Timeout(connect=8.0, read=12.0, total=20.0),
)


def reset_egress(mode=None, timeout=STEP_TIMEOUT):
    """运行期切换出网方式。给自愈引擎用。

    原来 _PROXY 只在导入时算一次，理由是"运行中途出网方式突变会让排查变难"。
    这条对人工操作成立，对自动修复不成立：服务器上最容易踩的坑就是
    「直连不通但环境里配了代理」或反过来，而这两种情况在没有外网时根本
    分不清。所以给自愈开一个受控的切换口，切换结果写进 state，日志里能查到
    什么时候切的、从什么切到什么。

    mode:
      'direct'  强制直连（等价 DELTA_NO_PROXY=1）
      'env'     跟随环境变量（清掉 DELTA_NO_PROXY）
      None      按当前环境重新解析（不改变量）

    返回 (ok, note)。
    """
    global _PROXY, _PROXY_NOTE, _TRUST_ENV, step_pool, probe_pool
    before = _PROXY_NOTE
    try:
        if mode == 'direct':
            os.environ['DELTA_NO_PROXY'] = '1'
        elif mode == 'env':
            os.environ.pop('DELTA_NO_PROXY', None)
        _PROXY, _PROXY_NOTE, _TRUST_ENV = _resolve_proxy()
        # 重建连接池：旧池里的空闲连接仍指向原来的出口，继续用会得到
        # "明明切了还是连不上"的假象。
        step_pool = _make_pool(num_pools=8, maxsize=64, timeout=timeout)
        probe_pool = _make_pool(
            num_pools=2, maxsize=8,
            timeout=urllib3.Timeout(connect=8.0, read=12.0, total=20.0))
        return True, f'{before} -> {_PROXY_NOTE}'
    except Exception as e:
        return False, f'切换失败: {type(e).__name__}: {e}'


def reset_pool():
    """只重建连接池，不动出网方式。

    什么时候需要：连接池里躺着一批"看起来还在、实际对端已经关了"的连接
    （NAT 超时 / 上游滚动重启），表现是**每发请求都卡满读超时才失败**。
    换出网（reset_egress）太重，会把本来正确的方式也换掉；这里只换连接。
    """
    global step_pool, probe_pool
    try:
        step_pool = _make_pool(num_pools=8, maxsize=64, timeout=STEP_TIMEOUT)
        probe_pool = _make_pool(
            num_pools=2, maxsize=8,
            timeout=urllib3.Timeout(connect=8.0, read=12.0, total=20.0))
        return True, '连接池已重建'
    except Exception as e:
        return False, f'重建失败: {type(e).__name__}: {e}'


def create_session():
    #构建requests
    s = requests.Session()
    # 和 urllib3 连接池保持同一种出网方式，见 _resolve_proxy 的第三个返回值
    s.trust_env = _TRUST_ENV
    s.headers.update({
        'User-Agent': rand_ua(),
        'Content-Type': 'application/json',
        'Accept': 'application/json, text/plain, */*',
    })
    #无重试
    adapter = requests.adapters.HTTPAdapter(
        pool_connections=8, pool_maxsize=32, max_retries=0)
    s.mount('http://', adapter)
    s.mount('https://', adapter)
    return s


# 求解侧的验证码钩子。
#
# 上游目前不校验 step 的 captcha 字段（发 None 即可）。但协议随时可能收紧 ——
# 那天到来时不应该需要改 do_step 的调用方，所以留三个入口，按优先级：
#   1) CAPTCHA_HOOK = callable(ticket) -> str     进程内注入（打码/浏览器通道）
#   2) DELTA_CAPTCHA_CMD="<shell 命令>"          命令最后一行输出被当作 token
#   3) 都没有 -> 发 None，行为与现在完全一致
CAPTCHA_HOOK = None


def _captcha_value(ticket):
    if CAPTCHA_HOOK is not None:
        try:
            v = CAPTCHA_HOOK(ticket)
            if v:
                return v
        except Exception:
            pass
    cmd = (os.environ.get('DELTA_CAPTCHA_CMD') or '').strip()
    if cmd:
        try:
            out = subprocess.run(cmd, shell=True, capture_output=True,
                                 text=True, timeout=240)
            lines = [l.strip() for l in (out.stdout or '').splitlines() if l.strip()]
            if lines:
                return lines[-1]
        except Exception:
            pass
    return None


def do_step(ticket, service=3, session=None, now_ms=None):
    # PUT /api/session/step
    #
    # 新版协议不校验 captcha 字段(可为 null),旧版图形验证码已随上游换 orbit 类型
    # 而作废,这里按真实浏览器抓包的形态发 null。
    # 若上游恢复校验,见上面的 CAPTCHA_HOOK / DELTA_CAPTCHA_CMD。
    step_ua = rand_ua()

    # ticket 太短就直接说不行,别往上游发。
    # 以前这里会让 AES 抛 ValueError,一路传到 main.py 的 except 才被接住,
    # 白跑一整轮。
    built = build_meta_stream(ticket, now_ms, user_agent=step_ua)
    if built is None:
        return {"success": False,
                "error": f"ticket 长度不足（{len(ticket)} 字符，至少要 {MIN_TICKET_LEN} 个）"}
    meta, stream = built

    url = f"{AUTH_API}/session/step?ticket={urllib.parse.quote(ticket)}&service={service}"

    body = json.dumps({
        "captcha": _captcha_value(ticket),
        "meta": meta,
        "stream": stream,
        "resolved": True
    }).encode()

    STEP_HTTP_RETRIES = 3
    STEP_RETRY_SLEEP = 0.5
    # 重试用更紧的预算：第一次已经证明链路慢，重试再给 12s 只会把一次失败
    # 放大成 24s。3+6+8 的组合保证重试最多再花 8s。
    RETRY_TIMEOUT = urllib3.Timeout(connect=3.0, read=6.0, total=8.0)
    STEP_DEADLINE = 16.0       # 墙钟兜底（内层 12s 先触发）
    RETRY_DEADLINE = 10.0
    last_err = None
    refreshed = False          # 本轮是否已经为 "outdated client" 刷新过版本
    conn_retried = False       # 连接层异常是否已重试过（只给一次机会）
    for attempt in range(STEP_HTTP_RETRIES + 1):
        try:
            _hdr = {
                'User-Agent': step_ua,
                'Content-Type': 'application/json',
                'Accept': 'application/json, text/plain, */*',
                'x-client-name': 'platoboost webclient',
                'x-client-version': client_version()
            }
            _to = STEP_TIMEOUT if attempt == 0 else RETRY_TIMEOUT

            def _once(_t=_to, _h=_hdr):
                return step_pool.request('PUT', url, body=body, redirect=False,
                                         timeout=_t, headers=_h)

            # 墙钟兜底：内层 socket 超时在这条链路上可能完全不触发（实测卡 90s+）
            r, _err = call_with_deadline(
                _once, STEP_DEADLINE if attempt == 0 else RETRY_DEADLINE)
            if _err:
                last_err = _fmt_err(_err)
                if not conn_retried:
                    conn_retried = True
                    time.sleep(STEP_RETRY_SLEEP)
                    continue
                return {"success": False, "error": last_err, "conn_error": True}
            if r.status != 200:
                last_err = f"http {r.status}"
                if attempt < STEP_HTTP_RETRIES:
                    time.sleep(STEP_RETRY_SLEEP)
                    continue
                return {"success": False, "error": last_err}
            try:
                data = json.loads(r.data)
            except Exception:
                last_err = "non-json response"
                if attempt < STEP_HTTP_RETRIES:
                    time.sleep(STEP_RETRY_SLEEP)
                    continue
                return {"success": False, "error": last_err}
            # 上游升版本 -> 当场强制刷新一次再重发，不必等下一小时的定时刷新。
            # 每小时刷一次意味着升级后的最长 1 小时全线停摆，这条就地自愈把窗口压到一次请求。
            if isinstance(data, dict) and not refreshed:
                msg = str(data.get('message') or data.get('error') or '').lower()
                if 'outdated' in msg:
                    refreshed = True
                    newv = force_refresh_version()
                    last_err = f"outdated client (刷新版本 -> {newv})"
                    if attempt < STEP_HTTP_RETRIES:
                        continue
            return data
        except Exception as e:
            # 连接层异常只重试一次。代理抖动时一次请求能挂到 12s（total 上限），
            # 按 STEP_HTTP_RETRIES 重 3 次就是 36s，而外层本来就有 3 轮 + 换 service。
            last_err = _fmt_err(e)
            if not conn_retried:
                conn_retried = True
                time.sleep(STEP_RETRY_SLEEP)
                continue
            return {"success": False, "error": last_err, "conn_error": True}
    return {"success": False, "error": last_err or "step failed"}


def _fmt_err(e):
    """把 urllib3 的多层元组异常压成一句人能读的话。

    urllib3 抛的是 MaxRetryError(conn, reason)，str() 出来是
    "(<HTTPSConnection object at 0x...>, 'Connection to ... timed out')"，
    日志里几乎没法看，也容易让人误以为是自己代码报错。
    """
    import re as _re
    s = str(e)
    m = _re.search(r"'([^']*(?:timed out|refused|failed|error|reset|closed)[^']*)'",
                   s, _re.I)
    if m:
        return m.group(1)
    s = _re.sub(r'<[^>]*>', '', s)
    return ' '.join(s.split())[:160].strip(' ,()') or 'network error'


def _is_conn_error(e):
    """连接层失败（代理抖动 / TLS 握手超时 / DNS 挂）判定。

    这类错误重试是把 12s 放大成 N×12s，而外层 solve_chain 本来就有轮次重试。
    区分开的意义：网络问题必须能被上层识别成 transient，绝不能当成"票无效"，
    也绝不能拿去猜 service —— 否则就是白烧一轮 + 报一个假的错误原因。
    """
    s = str(e).lower()
    return any(k in s for k in (
        'timed out', 'timeout', 'connection', 'proxy', 'ssl',
        'tunnel', 'remote end closed', 'name resolution',
        'newconnectionerror', 'connectionerror', 'read timeout',
    ))


HTTP_DEADLINE = 11.0        # 外层墙钟上限（略大于 STEP_TIMEOUT.total=10，内层先触发，这是兜底）


def get_json(path_qs, retries=1, sleep=0.3, timeout=None, deadline=None):
    """GET 一个 API，返回 dict。

    连接层失败**不重试**（见 _is_conn_error），HTTP 5xx 才重试一次。
    整个调用包在 call_with_deadline 里，墙钟到点必返回 —— 内层 socket 超时
    在这条链路上不可信（实测能卡 90s+，见 call_with_deadline 注释）。
    """
    url = f"{AUTH_API}/{path_qs}"
    hdr = {'User-Agent': rand_ua(), 'Accept': 'application/json'}
    last_err = None
    for attempt in range(retries + 1):
        def _once(_u=url, _h=hdr, _t=timeout):
            return step_pool.request('GET', _u, redirect=False,
                                     timeout=_t or STEP_TIMEOUT, headers=_h)

        r, err = call_with_deadline(_once, deadline or HTTP_DEADLINE)
        if err:
            return {"success": False, "error": _fmt_err(err),
                    "transient": True, "conn_error": True}
        try:
            if r.status != 200:
                last_err = f"http {r.status}"
                # 5xx 是上游临时故障，值得再试一次；4xx 是语义错误，重试无意义
                if r.status >= 500 and attempt < retries:
                    time.sleep(sleep)
                    continue
                return {"success": False, "error": last_err, "transient": True}
            try:
                return json.loads(r.data)
            except Exception:
                last_err = "non-json response"
                if attempt < retries:
                    time.sleep(sleep)
                    continue
                return {"success": False, "error": last_err, "transient": True}
        except Exception as e:
            last_err = _fmt_err(e)
            if _is_conn_error(e) or attempt >= retries:
                # 连接层错误立刻返回并打标，让上层走"网络异常"而不是"票无效"
                return {"success": False, "error": last_err,
                        "transient": True, "conn_error": True}
            time.sleep(sleep)
    return {"success": False, "error": last_err or "get failed",
            "transient": True, "conn_error": _is_conn_error(last_err or "")}


def get_session_status(ticket, session=None):
    # GET /api/session/status
    return get_json(f"session/status?ticket={urllib.parse.quote(ticket)}")


def get_session_metadata(ticket, session=None):
    # GET /api/session/metadata
    return get_json(f"session/metadata?ticket={urllib.parse.quote(ticket)}")


INVALID_MARKERS = ('invalid payload', 'expired', 'not found', 'invalid session',
                     'invalid ticket', 'does not exist')


def check_ticket_valid(ticket, session=None):
    try:
        meta = get_session_metadata(ticket, session=session)
    except Exception:
        return True, None
    if not isinstance(meta, dict):
        return True, None
    if meta.get('success') is True:
        return True, None
    if meta.get('transient'):
        return True, None
    if meta.get('success') is False:
        msg = str(meta.get('message') or meta.get('error') or '').lower()
        if any(m in msg for m in INVALID_MARKERS):
            return False, str(meta.get('message') or meta.get('error') or 'invalid link')
        return True, None
    return True, None