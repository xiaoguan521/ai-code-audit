const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { spawn } = require('node:child_process');
const { PiEventCollector, PiOutputError } = require('./pi_events.cjs');

function positiveInteger(name, fallback) {
  const raw = process.env[name] || String(fallback);
  if (!/^[1-9]\d*$/.test(raw) || !Number.isSafeInteger(Number(raw))) {
    throw new PiOutputError(`${name} 必须是正整数`);
  }
  return Number(raw);
}

async function main() {
  const baseUrl = process.env.OPENAI_BASE_URL;
  const model = process.env.OPENAI_MODEL;
  if (!baseUrl || !model) throw new PiOutputError('需要 OPENAI_BASE_URL 和 OPENAI_MODEL');
  const prompt = fs.readFileSync('/workspace/context/prompt.md', 'utf8');
  const skill = fs.readFileSync('/opt/audit/skills/eight-quality-code-audit/SKILL.md', 'utf8');
  const schema = fs.readFileSync('/opt/audit/result.schema.json', 'utf8');
  const maxTurns = positiveInteger('MAX_SESSION_TURNS', 300);
  const agentDir = fs.mkdtempSync(path.join(os.tmpdir(), 'ai-audit-pi-'));
  try {
    const systemPath = path.join(agentDir, 'system.md');
    fs.writeFileSync(systemPath, `${skill}\n\n## 输出格式（批量模式强制）\n完成审查后，最终回复只输出一个符合下列 JSON Schema 的 JSON 对象，不要输出解释或 Markdown。\n${schema}`, { mode: 0o600 });
    const models = { providers: { 'audit-internal': {
      baseUrl,
      api: 'openai-completions',
      apiKey: '$OPENAI_API_KEY',
      models: [{ id: model, contextWindow: positiveInteger('PI_CONTEXT_WINDOW', 128000), maxTokens: positiveInteger('PI_MAX_OUTPUT_TOKENS', 16384) }],
    } } };
    fs.writeFileSync(path.join(agentDir, 'models.json'), JSON.stringify(models), { mode: 0o600 });
    const args = [
      '--mode', 'json', '--no-session', '--no-approve', '--no-extensions', '--no-mcp',
      '--no-skills', '--no-prompt-templates', '--no-context-files', '--offline',
      '--tools', 'read,grep,find,ls', '--provider', 'audit-internal', '--model', model,
      '--extension', '/opt/audit/pi_guard.mjs',
      '--append-system-prompt', systemPath,
    ];
    const child = spawn('pi', args, {
      env: { ...process.env, OPENAI_API_KEY: process.env.OPENAI_API_KEY || 'none', PI_CODING_AGENT_DIR: agentDir },
      stdio: ['pipe', 'pipe', 'inherit'],
    });
    const collector = new PiEventCollector(maxTurns, () => child.kill('SIGTERM'));
    let parseError = null;
    child.stdout.setEncoding('utf8');
    child.stdout.on('data', (chunk) => {
      if (parseError) return;
      try { collector.push(chunk); } catch (error) { parseError = error; child.kill('SIGTERM'); }
    });
    child.stdin.on('error', () => {}); // Pi 启动失败时可能提前关闭 stdin。
    child.stdin.end(prompt);
    const forwardSignal = () => child.kill('SIGTERM');
    process.once('SIGTERM', forwardSignal);
    let status;
    try {
      status = await new Promise((resolve, reject) => {
        child.once('error', reject);
        child.once('close', (code, signal) => resolve({ code, signal }));
      });
    } finally {
      process.removeListener('SIGTERM', forwardSignal);
    }
    if (collector.limitReached) collector.finish();
    if (parseError) throw parseError;
    if (status.code !== 0) throw new PiOutputError(`Pi 进程退出：${status.code ?? status.signal}`);
    const result = collector.finish();
    // 等待 stdout 实际写完，避免大 JSON 在管道中被截断。
    await new Promise((resolve, reject) => process.stdout.write(`${JSON.stringify([result])}\n`, (error) => error ? reject(error) : resolve()));
  } finally {
    fs.rmSync(agentDir, { recursive: true, force: true });
  }
}

main().catch((error) => {
  console.error(`扫描入口失败：${error.message}`);
  process.exitCode = error.exitCode || 1;
});
