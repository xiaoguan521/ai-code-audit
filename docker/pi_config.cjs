// OpenAI 兼容服务的采样参数；只有显式配置才发送，避免改变现有后端行为。
function optionalSamplingParams(env) {
  const raw = env.PI_TEMPERATURE;
  if (raw === undefined || raw.trim() === '') return undefined;
  const value = Number(raw);
  if (!Number.isFinite(value) || value < 0 || value > 2) {
    throw new Error('PI_TEMPERATURE 必须是 0 到 2 之间的数字');
  }
  return { temperature: value };
}

function optionalResponseFormat(env, schema) {
  const mode = (env.PI_RESPONSE_FORMAT || '').trim().toLowerCase();
  if (!mode || mode === 'off') return undefined;
  if (mode === 'json_object') return { response_format: { type: 'json_object' } };
  if (mode === 'json_schema') {
    if (!schema || typeof schema !== 'object') throw new Error('PI_RESPONSE_FORMAT=json_schema 需要有效 JSON Schema');
    return { response_format: { type: 'json_schema', json_schema: {
      name: 'eight_quality_audit_result', strict: true, schema,
    } } };
  }
  throw new Error('PI_RESPONSE_FORMAT 必须是 off、json_object 或 json_schema');
}

module.exports = { optionalSamplingParams, optionalResponseFormat };
