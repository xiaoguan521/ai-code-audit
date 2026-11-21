#!/bin/sh
# Pi 扫描入口，保持原有模型环境变量和调度器输出接口。
set -eu
exec node /opt/audit/pi_entrypoint.cjs
