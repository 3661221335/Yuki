#!/usr/bin/env bash
# Delta 解卡服务 · 增量更新（GitHub 拉取 → 有变化才重装重启）
#
#   sudo delta-update                    # oneclick.sh --auto-update 装好后的命令
#   sudo bash deploy/update.sh           # 手动
#   GITHUB_TOKEN=xxx sudo -E bash ...    # 私有仓库
#
# 设计要点：HEAD 没变就什么都不做（不 pip、不 preflight、不重启），
# 所以定时器每 15 分钟跑一次也不会打扰在途求解。
set -euo pipefail

log()  { printf '\033[1;36m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[!]\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m[x]\033[0m %s\n' "$*" >&2; exit 1; }

ENV_DIR="${DELTA_ENV_DIR:-/etc/delta}"
CFG="$ENV_DIR/deploy.env"
[[ -f "$CFG" ]] || die "找不到 $CFG，请先跑一次 oneclick.sh"
set +u
# shellcheck disable=SC1090
. "$CFG"
set -u

SRC_DIR="${DELTA_SRC_DIR:-/opt/delta-src}"
APP_DIR="${DELTA_APP_DIR:-/opt/delta}"
ENV_DIR="${DELTA_ENV_DIR:-/etc/delta}"
PORT="${DELTA_PORT:-2233}"
BRANCH="${DELTA_BRANCH:-main}"
REF="${DELTA_REF:-}"
GH_TOKEN="${GITHUB_TOKEN:-${DELTA_GIT_TOKEN:-}}"
DRY_RUN="${DRY_RUN:-0}"
ALLOW_RESET="${ALLOW_RESET:-0}"

export GIT_TERMINAL_PROMPT=0 GIT_SSH_COMMAND="${GIT_SSH_COMMAND:-ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new}"
git_auth() {
    if [[ -n "$GH_TOKEN" ]]; then
        git -c http.extraHeader="AUTHORIZATION: bearer ${GH_TOKEN}" \
            -c "url.https://x-access-token:${GH_TOKEN}@github.com/.insteadOf=https://github.com/" \
            "$@"
    else
        git "$@"
    fi
}

[[ -d "$SRC_DIR/.git" ]] || die "源码目录不是 git 仓库：$SRC_DIR"
[[ -f "$SRC_DIR/deploy/install.sh" ]] || die "$SRC_DIR 里没有 deploy/install.sh"

OLD="$(git -C "$SRC_DIR" rev-parse HEAD)"

if [[ -n "$REF" ]]; then
    log "拉取固定 ref $REF"
    git_auth -C "$SRC_DIR" fetch --depth 1 origin "$REF"
    TARGET="$(git -C "$SRC_DIR" rev-parse FETCH_HEAD)"
else
    log "检查远端 $BRANCH"
    git_auth -C "$SRC_DIR" fetch --depth 1 origin "$BRANCH"
    TARGET="$(git -C "$SRC_DIR" rev-parse FETCH_HEAD)"
fi

if [[ "$OLD" = "$TARGET" ]]; then
    log "已是最新 ${OLD:0:12}，不重装、不重启"
    exit 0
fi

log "对齐远端 ${OLD:0:12} -> ${TARGET:0:12}"
# 浅克隆（--depth 1）没有共同祖先，merge --ff-only 必然报 unrelated histories。
# 这里直接把源码检出对齐到远端 HEAD；缓存/学习状态/重试队列都不在这个目录里。
git -C "$SRC_DIR" reset --hard -q "$TARGET"
if [[ -n "$REF" ]]; then
    git -C "$SRC_DIR" checkout --detach -q "$TARGET"
else
    git -C "$SRC_DIR" branch -f "$BRANCH" "$TARGET" 2>/dev/null || true
fi

SKIP_PREFLIGHT="${SKIP_PREFLIGHT:-1}"
[[ "${RUN_PREFLIGHT:-0}" != 1 ]] || SKIP_PREFLIGHT=0

log "重装服务（跳过端到端自检）"
env DELTA_SRC_DIR="$SRC_DIR" DELTA_APP_DIR="$APP_DIR" DELTA_ENV_DIR="$ENV_DIR" \
    DELTA_PORT="$PORT" SKIP_PREFLIGHT="$SKIP_PREFLIGHT" SKIP_DEPS="${SKIP_DEPS:-0}" \
    DRY_RUN="$DRY_RUN" bash "$SRC_DIR/deploy/install.sh"

if [[ "$DRY_RUN" != 1 ]]; then
    sed -i "s|^DEPLOY_HEAD=.*|DEPLOY_HEAD='$TARGET'|; s|^DEPLOY_TIME=.*|DEPLOY_TIME='$(date -u +%Y-%m-%dT%H:%M:%SZ)'|" "$CFG"
    chmod 640 "$CFG"
fi

log "更新完成 ${TARGET:0:12}"
