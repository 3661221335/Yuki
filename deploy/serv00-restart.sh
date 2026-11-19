#!/bin/sh
# 换端口用：DELTA_PORT=18234 sh serv00-restart.sh
# 面板申请到新端口后跑一次即可（会自动改写 .env 并重启）
set -e
APP_DIR="${DELTA_APP_DIR:-$HOME/delta}"
[ -n "${DELTA_PORT:-}" ] || { echo "用法: DELTA_PORT=新端口 sh $0"; exit 1; }
cd "$APP_DIR"
sh "$APP_DIR/stop.sh" 2>/dev/null || true
# 只改 DELTA 端口行，不动其他配置
if grep -q '^DELTA_PORT=' "$APP_DIR/.env" 2>/dev/null; then
    sed -i '' "s|^DELTA_PORT=.*|DELTA_PORT=$DELTA_PORT|" "$APP_DIR/.env" 2>/dev/null \
      || sed -i "s|^DELTA_PORT=.*|DELTA_PORT=$DELTA_PORT|" "$APP_DIR/.env"
else
    echo "DELTA_PORT=$DELTA_PORT" >> "$APP_DIR/.env"
fi
echo "端口已设为 $DELTA_PORT，重新启动"
sh "$APP_DIR/start.sh"
sleep 3
echo "自检:"
curl -sS --max-time 8 "http://127.0.0.1:$DELTA_PORT/healthz" | head -c 300 || true
echo ""
