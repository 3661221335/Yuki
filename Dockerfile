FROM python:3.11-slim

# 中文日志 + 立即刷新，少两个上线后才发现的问题
ENV LANG=C.UTF-8 \
    PYTHONUNBUFFERED=1 \
    PYTHONIOENCODING=utf-8 \
    DELTA_CACHE_FILE=/data/keys.json

WORKDIR /app

# 依赖单独一层，改代码不用重装
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip \
 && pip install --no-cache-dir -r requirements.txt

COPY . .

# 缓存持久化：不挂卷的话容器一重建，缓存就没了
RUN mkdir -p /data && chmod 777 /data
VOLUME ["/data"]

# Render 会自己注入 PORT（通常是 10000），本地 docker run 没设时用 2233。
EXPOSE 2233

# 自检写在 CMD 里不合适（会跑一次真实求解、拉长启动），
# 所以健康检查只看本地状态，不碰上游。端口跟随 PORT。
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD sh -c "python -c \"import os,sys,urllib.request; p=os.environ.get('PORT','2233'); sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:'+p+'/healthz', timeout=4).status==200 else 1)\""

# 免费层 512MB 内存，默认 3 个线程；本地可设 DELTA_WORKERS 调大。
CMD ["sh", "-c", "exec python delta.py --host 0.0.0.0 --port \"${PORT:-2233}\" --workers \"${DELTA_WORKERS:-3}\""]
