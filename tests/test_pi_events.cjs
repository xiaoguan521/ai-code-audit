const test = require('node:test');
const assert = require('node:assert/strict');
const { PiEventCollector, PiOutputError } = require('../docker/pi_events.cjs');

const encode = (...events) => events.map((event) => JSON.stringify(event) + '\n').join('');
const assistant = (text, stopReason = 'stop') => ({
  type: 'message_end', message: { role: 'assistant', stopReason, content: [{ type: 'text', text }] },
});

test('提取最终文本并保留大 JSON 和 Unicode 分隔符', () => {
  const collector = new PiEventCollector(3, () => assert.fail('不应触发轮次限制'));
  const answer = '前缀\u2028中文' + '审计结果'.repeat(40_000) + '结束';
  const data = encode({ type: 'session', id: 's1' }, { type: 'turn_start' }, assistant('中间结果', 'toolUse'),
    { type: 'turn_end', message: { stopReason: 'toolUse' } }, { type: 'turn_start' },
    assistant(answer), { type: 'agent_settled', aborted: false });
  for (let i = 0; i < data.length; i += 997) collector.push(data.slice(i, i + 997));
  const output = collector.finish();
  assert.equal(output.result, answer);
  assert.equal(output.session_id, 's1');
});

test('错误、模型长度限制和未完成的事件不会当作成功', () => {
  for (const [reason, expectedCode] of [['error', 1], ['length', 55]]) {
    const collector = new PiEventCollector(5, () => {});
    collector.push(encode({ type: 'turn_start' }, assistant('部分内容', reason), { type: 'agent_settled', aborted: false }));
    assert.throws(() => collector.finish(), (error) => error instanceof PiOutputError && error.exitCode === expectedCode);
  }
  const incomplete = new PiEventCollector(5, () => {});
  incomplete.push('{"type":"agent_settled"');
  assert.throws(() => incomplete.finish(), /不完整/);
});

test('轮次上限阻止继续调用工具，最后一轮正常完成允许通过', () => {
  let stopped = 0;
  const exceeded = new PiEventCollector(1, () => { stopped++; });
  exceeded.push(encode({ type: 'turn_start' }, assistant('', 'toolUse'), { type: 'turn_end', message: { stopReason: 'toolUse' } }));
  assert.equal(stopped, 1);
  assert.throws(() => exceeded.finish(), (error) => error.exitCode === 53);
  const complete = new PiEventCollector(1, () => assert.fail('不应停止'));
  complete.push(encode({ type: 'turn_start' }, assistant('ok'), { type: 'turn_end', message: { stopReason: 'stop' } }, { type: 'agent_settled' }));
  assert.equal(complete.finish().result, 'ok');
});
