"""Vercel Serverless 入口 —— 把 FastAPI 的 server.app 暴露给 Vercel。

Vercel 的 Python runtime 约定：入口文件顶层要有一个名为 `app` 的 ASGI 对象。
这里直接复用项目里已有的 server.app，不复制任何业务逻辑。

为什么在 import 之前设这些环境变量：
  - Serverless 每次请求可能是全新/隔离的进程，自愈线程、后台重试队列、
    版本监视这些"长期驻留"逻辑在这里没有意义，还会拖慢冷启动，所以关掉。
  - 缓存落到 /tmp（Vercel 唯一可写目录）；冷启动就没了，但热实例内仍命中。
"""
import os
import sys

# Serverless 里关掉长驻后台任务（只影响本进程，不改仓库其他部署方式）
os.environ.setdefault("DELTA_HEAL", "0")           # 不启动自愈线程
os.environ.setdefault("DELTA_HEAL_AUTO", "0")
os.environ.setdefault("DELTA_CACHE_FILE", "/tmp/delta-keys.json")
os.environ.setdefault("DELTA_RETRYQ_FILE", "/tmp/delta-retryq.json")
os.environ.setdefault("DELTA_LINK_STATE", "/tmp/delta-link-state.json")
os.environ.setdefault("PYTHONUNBUFFERED", "1")

# Vercel 会把 api/ 作为根，需要把项目根目录加进 sys.path 才能 import server
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
for p in (ROOT, HERE):
    if p not in sys.path:
        sys.path.insert(0, p)

from server import app  # noqa: E402

# Vercel 直接从模块拿 `app`，无需额外 handler。
