const test = require('node:test');
const assert = require('node:assert/strict');
const { optionalSamplingParams, optionalResponseFormat } = require('../docker/pi_config.cjs');

test('温度仅在显式配置时发送，接受数值 0', () => {
  assert.equal(optionalSamplingParams({}), undefined);
  assert.equal(optionalSamplingParams({ PI_TEMPERATURE: '' }), undefined);
  assert.deepEqual(optionalSamplingParams({ PI_TEMPERATURE: '0' }), { temperature: 0 });
  assert.deepEqual(optionalSamplingParams({ PI_TEMPERATURE: '1.2' }), { temperature: 1.2 });
});

test('拒绝无效温度', () => {
  for (const value of ['NaN', '-0.1', '2.1', 'not-a-number']) {
    assert.throws(() => optionalSamplingParams({ PI_TEMPERATURE: value }), /PI_TEMPERATURE/);
  }
});

test('结构化输出格式默认关闭且可按环境变量启用', () => {
  const schema = { type: 'object', properties: { ok: { type: 'boolean' } } };
  assert.equal(optionalResponseFormat({}, schema), undefined);
  assert.deepEqual(optionalResponseFormat({ PI_RESPONSE_FORMAT: 'json_object' }, schema),
    { response_format: { type: 'json_object' } });
  assert.deepEqual(optionalResponseFormat({ PI_RESPONSE_FORMAT: 'json_schema' }, schema),
    { response_format: { type: 'json_schema', json_schema: {
      name: 'eight_quality_audit_result', strict: true, schema,
    } } });
});

test('拒绝未知结构化输出模式', () => {
  assert.throws(() => optionalResponseFormat({ PI_RESPONSE_FORMAT: 'xml' }, {}), /PI_RESPONSE_FORMAT/);
});
