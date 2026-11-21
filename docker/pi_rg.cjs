#!/usr/bin/env node
// Pi 的 grep 会包含隐藏文件；最后追加排除规则，避免搜索结果直接带出 .env。
const { spawnSync } = require('node:child_process');
const args = process.argv.slice(2);
const endOfOptions = args.indexOf('--');
args.splice(endOfOptions < 0 ? args.length : endOfOptions, 0,
  '--glob', '!.env', '--glob', '!**/.env', '--glob', '!.env.*', '--glob', '!**/.env.*');
const result = spawnSync('/usr/bin/rg', args, { stdio: 'inherit' });
if (result.error) {
  console.error(result.error.message);
  process.exitCode = 1;
} else if (result.signal) {
  process.kill(process.pid, result.signal);
} else {
  process.exitCode = result.status ?? 1;
}
