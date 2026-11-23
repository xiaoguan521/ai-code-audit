const test = require('node:test');
const assert = require('node:assert/strict');
const { optionalSamplingParams } = require('../docker/pi_config.cjs');

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
