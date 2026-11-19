#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""门禁诊断与监控。

两件事：

1. `--watch`  常驻监控上游门禁状态。状态一变就打日志、发 webhook，
              不用等用户报"解不了了"才知道上游改了东西。
2. `--find-sitekey`  上游一旦启用 Turnstile，把 sitekey 从页面/接口里挖出来。

平时查一眼当前状态，直接跑 `python probe_gate.py` 就行（委托给 link_source）。

退出码：0 正常 / 1 门禁或有异常 / 2 参数问题
"""
import json
import os
import re
import sys
import time

import link_source as LS


def _env(name, default=''):
    return (os.environ.get(name) or default).strip()


# ---------------------------------------------------------------- sitekey 猎取

def find_sitekey(verbose=True):
    """从三个地方找 Turnstile sitekey，找到就写进 state。

    顺序：页面 HTML -> 页面 JS bundle -> 实际请求的响应。
    上游启用 Turnstile 时，sitekey 必须交给前端渲染，所以必然出现在其中一处。
    """
    found = LS.known_sitekey()
    if found:
        if verbose:
            print(f'[+] 已有 sitekey（环境变量或历史学习）: {found}')
        return found

    s = LS._http()
    auth_host = LS.AUTH_HOST
    html = ''
    try:
        r = s.get(f'https://{auth_host}/', timeout=(8, 20),
                  headers={'User-Agent': ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                                          'AppleWebKit/537.36 (KHTML, like Gecko) '
                                          'Chrome/131.0.0.0 Safari/537.36'),
                           'Accept': 'text/html,*/*'})
        html = r.text or ''
        if verbose:
            print(f'[.] 页面 HTML {len(html)} 字符')
        sk = LS.learn_sitekey(html)
        if sk:
            print(f'[+] 在页面 HTML 里找到 sitekey: {sk}')
            return sk
    except Exception as e:
        if verbose:
            print(f'[-] 页面取不到: {type(e).__name__}: {str(e)[:80]}')

    # 从 HTML 里挑出所有脚本，逐个拉，扫 sitekey
    srcs = re.findall(r'<script[^>]+src="([^"]+)"', html)
    if not srcs:
        srcs = ['/assets/index-121bd418.js']       # 已知入口的兜底
    for src in srcs[:5]:
        url = src if src.startswith('http') else f'https://{auth_host}{src}'
        try:
            rr = s.get(url, timeout=(10, 40))
            if verbose:
                print(f'[.] bundle {src[:60]} {len(rr.text)} 字符')
            sk = LS.learn_sitekey(rr.text)
            if sk:
                print(f'[+] 在 bundle {src[:40]} 里找到 sitekey: {sk}')
                return sk
        except Exception as e:
            if verbose:
                print(f'[-] bundle 取不到 {src[:50]}: {type(e).__name__}')

    # 最后试一次真实请求：上游 403 时可能把 sitekey 放在挑战 body 里
    try:
        LS.PublicProvider().fetch(6, LS.hashlib_hwid())
    except LS.GateError as e:
        pass
    except Exception:
        pass
    sk = LS.known_sitekey()
    if sk:
        print(f'[+] 从接口响应里找到 sitekey: {sk}')
        return sk

    if verbose:
        print('[-] 没找到 sitekey。')
        print('    它可能只存在于渲染后的 DOM 里（Turnstile iframe 的 ?k= 参数）。')
        print('    办法一：浏览器打开 auth 页，F12 看 iframe 的 src，把 k= 后面那串')
        print('            填进 DELTA_TURNSTILE_SITEKEY')
        print('    办法二：python browser_solver.py "<url>" --visible，'
              '让页面自己渲染，再从 DOM 读')
    return ''


# ---------------------------------------------------------------- 告警

def notify(gate, human, detail=''):
    """状态变化时的通知。webhook 走 POST，失败不抛（不能因为通知让监控挂掉）。"""
    log = _env('DELTA_ALERT_LOG')
    line = f'{time.strftime("%Y-%m-%d %H:%M:%S")}\t{gate}\t{human}\t{detail[:300]}'
    if log:
        try:
            with open(log, 'a', encoding='utf-8') as f:
                f.write(line + '\n')
        except Exception:
            pass
    hook = _env('DELTA_ALERT_WEBHOOK')
    if hook:
        try:
            LS._http().post(hook, json={
                'event': 'delta_gate_change',
                'gate': gate,
                'gate_human': human,
                'detail': detail[:500],
                'at': time.strftime('%Y-%m-%d %H:%M:%S'),
            }, timeout=(6, 12))
        except Exception:
            pass


def _gate_severity(gate):
    """越靠前越严重，用来决定要不要立刻报。"""
    order = [LS.G_OPEN, LS.G_SCHEMA, LS.G_RATE, LS.G_CF, LS.G_CAPTCHA,
             LS.G_AUTH, LS.G_OFFLINE, LS.G_UNKNOWN]
    return order.index(gate) if gate in order else 99


# ---------------------------------------------------------------- watch

def watch(interval=300, once_check=True, verbose=True):
    """常驻监控。状态变化才输出，避免刷屏。"""
    print(f'[watch] 每 {interval}s 探一次，Ctrl+C 退出')
    print(f'[watch] API {LS.API_HOST}   AUTH {LS.AUTH_HOST}')
    prev = None
    first = True
    while True:
        st = LS.load_state()
        gate = st.get('gate')
        # 探测：轻量，只用 public 层
        try:
            t0 = time.time()
            LS.PublicProvider().fetch(6, LS.hashlib_hwid())
            gate = LS.G_OPEN
            detail = f'{(time.time()-t0)*1000:.0f}ms'
        except LS.GateError as e:
            gate = e.gate
            detail = str(e)[:200]
        except Exception as e:
            gate = LS.classify_exception(e)
            detail = f'{type(e).__name__}: {e}'[:200]

        human = LS.GATE_HUMAN.get(gate, gate)
        if prev is None or gate != prev:
            ts = time.strftime('%H:%M:%S')
            if first:
                print(f'[{ts}] 起始状态: {gate} ({human})  {detail}')
                first = False
            else:
                worse = _gate_severity(gate) > _gate_severity(prev)
                mark = '!!! 恶化' if worse else 'ok 恢复'
                print(f'[{ts}] {mark}: {prev} -> {gate} ({human})  {detail}')
                notify(gate, human, detail)
            prev = gate
        elif verbose:
            sys.stdout.write('.')
            sys.stdout.flush()

        # 状态写回 state，其它模块（link_source）会读它
        st = LS.load_state()
        if st.get('gate') != gate:
            st['gate'] = gate
            LS.save_state(st)
        time.sleep(interval)


# ---------------------------------------------------------------- main

def main():
    import argparse
    ap = argparse.ArgumentParser(
        description='上游门禁诊断与监控',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='''示例:
  %(prog)s                          # 查一次当前状态（含各层可用性）
  %(prog)s --no-probe --json        # 只看本地状态，不发请求
  %(prog)s --watch 300              # 每 5 分钟探一次，变化时告警
  %(prog)s --watch 60 --alert-log /var/log/delta/gate.log
  %(prog)s --find-sitekey           # 上游上了 Turnstile 后挖 sitekey
''')
    ap.add_argument('--no-probe', action='store_true', help='不实际发请求')
    ap.add_argument('--json', action='store_true', help='JSON 输出')
    ap.add_argument('--watch', type=int, metavar='SEC', help='常驻监控，间隔秒数')
    ap.add_argument('--find-sitekey', action='store_true', help='猎取 Turnstile sitekey')
    ap.add_argument('--service', type=int, default=6)
    a = ap.parse_args()

    if a.find_sitekey:
        sk = find_sitekey()
        if sk:
            print(f'\n[+] DELTA_TURNSTILE_SITEKEY={sk}')
            return 0
        return 1

    if a.watch:
        try:
            watch(interval=max(10, a.watch))
        except KeyboardInterrupt:
            print('\n[watch] 停止')
        return 0

    rep = LS.diagnose(probe=not a.no_probe, service=a.service)
    if a.json:
        print(json.dumps(rep, ensure_ascii=False, indent=2))
        return 0 if rep['gate'] in (LS.G_OPEN, None) else 1

    print('=' * 66)
    print('上游门禁诊断')
    print('=' * 66)
    print(f'API  {rep["api_host"]}')
    print(f'AUTH {rep["auth_host"]}')
    print(f'\n状态: {rep["gate"]}  ({rep["gate_human"]})')
    pr = rep['probe']
    if pr:
        if pr.get('ok'):
            print(f'  实测: 正常  {pr["ms"]}ms  {pr["url_head"]}')
        else:
            print(f'  实测: 失败  {pr["ms"]}ms  status={pr.get("status")}')
            print(f'  原文: {pr.get("msg")}')
    if rep['learned_fields']:
        print(f'  已学到必填字段: {", ".join(rep["learned_fields"])}')
    if rep['sitekey']:
        print(f'  已记录 sitekey: {rep["sitekey"]}')

    print('\n--- 退路各层 ---')
    for p in rep['providers']:
        mark = '可用' if p['available'] else '不可用'
        extra = '' if p['available'] else f'  -> {p["why_not"]}'
        stt = rep['stats'].get(p['name']) or {}
        s = f'  [成功{stt.get("ok",0)}/失败{stt.get("fail",0)}]' if stt else ''
        print(f'  {mark}  {p["name"]:8s} {p["note"]}{s}{extra}')

    print('\n--- 该做什么 ---')
    print('  ' + LS.ADVICE.get(rep['gate'] or LS.G_UNKNOWN, LS.ADVICE[LS.G_UNKNOWN]))
    ok = rep['gate'] in (LS.G_OPEN, None)
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
