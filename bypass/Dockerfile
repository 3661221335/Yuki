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

EXPOSE 2233

# 自检写在 CMD 里不合适（会跑一次真实求解、拉长启动），
# 所以健康检查只看本地状态，不碰上游。写成单行，避免 Dockerfile 把
# 反斜杠续行吃掉导致命令拼错。
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:2233/healthz', timeout=4).status==200 else 1)"

CMD ["python", "server.py", "--host", "0.0.0.0", "--port", "2233", "--workers", "12"]
