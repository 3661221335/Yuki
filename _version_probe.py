#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""探测上游当前接受的 x-client-version。
原理: 用格式合法但不存在的假 ticket 发 step, 服务器先校验版本后校验票:
  - 版本太旧 -> "outdated client, please clear your browser cache"
  - 版本可用 -> 别的错误 (invalid payload / not found 等)
"""
import json
import random
import string
import sys
import urllib.parse
import urllib3

sys.path.insert(0, '.')
import auth_client as AUTH

pool = urllib3.PoolManager(num_pools=2, maxsize=16, retries=False,
                           timeout=urllib3.Timeout(connect=5.0, read=10.0))

letters = string.ascii_letters + string.digits
fake = ''.join(random.choice(letters) for _ in range(96))

def probe(version):
    ua = AUTH.FALLBACK_UA
    built = AUTH.build_meta_stream(fake, user_agent=ua)
    if built is None:
        return 'BUILD_FAIL'
    meta, stream = built
    url = f"{AUTH.AUTH_API}/session/step?ticket={urllib.parse.quote(fake)}&service=3"
    body = json.dumps({"captcha": None, "meta": meta, "stream": stream, "resolved": True}).encode()
    try:
        r = pool.request('PUT', url, body=body, redirect=False, headers={
            'User-Agent': ua,
            'Content-Type': 'application/json',
            'Accept': 'application/json',
            'x-client-name': 'platoboost webclient',
            'x-client-version': version,
        })
        txt = r.data.decode('utf-8', 'replace')[:160]
        flag = 'OUTDATED' if 'outdated client' in txt else 'OK?'
        return f'{flag:8s} {r.status} {txt}'
    except Exception as e:
        return f'ERR      {e}'

candidates = sys.argv[1:] or ['5.4.0', '5.4.1', '5.5.0', '5.6.0', '5.9.9', '6.0.0', '7.0.0', '9.9.9', '99.99.99']
for v in candidates:
    print(f'{v:10s} -> {probe(v)}', flush=True)
