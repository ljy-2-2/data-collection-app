#!/usr/bin/env bash
# Commit project changes and push to the owner's GitHub repository.
set -euo pipefail

# Conda/SDK library paths can make Ubuntu's SSH load an incompatible libcrypto.
# Scope cleanup to this script; preserve HOME, SSH config and SSH_AUTH_SOCK.
unset LD_LIBRARY_PATH LD_PRELOAD
export GIT_SSH_COMMAND=/usr/bin/ssh
export GIT_SSH_VARIANT=ssh
readonly git_bin=/usr/bin/git

cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."

if [[ "$("$git_bin" branch --show-current)" != "main" ]]; then
    printf '%s\n' '请先切换到 main 分支，再执行此脚本。' >&2
    exit 1
fi
if [[ "$("$git_bin" remote get-url --push ljy)" != 'git@github.com:ljy-2-2/data-collection-app.git' ]]; then
    printf '%s\n' '远程 ljy 与指定 GitHub 仓库不符，未提交或推送。' >&2
    exit 1
fi

"$git_bin" add -A
if ! "$git_bin" diff --cached --quiet; then
    "$git_bin" commit -m "${1:-更新数据采集软件}"
fi
"$git_bin" push --set-upstream ljy main
