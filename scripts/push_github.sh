#!/usr/bin/env bash
# Commit project changes and push to the owner's GitHub repository.
set -euo pipefail

cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."

if [[ "$(git branch --show-current)" != "main" ]]; then
    printf '%s\n' '请先切换到 main 分支，再执行此脚本。' >&2
    exit 1
fi
if [[ "$(git remote get-url --push ljy)" != 'git@github.com:ljy-2-2/data-collection-app.git' ]]; then
    printf '%s\n' '远程 ljy 与指定 GitHub 仓库不符，未提交或推送。' >&2
    exit 1
fi

git add -A
if ! git diff --cached --quiet; then
    git commit -m "${1:-更新数据采集软件}"
fi
git push --set-upstream ljy main
