// 在只读扫描容器里运行真实 Pi；模型由容器内的 HTTP 服务模拟。
const http = require('node:http');
const fs = require('node:fs');
const { spawn } = require('node:child_process');

const checklist = JSON.parse(fs.readFileSync('/workspace/context/checklist.json', 'utf8'));
const answer = JSON.stringify({
  inventory: { languages: ['Python'], frameworks: [], build_tools: [], file_count: 1, loc_estimate: 10, modules: [], checked_scope: '全部', unchecked: [] },
  items: checklist.items.map((item) => ({ id: item.id, result: 'PASS', evidence: ['fixture.txt:1'], risk: 'none', conclusion: 'ok', carried_over: false })),
  issues: [], need_info_list: [],
  dimension_notes: checklist.dimensions.map((dimension) => ({ dimension: dimension.id, conclusion: 'ok', key_analysis: '-' })),
  summary: { key_findings: [], top_risks: [], undetermined: '中文测试'.repeat(20_000) + 'AUDIT_MOCK_END' },
  self_check: [{ check: '64 项齐全', ok: true }],
});
let requests = 0;
let sawPrompt = false;
let sawAllowedTools = false;
let sawReadResult = false;
let sawAuth = false;
let sawOutsideBlocked = false;
let sawEnvBlocked = false;
let sawSecretSearch = false;
let leakedSecret = false;

function stream(res, deltas, finishReason) {
  res.writeHead(200, { 'Content-Type': 'text/event-stream', 'Cache-Control': 'no-cache' });
  for (const delta of deltas) {
    res.write(`data: ${JSON.stringify({ id: 'mock-id', object: 'chat.completion.chunk', created: 1, model: 'mock-model', choices: [{ index: 0, delta, finish_reason: null }] })}\n\n`);
  }
  res.end(`data: ${JSON.stringify({ id: 'mock-id', object: 'chat.completion.chunk', created: 1, model: 'mock-model', choices: [{ index: 0, delta: {}, finish_reason: finishReason }], usage: { prompt_tokens: 1, completion_tokens: 1, total_tokens: 2 } })}\n\ndata: [DONE]\n\n`);
}

function callTool(res, id, name, input) {
  stream(res, [{ role: 'assistant', tool_calls: [{ index: 0, id, type: 'function', function: { name, arguments: JSON.stringify(input) } }] }], 'tool_calls');
}

const server = http.createServer((req, res) => {
  if (req.method !== 'POST') {
    res.writeHead(200, { 'Content-Type': 'application/json' }).end(JSON.stringify({ object: 'list', data: [{ id: 'mock-model', object: 'model' }] }));
    return;
  }
  let body = '';
  req.setEncoding('utf8');
  req.on('data', (chunk) => { body += chunk; });
  req.on('end', () => {
    let payload;
    try { payload = JSON.parse(body); } catch { res.writeHead(400).end(); return; }
    requests++;
    sawAuth ||= req.headers.authorization === 'Bearer mock-key';
    sawPrompt ||= body.includes('AUDIT_ENTRYPOINT_TEST') && body.includes('name: eight-quality-code-audit');
    const names = (payload.tools || []).map((tool) => tool.function?.name).filter(Boolean);
    sawAllowedTools ||= names.includes('read') && names.every((name) => ['read', 'grep', 'find', 'ls'].includes(name));
    const toolMessages = (payload.messages || []).filter((message) => message.role === 'tool');
    sawReadResult ||= toolMessages.some((message) => message.tool_call_id === 'call_read_1' && JSON.stringify(message.content).includes('AUDIT_MOCK_READ_CONTENT'));
    sawOutsideBlocked ||= toolMessages.some((message) => message.tool_call_id === 'call_outside' && JSON.stringify(message.content).includes('只允许读取扫描目录'));
    sawEnvBlocked ||= toolMessages.some((message) => message.tool_call_id === 'call_env' && JSON.stringify(message.content).includes('非 .env 文件'));
    sawSecretSearch ||= toolMessages.some((message) => message.tool_call_id === 'call_grep');
    leakedSecret ||= body.includes('DONT_SEND_THIS_VALUE');
    if (requests === 1) {
      callTool(res, 'call_read_1', 'read', { path: '/workspace/repo/fixture.txt' });
    } else if (requests === 2) {
      callTool(res, 'call_outside', 'read', { path: '/proc/self/environ' });
    } else if (requests === 3) {
      callTool(res, 'call_env', 'read', { path: '/workspace/repo/.env' });
    } else if (requests === 4) {
      callTool(res, 'call_grep', 'grep', { pattern: 'PRIVATE_TOKEN', path: '/workspace/repo', glob: '.env' });
    } else {
      const deltas = [];
      for (let i = 0; i < answer.length; i += 2048) deltas.push({ content: answer.slice(i, i + 2048) });
      stream(res, deltas, 'stop');
    }
  });
});

server.listen(0, '127.0.0.1', () => {
  const child = spawn('/opt/audit/entrypoint.sh', [], {
    env: { ...process.env, OPENAI_BASE_URL: `http://127.0.0.1:${server.address().port}/v1`, OPENAI_API_KEY: 'mock-key', OPENAI_MODEL: 'mock-model', MAX_SESSION_TURNS: '10' },
  });
  let stdout = '';
  let stderr = '';
  child.stdout.setEncoding('utf8');
  child.stderr.setEncoding('utf8');
  child.stdout.on('data', (chunk) => { stdout += chunk; });
  child.stderr.on('data', (chunk) => { stderr += chunk; });
  child.on('error', (error) => { console.error(error); process.exitCode = 1; server.close(); });
  const timer = setTimeout(() => { console.error('Pi 入口测试超时'); child.kill('SIGKILL'); }, 180_000);
  child.on('close', (code) => {
    clearTimeout(timer);
    server.close();
    let complete = false;
    try { const events = JSON.parse(stdout); complete = Array.isArray(events) && events.at(-1)?.result === answer; } catch {}
    if (code !== 0 || !sawPrompt || !sawAllowedTools || !sawReadResult || !sawOutsideBlocked || !sawEnvBlocked || !sawSecretSearch || leakedSecret || !sawAuth || !complete) {
      console.error(`Pi 入口测试失败：exit=${code}, requests=${requests}, prompt=${sawPrompt}, tools=${sawAllowedTools}, read=${sawReadResult}, outside=${sawOutsideBlocked}, env=${sawEnvBlocked}, grep=${sawSecretSearch}, leaked=${leakedSecret}, auth=${sawAuth}, complete=${complete}, bytes=${Buffer.byteLength(stdout)}`);
      console.error(stderr);
      process.exitCode = 1;
      return;
    }
    fs.writeFileSync(`/workspace/test-results/${process.env.AUDIT_TEST_ARCH}.json`, stdout);
    console.log(`Pi 真实入口测试通过：只读范围、.env 排除、模型认证、前导短横线和 ${Buffer.byteLength(stdout)} 字节输出均正常`);
  });
});
