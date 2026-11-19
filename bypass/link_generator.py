#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import hashlib
import random
import time
import sys
import requests

API = "https://api.platoboost.net"
UA = "Platoboost Python Client/1.0"
SERVICES = {"android": 6, "ios": 8}
RETRIES = 4
RETRY_SLEEP = 1.5

_session = None


def _http():
    """复用一个 Session：keep-alive 省掉每次的 TLS 握手，也方便统一出网方式。

    出网方式必须和 auth_client 一致 —— 如果那边因为 DELTA_NO_PROXY=1 直连、
    这边还闷头走环境代理，会出现"链接生成了但解不了"这种最难查的组合。
    """
    global _session
    if _session is None:
        s = requests.Session()
        try:
            import auth_client as _a
            s.trust_env = _a._TRUST_ENV
        except Exception:
            pass
        s.headers.update({"User-Agent": UA, "Content-Type": "application/json"})
        _session = s
    return _session


def get_auth_link(service=6, hwid=None, only=None, verbose=False):
    """拿一条 auth 链接。

    主路已委托给 link_source 的多层链路：
        public -> cookie -> mirror -> captcha -> browser -> manual

    也就是说，上游哪天给 /public/start 加上凭据 / Turnstile / OAuth，
    这里不需要改，由 link_source 按实际状态挑一层接管。哪一层在生效、
    当前上游是什么门禁状态，随时可以 `python probe_gate.py` 看。

    only='public' 可以强制只用某一层（排障用）。
    """
    import link_source
    return link_source.fetch_url(service=service, identifier=hwid,
                                 only=only, verbose=verbose)


def _legacy_get_auth_link(service=6, hwid=None):
    """旧的单路实现，保留作参考与应急单点验证（等价于 public 层）。"""
    if hwid is None:
        hwid = hashlib.sha256(
            str(random.randint(0, 2 ** 32)).encode()
        ).hexdigest()

    last_err = None
    for attempt in range(RETRIES):
        try:
            r = _http().post(f"{API}/public/start",
                             json={"service": service, "identifier": hwid},
                             timeout=(8, 20))
            data = r.json()
            if data.get("success"):
                return data["data"]["url"]
            # 上游明确拒绝（非网络原因）就不重试了，重试也是同一个结果
            raise RuntimeError(f"API error: {data.get('message', 'unknown')}")
        except RuntimeError:
            raise
        except Exception as e:
            # 代理 502 / 隧道失败 / 读写超时：都是链路抖动，退避后重来
            last_err = e
            if attempt < RETRIES - 1:
                time.sleep(RETRY_SLEEP * (attempt + 1))
                continue
    raise RuntimeError(f"生成链接失败（已重试 {RETRIES} 次）: {last_err}")


def batch_links(count=5, service=6, interval=0.3):
    urls = []
    for i in range(count):
        try:
            url = get_auth_link(service)
            urls.append(url)
            print(f"[{i + 1}/{count}] {url[:64]}...", flush=True)
        except Exception as e:
            print(f"[{i + 1}/{count}] 失败: {e}", file=sys.stderr, flush=True)
        if i < count - 1:
            time.sleep(interval)
    return urls


if __name__ == '__main__':
    service = 6
    count = 1
    if len(sys.argv) >= 2:
        arg = sys.argv[1].lower()
        if arg in SERVICES:
            service = SERVICES[arg]
        else:
            try:
                count = int(arg)
            except ValueError:
                print(f"用法: python {sys.argv[0]} [android|ios] [数量]", file=sys.stderr)
                sys.exit(1)
    if len(sys.argv) >= 3:
        try:
            count = int(sys.argv[2])
        except ValueError:
            pass
    for url in batch_links(count, service):
        print(url)