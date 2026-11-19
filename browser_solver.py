#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""浏览器通道：真实浏览器打开 auth 链接，自动抓 key。

【为什么需要它】
主链路 auth_client.do_step() 是协议直连 —— 不加载页面、不执行 JS，所以
reCAPTCHA 脚本从未被注入，服务端也就不要求 captcha token（body 里
"captcha": null 即可通过）。这条路最快（1~7s），但代价是：它没有"人"的痕迹。

一旦真的用浏览器打开 auth 页面，reCAPTCHA 会被注入并开始对以下信号打分：
  navigator.webdriver / Canvas+WebGL 指纹 / 鼠标轨迹 / 滚动 / 停留时长 / cookie 历史
机房 IP + 全新 profile + 自动化特征 = 低分 -> 降级弹 v2 图片挑战。
这就是"打开浏览器就有谷歌验证"的全部原因。

【本文件的定位】
不是替代协议链路，而是给它兜底：浏览器只负责"过验证 + 拿到可信会话"，
key 一出现就抓走，写进和 CLI/HTTP 完全相同的缓存（key_cache.py）。
下次同链接复跑又是 0 秒。

【两种用法】

  1) 半自动（推荐，最省事）
     python browser_solver.py "<url>" --visible
     弹出浏览器，你手动点掉验证码。程序盯着 DOM 和所有 XHR，key 一出现立刻
     抓走并落库，你不用自己找、不用复制。验证过一次后 profile 会留着，
     下次大概率直接免验证。

  2) 全自动（打码平台，无人值守）
     python browser_solver.py "<url>" --auto-solve --capsolver KEY
     用 CapSolver 过 reCAPTCHA v2。v3 无感通过时不会触发。

【profile 是关键】
--profile 目录会持久化 cookie / localStorage / 指纹种子。同一个 profile 用得
越久，reCAPTCHA 的信任分越高，v2 图片挑战出现得越少（这是真实浏览器的正常
行为，不是漏洞）。反之每次开无痕 = 每次都是零信任新访客 = 必然弹挑战。
"""

import argparse
import json
import os
import re
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import auth_client as AUTH
import key_cache as KC

# FREE_ 前缀 + 32 位 hex。故意写宽一点（[0-9a-f]{28,40}），
# 免得上游哪天改了位数又抓不到。
KEY_RE = re.compile(r'\b(FREE_[0-9a-fA-F]{28,40})\b')

# 页面上可能出现的验证类型 -> (选择器, 人话名字)
CHALLENGE_PROBES = [
    ('iframe[src*="recaptcha/api2/anchor"]', 'reCAPTCHA v2 复选框'),
    ('iframe[src*="recaptcha/api2/bframe"]', 'reCAPTCHA v2 图片挑战'),
    ('iframe[src*="recaptcha/enterprise"]', 'reCAPTCHA Enterprise'),
    ('div.g-recaptcha', 'reCAPTCHA 容器'),
    ('iframe[src*="hcaptcha.com"]', '.hCaptcha'),
    ('iframe[src*="challenges.cloudflare.com"]', 'Cloudflare Turnstile'),
    ('iframe[src*="doublecounter"]', 'Double Counter'),
    ('iframe[src*="discord.com"]', 'Discord 授权'),
    ('a[href*="discord.com/oauth2"]', 'Discord OAuth 入口'),
]


class KeySniffer:
    """同时从三个来源抓 key：XHR 响应体、DOM 文本、localStorage。

    只用 DOM 会漏 —— key 常常只在一次 /api/session/status 的 JSON 里出现，
    页面上不一定渲染成文本。只用 XHR 也会漏 —— 有些版本直接把它写进页面。
    """

    def __init__(self):
        self.key = None
        self.source = None
        self.seen_urls = []

    def feed(self, text, source):
        if self.key or not text:
            return
        m = KEY_RE.search(text)
        if m:
            self.key = m.group(1)
            self.source = source

    def on_response(self, response):
        # 只关心自家的 JSON 接口，别去读图片/字体流
        try:
            ct = (response.headers or {}).get('content-type', '')
            if 'json' not in ct.lower() and 'text' not in ct.lower():
                return
            if response.url not in self.seen_urls:
                self.seen_urls.append(response.url)
            body = response.text()
            self.feed(body, f'XHR {response.url.split("?")[0]}')
        except Exception:
            # response.text() 对已关闭的响应会抛，正常现象，跳过
            pass


def detect_challenges(page):
    """返回当前页面上存在的验证类型（人话名字列表）。"""
    found = []
    for sel, name in CHALLENGE_PROBES:
        try:
            if page.query_selector(sel):
                found.append(name)
        except Exception:
            pass
    return found


def dump_debug(page, tag):
    """卡住时留证据：截图 + HTML。排障全靠它。"""
    d = os.path.join(HERE, 'browser_debug')
    os.makedirs(d, exist_ok=True)
    ts = time.strftime('%Y%m%d-%H%M%S')
    try:
        page.screenshot(path=os.path.join(d, f'{tag}-{ts}.png'), full_page=True)
    except Exception:
        pass
    try:
        with open(os.path.join(d, f'{tag}-{ts}.html'), 'w', encoding='utf-8') as f:
            f.write(page.content())
    except Exception:
        pass
    return d


def solve_recaptcha_v2(page, api_key, site_key=None, timeout=120):
    """用 CapSolver 过 reCAPTCHA v2，返回 True/False。

    只处理 v2 复选框/图片。v3 是无感的，不该走到这里 —— 如果走到了，说明
    分数低到被判成机器人，换 IP 或养 profile 比打码更有效。
    """
    try:
        import requests
    except Exception:
        print('[!] 需要 requests 才能调打码平台', flush=True)
        return False

    if not site_key:
        # 从页面里挖 sitekey
        try:
            html = page.content()
            m = re.search(r'data-sitekey="([^"]+)"', html) or \
                re.search(r'sitekey["\']?\s*[:=]\s*["\']([^"\']+)', html)
            site_key = m.group(1) if m else None
        except Exception:
            site_key = None
    if not site_key:
        print('[!] 找不到 reCAPTCHA sitekey，无法自动打码', flush=True)
        return False

    print(f'  [打码] 提交任务 sitekey={site_key[:12]}...', flush=True)
    try:
        r = requests.post('https://api.capsolver.com/createTask', json={
            'clientKey': api_key,
            'task': {'type': 'ReCaptchaV2TaskProxyLess',
                     'websiteURL': page.url, 'websiteKey': site_key},
        }, timeout=30).json()
        tid = r.get('taskId')
        if not tid:
            print(f'  [打码] 创建失败: {r}', flush=True)
            return False

        deadline = time.time() + timeout
        while time.time() < deadline:
            time.sleep(3)
            q = requests.post('https://api.capsolver.com/getTaskResult', json={
                'clientKey': api_key, 'taskId': tid}, timeout=30).json()
            st = q.get('status')
            if st == 'ready':
                token = q['solution']['gRecaptchaResponse']
                # 把 token 塞进 textarea 并触发页面自己的回调
                page.evaluate("""(t) => {
                    const ta = document.querySelector('#g-recaptcha-response');
                    if (ta) { ta.style.display = 'block'; ta.value = t; }
                    document.querySelectorAll('textarea[name="g-recaptcha-response"]')
                        .forEach(e => e.value = t);
                }""", token)
                print('  [打码] token 已注入', flush=True)
                return True
            if st == 'failed':
                print(f'  [打码] 失败: {q}', flush=True)
                return False
        print('  [打码] 超时', flush=True)
        return False
    except Exception as e:
        print(f'  [打码] 异常: {e}', flush=True)
        return False


def try_authorize(page, state):
    """在 Discord OAuth 授权页上点 Authorize。

    关键前提：profile 里已经登录过 Discord。这时授权页只显示一个
    「Authorize / 授权」按钮，点一下就走完，不会再要验证码。
    如果没登录过，页面会是登录表单（还带 hCaptcha）—— 那一步只能人工做一次。
    """
    try:
        url = page.url
    except Exception:
        return False
    if 'discord.com/oauth2/authorize' not in url:
        return False
    # 同一个 URL 只点一次，避免来回刷新时反复点
    if state.get('authorized') == url:
        return False
    for sel in ('button:has-text("Authorize")',
                'button:has-text("授权")',
                'button:has-text("Autorizar")',
                'div[role="button"]:has-text("Authorize")'):
        try:
            el = page.query_selector(sel)
            if el and el.is_visible():
                el.click()
                state['authorized'] = url
                print('  [oauth] 已点击 Authorize', flush=True)
                return True
        except Exception:
            pass
    return False


def run(url, visible=True, timeout=240, profile=None, auto_solve=False,
        capsolver=None, use_pool=False):
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print('[-] 没装 playwright。执行：\n'
              '    pip install playwright\n'
              '    python -m playwright install chromium   # 或者用本机 Edge，见下\n'
              '\n    只想复用系统 Edge（不下载 150MB）：装完 playwright 就能用 '
              '--channel msedge，无需 install chromium。', flush=True)
        return None

    ticket = AUTH.extract_ticket(url)

    # 账号池：按配额轮换，一个账号一个 profile
    pool = None
    acct = None
    if use_pool:
        try:
            import discord_pool
        except ImportError:
            print('[-] 缺 discord_pool.py', flush=True)
            return None
        pool = discord_pool.AccountPool()
        label, rec = pool.pick()
        if not label:
            s = pool.summary()
            print(f'[-] 今日 Discord 授权额度已用尽（合计剩余 {s["total_remaining"]} 次）。',
                  flush=True)
            print('    加账号：python discord_pool.py add <标签>', flush=True)
            print('    或等明天重置（DELTA_QUOTA_TZ=utc 对齐上游日界）', flush=True)
            return None
        acct = label
        profile = rec['profile']
        print(f'[*] 账号: {label}   今日剩余 {pool.remaining(label)}/{pool.limit}',
              flush=True)

    profile = profile or os.path.join(HERE, '.browser_profile')
    os.makedirs(profile, exist_ok=True)

    sniffer = KeySniffer()

    with sync_playwright() as p:
        # 用持久化 profile：cookie / localStorage / 指纹种子都留着。
        # 这是"验证过一次以后就很少再弹"的唯一办法 —— 每次开无痕等于每次都是
        # 全新零信任访客。
        launch_kw = dict(
            user_data_dir=profile,
            headless=not visible,
            args=['--disable-blink-features=AutomationControlled'],
            viewport={'width': 1280, 'height': 860},
            locale='en-US',
        )
        # 优先本机 Edge / Chrome，省掉下载浏览器
        ctx = None
        for ch in ('msedge', 'chrome', None):
            try:
                ctx = p.chromium.launch_persistent_context(
                    channel=ch, **launch_kw) if ch else \
                    p.chromium.launch_persistent_context(**launch_kw)
                break
            except Exception:
                continue
        if ctx is None:
            print('[-] 起不来浏览器，确认装了 Edge/Chrome 或执行 '
                  'python -m playwright install chromium', flush=True)
            return None

        # 抹掉最明显的自动化指纹。注意这只是降低噪声，改变不了 IP 信誉。
        ctx.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
            Object.defineProperty(navigator, 'languages', {get: () => ['en-US','en']});
            Object.defineProperty(navigator, 'plugins', {get: () => [1,2,3,4,5]});
            window.chrome = window.chrome || {runtime: {}};
        """)

        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.on('response', sniffer.on_response)

        print(f'[*] 打开: {url[:64]}...', flush=True)
        print(f'[*] profile: {profile}', flush=True)
        try:
            page.goto(url, wait_until='domcontentloaded', timeout=60000)
        except Exception as e:
            print(f'[!] 打开失败: {e}', flush=True)

        t0 = time.time()
        last_state = None
        solved_once = False
        oauth_state = {}
        login_prompted = False

        while time.time() - t0 < timeout:
            # 1) 先看有没有 key
            if not sniffer.key:
                try:
                    sniffer.feed(page.inner_text('body', timeout=2000), 'DOM 文本')
                except Exception:
                    pass
            if not sniffer.key:
                try:
                    ls = page.evaluate(
                        "() => JSON.stringify(Object.entries(localStorage))")
                    sniffer.feed(ls, 'localStorage')
                except Exception:
                    pass

            if sniffer.key:
                print(f'\n[+] 抓到 KEY（来源：{sniffer.source}）', flush=True)
                break

            # 2) 没有 key 就看卡在什么验证上
            ch = detect_challenges(page)
            state = ','.join(ch) if ch else '无验证组件'
            if state != last_state:
                if ch:
                    print(f'  [gate] 检测到: {state}', flush=True)
                last_state = state

            # 2.1) 已登录 Discord 时，授权页只需点一下
            try_authorize(page, oauth_state)

            # 2.2) 授权页里有登录表单 = 这个 profile 还没登录过
            #      这一步只能人工做一次（Discord 登录自带 hCaptcha），之后 cookie 一直有效
            if visible and not login_prompted:
                try:
                    if 'discord.com' in page.url and \
                            page.query_selector('input[name="email"], input[type="email"]'):
                        print('  [登录] 这个 profile 还没登录 Discord —— '
                              '请在浏览器里登录一次（只需一次，之后长期有效）', flush=True)
                        login_prompted = True
                except Exception:
                    pass

            # 3) 全自动模式：遇到 v2 就打码
            if auto_solve and capsolver and not solved_once and ch and \
                    any('v2' in c for c in ch):
                if solve_recaptcha_v2(page, capsolver):
                    solved_once = True

            if visible and ch and int(time.time() - t0) % 15 == 0:
                print(f'  [等待] {int(time.time()-t0)}s  请在上方浏览器里完成验证',
                      flush=True)
            time.sleep(0.5)

        if sniffer.key:
            key = sniffer.key
            dur = time.time() - t0
            # 写进与 CLI / HTTP 完全相同的缓存，下次复跑 0 秒
            c = KC.default()
            c.put(ticket, key, dur)
            c.save_now()
            print('\n' + '=' * 60, flush=True)
            print(f'[+] DELTA KEY: {key}', flush=True)
            print(f'[+] 耗时: {dur:.2f}s', flush=True)
            print(f'[+] 来源: {sniffer.source}', flush=True)
            print(f'[+] 已写入缓存，下次同链接复跑无需再开浏览器', flush=True)
            # 只有真拿到 key 才扣额度 —— 上游没给成功就不该记账
            if pool and acct:
                pool.consume(acct)
                print(f'[+] 账号 {acct} 已扣 1 次，今日剩 '
                      f'{pool.remaining(acct)}/{pool.limit}', flush=True)
            print('=' * 60, flush=True)
            try:
                ctx.close()
            except Exception:
                pass
            return key

        d = dump_debug(page, 'timeout')
        print(f'\n[-] {timeout}s 内没抓到 key。', flush=True)
        print(f'[-] 现场已保存: {d}', flush=True)
        if detect_challenges(page):
            print(f'[-] 仍卡在验证: {", ".join(detect_challenges(page))}', flush=True)
        try:
            ctx.close()
        except Exception:
            pass
        return None


def main():
    ap = argparse.ArgumentParser(
        description='浏览器通道：真实浏览器打开 auth 链接并自动抓 key',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='''示例:
  %(prog)s "<url>" --visible              # 半自动：你点验证，程序抓 key
  %(prog)s "<url>" --pool                # 从 Discord 账号池按配额轮换
  %(prog)s "<url>"                        # 无头模式（多半会撞验证）
  %(prog)s "<url>" --auto-solve --capsolver XXX
''')
    ap.add_argument('url', help='auth 链接')
    ap.add_argument('--visible', action='store_true', default=True,
                    help='显示浏览器窗口（默认开；人工过验证需要它）')
    ap.add_argument('--headless', action='store_true',
                    help='无头模式，不显示窗口')
    ap.add_argument('--timeout', type=int, default=240, help='总超时（秒）')
    ap.add_argument('--profile', default=None,
                    help='浏览器 profile 目录（默认 .browser_profile，留着复用信任度）')
    ap.add_argument('--auto-solve', action='store_true',
                    help='遇到 reCAPTCHA v2 时调用打码平台')
    ap.add_argument('--pool', action='store_true',
                    help='从 Discord 账号池按配额轮换（每号每天 3 次由 discord_pool.py 记账）')
    ap.add_argument('--capsolver', default=os.environ.get('CAPSOLVER_KEY'),
                    help='CapSolver API key（也可用环境变量 CAPSOLVER_KEY）')
    args = ap.parse_args()

    key = run(args.url, visible=not args.headless, timeout=args.timeout,
              profile=args.profile, auto_solve=args.auto_solve,
              capsolver=args.capsolver, use_pool=args.pool)
    sys.exit(0 if key else 1)


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        print('\n中断', file=sys.stderr)
        sys.exit(130)
