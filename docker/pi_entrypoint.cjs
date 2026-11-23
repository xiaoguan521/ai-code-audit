const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { spawn } = require('node:child_process');
const { PiEventCollector, PiOutputError } = require('./pi_events.cjs');
const { optionalSamplingParams, optionalResponseFormat } = require('./pi_config.cjs');

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
  const dimensionSkill = '/workspace/context/dimension.skill.md';
  const dimensionSchema = '/workspace/context/dimension.schema.json';
  const hasDimensionSkill = fs.existsSync(dimensionSkill);
  if (hasDimensionSkill !== fs.existsSync(dimensionSchema)) {
    throw new PiOutputError('分维度 Schema 和 Skill 必须同时存在');
  }
  const skill = fs.readFileSync(hasDimensionSkill ? dimensionSkill : '/opt/audit/skills/eight-quality-code-audit/SKILL.md', 'utf8');
  const schema = fs.readFileSync(hasDimensionSkill ? dimensionSchema : '/opt/audit/result.schema.json', 'utf8');
  // 在调用模型前就拒绝损坏的契约，避免带着无效 Schema 长时间运行。
  JSON.parse(schema);
  const maxTurns = positiveInteger('MAX_SESSION_TURNS', 300);
  const agentDir = fs.mkdtempSync(path.join(os.tmpdir(), 'ai-audit-pi-'));
  let collector = null;
  try {
    const systemPath = path.join(agentDir, 'system.md');
    fs.writeFileSync(systemPath, `${skill}\n\n## 输出格式（批量模式强制）\n完成审查后，最终回复只输出一个符合下列 JSON Schema 的 JSON 对象，不要输出解释或 Markdown。\n${schema}`, { mode: 0o600 });
    const samplingParams = {
      ...(optionalSamplingParams(process.env) || {}),
      ...(optionalResponseFormat(process.env, JSON.parse(schema)) || {}),
    };
    const models = { providers: { 'audit-internal': {
      baseUrl,
      api: 'openai-completions',
      apiKey: '$OPENAI_API_KEY',
      models: [{ id: model, contextWindow: positiveInteger('PI_CONTEXT_WINDOW', 128000), maxTokens: positiveInteger('PI_MAX_OUTPUT_TOKENS', 16384),
        ...(Object.keys(samplingParams).length ? { samplingParams } : {}) }],
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
    collector = new PiEventCollector(maxTurns, () => child.kill('SIGTERM'));
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
    // 失败时也返回已观察到的统计；不包含提示词或源码。
    if (collector) process.stderr.write(`AI_AUDIT_STATS:${JSON.stringify(collector.stats())}\n`);
    fs.rmSync(agentDir, { recursive: true, force: true });
  }
}

main().catch((error) => {
  console.error(`扫描入口失败：${error.message}`);
  process.exitCode = error.exitCode || 1;
});
