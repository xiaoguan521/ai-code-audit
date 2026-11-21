// 在扫描镜像内以真实入口启动 Qwen，模型由本地 HTTP 服务模拟。
const http = require('http');
const { spawn } = require('child_process');

const marker = 'AUDIT_ENTRYPOINT_TEST';
const answer = 'AUDIT_MOCK_OK';
let sawPrompts = false;

const server = http.createServer((req, res) => {
  if (req.method === 'GET') {
    res.writeHead(200, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify({ object: 'list', data: [{ id: 'mock-model', object: 'model' }] }));
    return;
  }
  let body = '';
  req.setEncoding('utf8');
  req.on('data', (chunk) => { body += chunk; });
  req.on('end', () => {
    let payload;
    try {
      payload = JSON.parse(body);
    } catch {
      res.writeHead(400).end();
      return;
    }
    if (body.includes(marker) && body.includes('name: eight-quality-code-audit')) {
      sawPrompts = true;
    }
    if (payload.stream) {
      const chunks = [
        { id: 'mock-id', object: 'chat.completion.chunk', created: 1, model: 'mock-model', choices: [{ index: 0, delta: { role: 'assistant', content: answer }, finish_reason: null }] },
        { id: 'mock-id', object: 'chat.completion.chunk', created: 1, model: 'mock-model', choices: [{ index: 0, delta: {}, finish_reason: 'stop' }], usage: { prompt_tokens: 1, completion_tokens: 1, total_tokens: 2 } },
      ];
      res.writeHead(200, { 'Content-Type': 'text/event-stream' });
      res.end(chunks.map((chunk) => `data: ${JSON.stringify(chunk)}\n\n`).join('') + 'data: [DONE]\n\n');
    } else {
      res.writeHead(200, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ id: 'mock-id', object: 'chat.completion', created: 1, model: 'mock-model', choices: [{ index: 0, message: { role: 'assistant', content: answer }, finish_reason: 'stop' }], usage: { prompt_tokens: 1, completion_tokens: 1, total_tokens: 2 } }));
    }
  });
});

server.listen(0, '127.0.0.1', () => {
  const entrypoint = process.argv[2] || '/opt/audit/entrypoint.sh';
  const child = spawn(entrypoint, [], {
    env: {
      ...process.env,
      OPENAI_BASE_URL: `http://127.0.0.1:${server.address().port}/v1`,
      OPENAI_API_KEY: 'mock-key',
      OPENAI_MODEL: 'mock-model',
      MAX_SESSION_TURNS: '1',
    },
  });
  let stdout = '';
  let stderr = '';
  child.stdout.on('data', (chunk) => { stdout += chunk; });
  child.stderr.on('data', (chunk) => { stderr += chunk; });
  child.on('error', (error) => {
    console.error(error);
    process.exitCode = 1;
    server.close();
  });
  const timer = setTimeout(() => {
    console.error('Qwen 入口测试超时');
    child.kill('SIGKILL');
  }, 180_000);
  child.on('close', (code) => {
    clearTimeout(timer);
    server.close();
    if (code !== 0 || !sawPrompts || !stdout.includes(answer)) {
      console.error(`Qwen 入口测试失败：exit=${code}, sawPrompts=${sawPrompts}`);
      console.error(stderr);
      console.error(stdout);
      process.exitCode = 1;
      return;
    }
    console.log('Qwen 真实入口测试通过：两个提示词和模拟模型请求均正常');
  });
});
