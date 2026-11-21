#!/bin/sh
# Git 只从本地只读凭据文件读取；不修改或删除用户的凭据文件。
if [ "${1:-}" = get ]; then
  exec git credential-store --file="${AUDIT_GIT_CREDENTIAL_FILE:-/run/secrets/git-credentials}" get
fi
exit 0
