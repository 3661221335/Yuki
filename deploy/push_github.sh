#!/usr/bin/env bash
# Delta 解卡服务 · 把当前目录上传到自己的 GitHub 仓库
#
# 用法（Windows Git Bash / macOS / Linux 都可以）：
#   bash deploy/push_github.sh https://github.com/<你的用户名>/<仓库>.git
#   GITHUB_TOKEN=ghp_xxx bash deploy/push_github.sh https://github.com/u/r.git
#   DRY_RUN=1 bash deploy/push_github.sh <url>     # 只看会提交哪些文件，不推
#
# 仓库要在 GitHub 上先建好（可以空仓库，别勾 README/LICENSE，免得首次要 pull）。
# 运行时产生的 key 缓存、学习状态、重试队列、账号池都已在 .gitignore 里，不会上传。
set -euo pipefail

URL="${1:-${DELTA_REPO:-}}"
BRANCH="${DELTA_BRANCH:-main}"
MSG="${DELTA_COMMIT_MSG:-更新 Delta 解卡服务 $(date +%Y-%m-%d\ %H:%M)}"
DRY_RUN="${DRY_RUN:-0}"
GH_TOKEN="${GITHUB_TOKEN:-${DELTA_GIT_TOKEN:-}}"

log()  { printf '\033[1;36m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[!]\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m[x]\033[0m %s\n' "$*" >&2; exit 1; }

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
[[ -f delta.py && -f deploy/oneclick.sh ]] || die "当前目录不像完整项目：$ROOT"
[[ -n "$URL" ]] || die '缺少仓库地址：bash deploy/push_github.sh https://github.com/<你>/<仓库>.git'

export GIT_TERMINAL_PROMPT=0
git_auth() {
    if [[ -n "$GH_TOKEN" ]]; then
        git -c http.extraHeader="AUTHORIZATION: bearer ${GH_TOKEN}" \
            -c "url.https://x-access-token:${GH_TOKEN}@github.com/.insteadOf=https://github.com/" \
            "$@"
    else
        git "$@"
    fi
}

# ---------- 1. 初始化 ----------
if [[ ! -d .git ]]; then
    log "初始化 git 仓库（分支 $BRANCH）"
    git init -q -b "$BRANCH" 2>/dev/null || { git init -q && git checkout -q -b "$BRANCH"; }
else
    log "已有 git 仓库，当前分支 $(git branch --show-current)"
fi

if git remote get-url origin >/dev/null 2>&1; then
    OLD="$(git remote get-url origin)"
    [[ "$OLD" = "$URL" ]] || warn "origin 从 $OLD 改成 $URL"
    git remote set-url origin "$URL"
else
    git remote add origin "$URL"
fi

# ---------- 2. 再确认一次敏感文件不会进库 ----------
cat >> .git/info/exclude <<'EOF'
# 运行时状态，本地排除（.gitignore 里也有一份，这里双保险）
.key_cache.json*
.link_source_state.json*
.delta_retryq.json*
.discord_accounts.json
.profiles/
.browser_profile/
delta.env
.env
EOF
# git 会对重复行去重吗？不会，但 exclude 重复无害；只在第一次追加标记
python3 - <<'PY' 2>/dev/null || true
p = '.git/info/exclude'
lines = open(p, encoding='utf-8', errors='replace').read().splitlines()
out, seen = [], set()
for line in lines:
    if line in seen and line.startswith(('.key_cache', '.link_source', '.delta_retryq',
                                        '.discord_', '.profiles', '.browser_', 'delta.env', '.env')):
        continue
    out.append(line); seen.add(line)
open(p, 'w', encoding='utf-8').write('\n'.join(out) + '\n')
PY

# 新装的 Git Bash 常常没配 user.name / user.email，提交会直接失败
if ! git config user.email >/dev/null 2>&1; then
    warn "未配置 git user.email，本仓库临时用 ${GIT_AUTHOR_EMAIL:-delta@localhost}"
    git config user.email "${GIT_AUTHOR_EMAIL:-delta@localhost}"
fi
if ! git config user.name >/dev/null 2>&1; then
    warn "未配置 git user.name，本仓库临时用 ${GIT_AUTHOR_NAME:-Delta Deploy}"
    git config user.name "${GIT_AUTHOR_NAME:-Delta Deploy}"
fi

# ---------- 3. 提交并推送 ----------
git add -A
if git diff --cached --quiet; then
    log '没有需要提交的变化'
    HEAD="$(git rev-parse HEAD 2>/dev/null || echo '(还没有提交)')"
else
    log "暂存文件："
    git diff --cached --name-status | head -40
    [[ "$DRY_RUN" = 1 ]] && { warn 'DRY_RUN，到此为止'; exit 0; }
    git commit -q -m "$MSG"
    HEAD="$(git rev-parse HEAD)"
    log "已提交 ${HEAD:0:12}"
fi

[[ "$DRY_RUN" = 1 ]] && { warn 'DRY_RUN，未推送'; exit 0; }

log "推送到 origin/$BRANCH"
git_auth push -u origin "$BRANCH"

cat <<EOF

$(printf '\033[1;32m')上传完成$(printf '\033[0m')

  仓库    $URL
  分支    $BRANCH
  提交    ${HEAD:0:12}
  服务器  curl -fsSL https://raw.githubusercontent.com/<你>/<仓库>/$BRANCH/deploy/oneclick.sh | sudo bash -s -- $URL

如果 GitHub Actions 里配了 SERVER_HOST / SERVER_SSH_KEY，push 之后服务器会自动更新。
EOF
