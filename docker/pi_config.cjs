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

module.exports = { optionalSamplingParams };
