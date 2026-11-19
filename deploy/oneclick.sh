#!/usr/bin/env bash
# Delta 解卡服务 · 服务器一键部署（从 GitHub 拉取）
#
# 服务器上的一条命令（公有仓库）：
#   curl -fsSL https://raw.githubusercontent.com/<你的用户名>/<你的仓库>/main/deploy/oneclick.sh \
#     | sudo bash -s -- https://github.com/<你的用户名>/<你的仓库>.git
#
# 已经 clone 过、或私有仓库（推荐用服务器上的 deploy key / token）：
#   sudo bash deploy/oneclick.sh                       # 自动读 origin
#   sudo GITHUB_TOKEN=ghp_xxx bash deploy/oneclick.sh https://github.com/u/r.git
#
# 做四件事：拉代码 → 调 install.sh 装依赖/服务 → 记部署信息 → 可选装自动更新定时器。
# 幂等：重复执行只更新到远端 HEAD，不会动缓存、学习状态、重试队列。
set -euo pipefail

log()  { printf '\033[1;36m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[!]\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m[x]\033[0m %s\n' "$*" >&2; exit 1; }

URL="${1:-${DELTA_REPO:-}}"
BRANCH="${DELTA_BRANCH:-main}"
REF="${DELTA_REF:-}"                       # 可钉死到 tag / commit
SRC_DIR="${DELTA_SRC_DIR:-/opt/delta-src}" # GitHub 克隆目录
APP_DIR="${DELTA_APP_DIR:-/opt/delta}"     # 服务运行目录
ENV_DIR="${DELTA_ENV_DIR:-/etc/delta}"
PORT="${DELTA_PORT:-2233}"
GH_TOKEN="${GITHUB_TOKEN:-${DELTA_GIT_TOKEN:-}}"
DRY_RUN="${DRY_RUN:-0}"
AUTO_UPDATE="${DELTA_AUTO_UPDATE:-0}"
SKIP_PREFLIGHT="${SKIP_PREFLIGHT:-0}"
SKIP_DEPS="${SKIP_DEPS:-0}"

export GIT_TERMINAL_PROMPT=0 GIT_SSH_COMMAND="${GIT_SSH_COMMAND:-ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new}"

# token 只在进程内用，绝不写进 origin / 配置文件 / 日志。
# Bearer 头给 GitHub API 风格的鉴权，insteadOf 给 git-http-backend（它只认基本鉴权）。
git_auth() {
    if [[ -n "$GH_TOKEN" ]]; then
        git -c http.extraHeader="AUTHORIZATION: bearer ${GH_TOKEN}" \
            -c "url.https://x-access-token:${GH_TOKEN}@github.com/.insteadOf=https://github.com/" \
            "$@"
    else
        git "$@"
    fi
}

repo_key() {
    printf '%s' "$1" \
      | sed -E 's#^(git@github\.com:|ssh://git@github\.com/|https?://github\.com/)##; s#\.git$##; s#/$##'
}

# 脚本直接在仓库里执行时可以省掉 URL 参数
if [[ -z "$URL" ]]; then
    HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
    URL="$(git -C "$HERE" remote get-url origin 2>/dev/null || true)"
fi
[[ -n "$URL" ]] || die '缺少仓库地址。用法：sudo bash oneclick.sh https://github.com/<用户>/<仓库>.git'
[[ "$URL" == *github.com* || "$URL" == file://* || "$URL" == /* ]] \
    || warn "仓库地址不像 GitHub：$URL（仍会尝试）"

if [[ "$DRY_RUN" != 1 && "$EUID" -ne 0 ]]; then
    die '请用 root 执行：sudo bash oneclick.sh <仓库地址>'
fi

# ---------- 1. git ----------
if ! command -v git >/dev/null 2>&1; then
    [[ "$DRY_RUN" = 1 ]] && die 'DRY_RUN 下不会装 git，请先自行安装'
    log '安装 git'
    if command -v apt-get >/dev/null 2>&1; then
        apt-get update -y && apt-get install -y git
    elif command -v dnf >/dev/null 2>&1; then
        dnf install -y git
    elif command -v yum >/dev/null 2>&1; then
        yum install -y git
    else
        die '找不到包管理器，请先安装 git'
    fi
fi

# ---------- 2. 拉取 ----------
mkdir -p "$(dirname "$SRC_DIR")"
NEED_CLONE=1
if [[ -d "$SRC_DIR/.git" ]]; then
    CUR="$(git -C "$SRC_DIR" remote get-url origin 2>/dev/null || true)"
    if [[ -n "$CUR" && "$(repo_key "$CUR")" == "$(repo_key "$URL")" ]]; then
        NEED_CLONE=0
    else
        BAK="${SRC_DIR}.bak.$(date +%Y%m%d%H%M%S)"
        warn "现有仓库不是同一个（$CUR），移到 $BAK 后重新拉取"
        mv "$SRC_DIR" "$BAK"
    fi
fi

if [[ "$NEED_CLONE" = 1 ]]; then
    log "克隆 $URL ($BRANCH) -> $SRC_DIR"
    CLONED=0
    if git_auth clone --depth 1 --branch "$BRANCH" "$URL" "$SRC_DIR"; then
        CLONED=1
    elif [[ -z "${DELTA_BRANCH:-}" && "$BRANCH" = main ]]; then
        warn 'main 分支不存在，改试 master'
        rm -rf "$SRC_DIR"
        git_auth clone --depth 1 --branch master "$URL" "$SRC_DIR"
        BRANCH=master
        CLONED=1
    fi
    [[ "$CLONED" = 1 ]] || die '克隆失败：确认仓库地址/分支，私有仓库请配 deploy key 或 GITHUB_TOKEN'
else
    log "已有仓库，拉取 $BRANCH 最新提交"
    git_auth -C "$SRC_DIR" fetch --depth 1 origin "$BRANCH"
    OLD="$(git -C "$SRC_DIR" rev-parse HEAD)"
    NEW="$(git -C "$SRC_DIR" rev-parse FETCH_HEAD)"
    # 浅克隆没有共同祖先，merge --ff-only 会报 unrelated histories。
    # 源码目录只是检出，运行时状态都在 /opt/delta 和 /var/lib/delta，
    # 所以直接对齐远端 HEAD 是安全的，也是浅克隆更新的标准做法。
    if [[ "$OLD" != "$NEW" ]]; then
        git -C "$SRC_DIR" reset --hard -q "$NEW"
        git -C "$SRC_DIR" branch -f "$BRANCH" "$NEW" 2>/dev/null || true
        log "更新 ${OLD:0:12} -> ${NEW:0:12}"
    else
        log "代码已是最新 ($NEW)"
    fi
fi

if [[ -n "$REF" ]]; then
    log "钉死到提交 $REF"
    git_auth -C "$SRC_DIR" fetch --depth 1 origin "$REF"
    git -C "$SRC_DIR" checkout --detach FETCH_HEAD
fi

for f in delta.py server.py requirements.txt deploy/install.sh; do
    [[ -f "$SRC_DIR/$f" ]] || die "仓库里缺少 $f —— 上传时别忘了 deploy/ 目录"
done

HEAD_SHA="$(git -C "$SRC_DIR" rev-parse HEAD)"

# ---------- 3. 调安装脚本 ----------
log '调用 install.sh（保留缓存 / 学习状态 / 重试队列）'
env DELTA_SRC_DIR="$SRC_DIR" DELTA_APP_DIR="$APP_DIR" DELTA_ENV_DIR="$ENV_DIR" \
    DELTA_PORT="$PORT" SKIP_PREFLIGHT="$SKIP_PREFLIGHT" SKIP_DEPS="$SKIP_DEPS" \
    DRY_RUN="$DRY_RUN" bash "$SRC_DIR/deploy/install.sh"

# ---------- 4. 记部署信息 ----------
if [[ "$DRY_RUN" != 1 ]]; then
    mkdir -p "$ENV_DIR"
    cat > "$ENV_DIR/deploy.env" <<EOF
# 由 deploy/oneclick.sh 生成，update.sh / GitHub Actions 会读它
DELTA_REPO='$URL'
DELTA_BRANCH='$BRANCH'
DELTA_REF='$REF'
DELTA_SRC_DIR='$SRC_DIR'
DELTA_APP_DIR='$APP_DIR'
DELTA_ENV_DIR='$ENV_DIR'
DELTA_PORT='$PORT'
DEPLOY_HEAD='$HEAD_SHA'
DEPLOY_TIME='$(date -u +%Y-%m-%dT%H:%M:%SZ)'
EOF
    chmod 640 "$ENV_DIR/deploy.env"
fi

# ---------- 5. 可选：自动更新 ----------
if [[ "$AUTO_UPDATE" = 1 && "$DRY_RUN" != 1 ]]; then
    log '安装自动更新定时器（每 15 分钟检查一次，HEAD 没变不重启）'
    install -m 0755 "$SRC_DIR/deploy/update.sh" /usr/local/bin/delta-update
    install -m 0644 "$SRC_DIR/deploy/delta-update.service" /etc/systemd/system/delta-update.service
    install -m 0644 "$SRC_DIR/deploy/delta-update.timer" /etc/systemd/system/delta-update.timer
    systemctl daemon-reload
    systemctl enable --now delta-update.timer
fi

cat <<EOF

$(printf '\033[1;32m')从 GitHub 部署完成$(printf '\033[0m')

  仓库      $URL
  分支      $BRANCH
  提交      ${HEAD_SHA:0:12}
  源码目录  $SRC_DIR
  服务目录  $APP_DIR
  接口      http://127.0.0.1:$PORT/delta?url=<链接>
  健康      curl http://127.0.0.1:$PORT/healthz
  日志      journalctl -u delta -f

  以后更新：sudo delta-update
  或重新跑：sudo bash $SRC_DIR/deploy/oneclick.sh
EOF
