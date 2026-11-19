#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""服务器上线自检。

在服务器上第一次跑之前，先执行：

    python preflight.py

它会按依赖 -> 编码 -> 出网方式 -> DNS -> TLS -> API -> 缓存 -> 版本 -> 端到端
的顺序逐项检查，每项给出耗时和结论。哪一环不行，直接告诉你改哪个环境变量。

退出码：
    0  全部关键项通过
    1  网络/上游不可达
    2  依赖缺失或环境不合格
"""

import base64
import os
import socket
import ssl
import sys
import time
import traceback
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

OK, FAIL, WARN = 'PASS', 'FAIL', 'WARN'
_results = []


def report(name, status, detail='', hint=''):
    _results.append((name, status, detail, hint))
    mark = {OK: '  [+]', FAIL: '  [-]', WARN: '  [!]'}[status]
    line = f'{mark} {name}'
    if detail:
        line += f'  {detail}'
    print(line, flush=True)
    if hint and status != OK:
        print(f'      -> {hint}', flush=True)


def step(name, fn, hint=''):
    t = time.time()
    try:
        detail, status = fn()
    except Exception as e:
        report(name, FAIL, f'{type(e).__name__}: {e}', hint)
        return False
    status = status or OK
    report(name, status, f'{detail}  ({time.time() - t:.2f}s)', hint)
    return status != FAIL


# --------------------------------------------------------------------------
# 1. 运行环境
# --------------------------------------------------------------------------
def chk_python():
    v = sys.version_info
    if v < (3, 8):
        return f'{v.major}.{v.minor}.{v.micro} 太低', FAIL
    return f'{v.major}.{v.minor}.{v.micro} ({sys.platform})', OK


def chk_deps():
    missing = []
    versions = []
    for mod, label in (('requests', 'requests'), ('urllib3', 'urllib3'),
                       ('Crypto', 'pycryptodome')):
        try:
            m = __import__(mod)
            versions.append(f'{label}={getattr(m, "__version__", "?")}')
        except Exception:
            missing.append(label)
    if missing:
        return '缺少 ' + ', '.join(missing), FAIL
    return '  '.join(versions), OK


def chk_optional():
    got = []
    for mod in ('uvloop', 'httptools', 'orjson', 'fake_useragent'):
        try:
            __import__(mod)
            got.append(mod)
        except Exception:
            pass
    if not got:
        return '都没装（功能不受影响，仅性能/兼容性略差）', WARN
    return '已装: ' + ', '.join(got), OK


def chk_encoding():
    import auth_client  # noqa: F401  （导入即修正 stdout 编码）
    enc = (sys.stdout.encoding or '?').lower()
    if 'utf' not in enc:
        return f'stdout.encoding={enc}', FAIL
    # 真打印一次中文，确认不会 UnicodeEncodeError
    print('      编码自检: 中文输出正常', flush=True)
    return f'stdout.encoding={enc}', OK


def chk_tz():
    return time.strftime('%Y-%m-%d %H:%M:%S %Z'), OK


# --------------------------------------------------------------------------
# 2. 出网
# --------------------------------------------------------------------------
def chk_egress():
    import auth_client as A
    info = A.proxy_info()
    return f"{info['mode']}  {info['note']}", OK


def chk_dns():
    hosts = ('auth.platorelay.com', 'api.platoboost.net')
    out = []
    for h in hosts:
        t = time.time()
        try:
            infos = socket.getaddrinfo(h, 443, proto=socket.IPPROTO_TCP)
            ips = sorted({i[4][0] for i in infos})
            out.append(f'{h}->{ips[0]}({len(ips)}个IP,{(time.time() - t) * 1000:.0f}ms)')
        except Exception as e:
            return f'{h} 解析失败: {e}', FAIL
    return '  '.join(out), OK


def _proxy_parts(proxy_url):
    """'http://user:pass@host:port' -> (host, port, 认证头)。"""
    u = urllib.parse.urlparse(proxy_url)
    host = u.hostname
    port = u.port or (443 if u.scheme == 'https' else 80)
    auth = ''
    if u.username:
        raw = f'{u.username}:{u.password or ""}'.encode()
        auth = 'Proxy-Authorization: Basic ' + base64.b64encode(raw).decode() + '\r\n'
    return host, port, auth


def _tls_once(host, timeout=8.0):
    """建立到 host:443 的 TLS 连接，返回耗时毫秒。

    【必须跟着实际出网方式走】这里如果无脑裸 socket 直连，在"必须走代理"的
    机器上必然超时 —— 而程序本身走代理是通的，于是自检报红、实际能跑，
    等于给了一个假警报。所以按 _PROXY 分两条路：
      直连: TCP -> TLS
      代理: TCP 到代理 -> CONNECT 隧道 -> TLS
    """
    import auth_client as A
    t = time.time()
    if A._PROXY:
        phost, pport, pauth = _proxy_parts(A._PROXY)
        sock = socket.create_connection((phost, pport), timeout=timeout)
        try:
            req = (f'CONNECT {host}:443 HTTP/1.1\r\nHost: {host}:443\r\n'
                   f'{pauth}\r\n')
            sock.sendall(req.encode())
            buf = b''
            while b'\r\n\r\n' not in buf:
                chunk = sock.recv(1024)
                if not chunk:
                    raise RuntimeError('代理提前关闭连接')
                buf += chunk
                if len(buf) > 8192:
                    raise RuntimeError('代理响应异常')
            status_line = buf.split(b'\r\n', 1)[0].decode('latin1')
            if ' 200' not in status_line:
                raise RuntimeError(f'代理拒绝隧道: {status_line}')
            ctx = ssl.create_default_context()
            with ctx.wrap_socket(sock, server_hostname=host):
                pass
        finally:
            try:
                sock.close()
            except Exception:
                pass
    else:
        ctx = ssl.create_default_context()
        with socket.create_connection((host, 443), timeout=timeout) as s:
            with ctx.wrap_socket(s, server_hostname=host):
                pass
    return (time.time() - t) * 1000


def _tls_probe(host, rounds=3):
    times = []
    last = None
    for _ in range(rounds):
        try:
            times.append(_tls_once(host))
        except Exception as e:
            last = e
    if not times:
        raise last or RuntimeError('全部握手失败')
    times.sort()
    return times


def chk_tls():
    import auth_client as A
    mode = '代理隧道' if A._PROXY else '直连'
    try:
        a = _tls_probe('auth.platorelay.com')
        b = _tls_probe('api.platoboost.net')
    except Exception as e:
        return f'{mode} 握手失败: {e}', FAIL
    detail = (f'{mode}  auth: {a[0]:.0f}/{a[len(a) // 2]:.0f}ms  '
              f'api: {b[0]:.0f}/{b[len(b) // 2]:.0f}ms (min/中位)')
    status = OK if max(a[0], b[0]) < 3000 else WARN
    return detail, status


def chk_http():
    """入口页取一次。重试 3 次：代理/边缘节点偶发抽风不能直接判死刑。"""
    import auth_client as A
    last = None
    for attempt in range(3):
        r, err = A.call_with_deadline(
            lambda: A.step_pool.request(
                'GET', A.APP_URL,
                headers={'User-Agent': A.FALLBACK_UA}, redirect=False),
            A.HTTP_DEADLINE)
        if err or r is None or r.status != 200:
            last = err or f'HTTP {getattr(r, "status", "?")}'
            time.sleep(0.4)
            continue
        body = r.data or b''
        if b'/assets/index-' not in body:
            last = f'HTTP 200 但页面结构变了（{len(body)}B，找不到 /assets/index-）'
            break
        return f'HTTP 200, {len(body)}B (第 {attempt + 1} 次尝试)', OK
    return f'3 次均失败: {last}', FAIL


def chk_metadata():
    """假 ticket 打一次 metadata：验证 API 语义通、能拿到 JSON。"""
    import auth_client as A
    r = A.get_session_metadata('Z' * 60)
    if not isinstance(r, dict):
        return f'非预期返回: {r!r}'[:80], FAIL
    if r.get('conn_error'):
        return f"连接失败: {r.get('error')}", FAIL
    msg = str(r.get('message') or r.get('error') or '')
    return f'返回正常（{msg[:40] or "无 message"}）', OK


# --------------------------------------------------------------------------
# 3. 本地状态
# --------------------------------------------------------------------------
def chk_cache():
    import server
    w = server._writable(server.CACHE_FILE)
    return (f'{server.CACHE_FILE} ({server.CACHE_SOURCE}, '
            f'已有 {len(server.key_cache)} 条)'), (OK if w else FAIL)


def chk_cache_roundtrip():

    """验证"复跑不出网"这条路真的通：写入 -> 新实例读回 -> 计时。

    这是复跑能从 6~16s 降到毫秒级的唯一前提。缓存写不进磁盘（权限、只读挂载）
    时，CLI 每次都要重走 metadata+step，表现就是"明明刚解过又要等十几秒"。

    用临时目录里的独立实例，绝不碰线上缓存文件，跑完自己清干净。
    """
    import shutil
    import tempfile
    import key_cache as KC

    tmpdir = tempfile.mkdtemp(prefix='delta-pf-')
    path = os.path.join(tmpdir, 'keys.json')
    try:
        c = KC.KeyCache(path=path, ttl=600)
        c.load()

        fake = 'x' * 441
        t0 = time.time()
        c.put(fake, 'FREE_preflight_probe', 0.0)
        saved = c.save_now()
        t_put = time.time() - t0

        # 换一个实例，强制从磁盘读 —— 只测内存字典等于没测
        c2 = KC.KeyCache(path=path, ttl=600)
        c2.load()
        t0 = time.time()
        got, hit, _ = c2.get(fake)
        t_hit = time.time() - t0

        if not saved:
            return f'落盘失败: {c.last_error}', FAIL
        if not hit or got != 'FREE_preflight_probe':
            return '写入后读不回来（落盘或合并逻辑有问题）', FAIL
        if t_hit > 0.5:
            return f'命中读取 {t_hit * 1000:.0f}ms 偏慢', WARN
        return f'写入 {t_put * 1000:.1f}ms, 磁盘读回 {t_hit * 1000:.2f}ms', OK
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def chk_link_gate():
    """链接通道是否可用。

    上游给 /public/start 加门禁时，这一项会变 FAIL 并直接告诉你是哪一种门禁 ——
    不用等到"生成链接失败"再去猜。
    """
    import link_source as LS
    rep = LS.diagnose(probe=True)
    gate = rep.get('gate')
    if gate in (LS.G_OPEN, None):
        pr = rep.get('probe') or {}
        layers = [p['name'] for p in (rep.get('providers') or []) if p['available']]
        return (f'正常（无门禁），实测 {float(pr.get("ms") or 0):.0f}ms，'
                f'可用层 {len(layers)} 个: {",".join(layers)}'), OK
    return (f'{gate}: {LS.GATE_HUMAN.get(gate, "")}'), FAIL


def chk_selfheal():
    """自愈引擎是否就绪。

    不发请求（真正的探测由"链接通道"那一项负责），只看三件事：
    能导入、动作表覆盖了所有失败类型、state 可读写。
    自愈要是没装好，上游一变就只能等人报错 —— 所以这一项必须绿。
    """
    import link_source as LS
    import selfheal as SH

    h = SH.Healer(interval=180, verbose=False)
    missing = [g for g in (LS.G_SCHEMA, LS.G_CF, LS.G_AUTH, LS.G_CAPTCHA,
                           LS.G_OFFLINE, LS.G_RATE) if not h._plan(g)]
    if missing:
        return f'动作表缺少这些失败类型: {",".join(missing)}', FAIL

    # state 可写性：自愈学到的字段名/sitekey/经验全靠它。
    # 用 last_ok 做探针，不碰 learned_fields —— 那个字段在 save_state 里是
    # "取并集"语义，写进去就再也删不掉了（自检会把自己的测试值留在线上）。
    before = LS.load_state()
    sentinel = {'provider': '__preflight__', 'ts': time.time(), 'ms': None}
    try:
        LS.save_state(dict(before, last_ok=sentinel))
        got = LS.load_state()
        writable = (got.get('last_ok') or {}).get('provider') == '__preflight__'
        LS.save_state(before)          # 原样写回，不留测试数据
    except Exception as e:
        return f'state 不可写: {type(e).__name__}: {e}', FAIL

    if not writable:
        return f'state 写入后读不回: {LS.STATE_FILE}', FAIL

    auto = SH.enabled()
    days = len((before.get('lessons') or []))
    return (f'就绪：{"自动修复开" if auto else "只观测"}，'
            f'冷却 {SH.default().min_repair_gap:.0f}s，'
            f'已积累 {days} 条经验，state={LS.STATE_FILE}'), OK


def chk_fastlane():
    """提速层自检：只读内存状态，不发请求。"""
    import fastlane as FL
    g = FL.governor()
    s = FL.snapshot()
    return (f'预算 {FL.SOLVE_BUDGET:.0f}s，自适应步间隔 {g.value():.2f}s'
            f'（{g.lo:.1f}~{g.hi:.1f}s），幂等请求对冲='
            f'{"开" if s["hedge"] else "关"}，TLS 预热'
            f'={"已完成" if FL.warm_info().get("ok") else "待启动"}'), OK


def chk_retryq():
    """失败重试队列：文件可写就算就绪，worker 由服务 lifespan 启动。"""
    import retryq as RQ
    q = RQ.default()
    good = q.save()
    detail = (f'队列文件 {q.path}，{len(q)} 条待解，'
              f'worker={"运行中" if q.running() else "服务启动后自动运行"}')
    return detail, (OK if good else FAIL)


def chk_version():
    import auth_client as A
    t = time.time()
    v = A.force_refresh_version()
    return f'{v}  (冷启动兜底 {A.FALLBACK_VERSION})', (OK if v else FAIL)


def chk_e2e():
    """真跑一条：生成链接 -> 求解 -> 必须出 key。"""
    import link_generator as LG
    import main
    url = LG.get_auth_link(6)
    ticket = url.split('d=')[-1]
    key, timer = main.solve_chain(ticket, verbose=False)
    if not key:
        reason = getattr(timer, 'fail_reason', None) or getattr(timer, 'invalid_reason', None)
        return f'未出 key（原因 {reason}）', FAIL
    return f'{key}  耗时 {timer.total():.1f}s', OK


def main_entry():
    print('=' * 62)
    print('Delta 解卡系统 · 服务器上线自检')
    print('=' * 62)

    print('\n[1] 运行环境')
    step('Python 版本', chk_python, '升级到 3.9+')
    step('必需依赖', chk_deps, 'pip install -r requirements.txt')
    step('可选加速库', chk_optional, 'pip install uvloop httptools orjson')
    step('标准输出编码', chk_encoding, '务必让 stdout 是 UTF-8，否则中文日志会抛异常')
    step('系统时间', chk_tz)

    print('\n[2] 出网链路')
    step('出网方式', chk_egress)
    step('DNS 解析', chk_dns, '检查 /etc/resolv.conf 或内网 DNS')
    step('TCP+TLS 握手', chk_tls, '握手慢说明链路质量差，可考虑换机房/加线路')
    step('入口页 HTTP', chk_http, '服务端要能访问 auth.platorelay.com')
    step('链接通道', chk_link_gate,
         '上游加门禁了。跑 python probe_gate.py 看是哪种，按提示配变量')
    step('metadata API', chk_metadata)

    print('\n[3] 本地状态')
    step('缓存文件', chk_cache, '给运行用户写权限，或设 DELTA_CACHE_FILE 指到可写位置')
    step('缓存读写往返', chk_cache_roundtrip,
         '复跑加速全靠它；这项失败等于每次都要重走十几秒的链路')
    step('客户端版本探测', chk_version, '若失败，手动把 FALLBACK_VERSION 调成当前版本')
    step('自愈引擎', chk_selfheal,
         '自愈没就绪时上游一变只能等人报错；检查 selfheal.py 是否随代码一起部署')
    step('提速层', chk_fastlane,
         '检查 fastlane.py 是否随代码部署；步间隔写 DELTA_STEP_GAP 可覆盖')
    step('失败重试队列', chk_retryq,
         '检查 DELTA_RETRYQ_FILE 指向的位置是否可写')

    print('\n[4] 端到端')
    step('生成链接并解出 key', chk_e2e, '前面都 PASS 还失败，说明上游协议又变了')

    fails = [r for r in _results if r[1] == FAIL]
    warns = [r for r in _results if r[1] == WARN]
    print('\n' + '=' * 62)
    print(f'结论: {len(_results) - len(fails) - len(warns)} 通过, '
          f'{len(warns)} 警告, {len(fails)} 失败')
    if fails:
        print('\n必须先解决的项:')
        for name, _, detail, hint in fails:
            print(f'  - {name}: {detail}')
            if hint:
                print(f'    建议: {hint}')
    elif warns:
        print('可以上线，警告项不影响功能。')
    else:
        print('全部通过，可以上线。')
    print('=' * 62)

    if fails:
        net_related = {'DNS 解析', 'TCP+TLS 握手', '入口页 HTTP', 'metadata API', '出网方式'}
        return 1 if any(f[0] in net_related for f in fails) else 2
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main_entry())
    except KeyboardInterrupt:
        print('\n中断', file=sys.stderr)
        sys.exit(130)
    except Exception:
        traceback.print_exc()
        sys.exit(2)
