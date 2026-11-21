import fs from 'node:fs';
import path from 'node:path';

// 被审查仓库是不可信输入；不加载它的扩展，也不让只读工具读取容器内其他位置。
export default function (pi) {
  const roots = ['/workspace/repo', '/workspace/context'].map((root) => fs.realpathSync(root));
  pi.on('tool_call', (event, ctx) => {
    if (!['read', 'grep', 'find', 'ls'].includes(event.toolName)) {
      return { block: true, reason: '只允许只读审计工具' };
    }
    const rawPath = event.input.path || '.';
    if (typeof rawPath !== 'string') {
      return { block: true, reason: '只允许读取扫描目录' };
    }
    // Pi 的内置工具会剥离路径开头的 @，必须按同样规则检查。
    const inputPath = rawPath.startsWith('@') ? rawPath.slice(1) : rawPath;
    if (inputPath.startsWith('~')) return { block: true, reason: '只允许读取扫描目录' };
    const requested = path.resolve(ctx.cwd, inputPath);
    let actual;
    try { actual = fs.realpathSync(requested); } catch { return { block: true, reason: '路径不存在或不可读取' }; }
    const isInside = roots.some((root) => actual === root || actual.startsWith(root + path.sep));
    const secretPath = [requested, actual].some((p) => p.split(path.sep).some((part) => part === '.env' || part.startsWith('.env.')));
    if (!isInside || secretPath) return { block: true, reason: '只允许读取扫描目录内的非 .env 文件' };
    return undefined;
  });
}
