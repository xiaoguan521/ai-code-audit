// 将 Pi 的 JSONL 事件收敛为调度器原有的最终结果格式。
class PiOutputError extends Error {
  constructor(message, exitCode = 1) {
    super(message);
    this.exitCode = exitCode;
  }
}

class PiEventCollector {
  constructor(maxTurns, onLimit) {
    this.maxTurns = maxTurns;
    this.onLimit = onLimit;
    this.pending = '';
    this.turns = 0;
    this.sessionId = null;
    this.lastMessage = null;
    this.settled = false;
    this.aborted = false;
    this.limitReached = false;
    this.toolCalls = 0;
    this.toolCallsByName = {};
    this.toolDurationMs = 0;
    this.timedToolCalls = 0;
    this.assistantMessages = 0;
    this.messagesWithUsage = 0;
    this.usageTotal = {};
  }

  limit() {
    if (!this.limitReached) {
      this.limitReached = true;
      this.onLimit();
    }
  }

  push(chunk) {
    this.pending += chunk;
    let newline;
    while ((newline = this.pending.indexOf('\n')) !== -1) {
      const line = this.pending.slice(0, newline).replace(/\r$/, '');
      this.pending = this.pending.slice(newline + 1);
      if (!line) continue;
      let event;
      try {
        event = JSON.parse(line);
      } catch {
        throw new PiOutputError('Pi 返回了无效的 JSONL 事件');
      }
      if (event.type === 'session') this.sessionId = event.id || null;
      if (event.type === 'turn_start' && ++this.turns > this.maxTurns) this.limit();
      if (event.type === 'tool_execution_start') {
        this.toolCalls++;
        const name = typeof event.toolName === 'string' ? event.toolName : 'unknown';
        this.toolCallsByName[name] = (this.toolCallsByName[name] || 0) + 1;
      }
      if (event.type === 'tool_execution_end' && Number.isFinite(event.durationMs) && event.durationMs >= 0) {
        this.toolDurationMs += event.durationMs;
        this.timedToolCalls++;
      }
      if (event.type === 'message_end' && event.message?.role === 'assistant') {
        this.lastMessage = event.message;
        this.assistantMessages++;
        const usage = event.message.usage;
        if (usage && typeof usage === 'object') {
          this.messagesWithUsage++;
          for (const [key, value] of Object.entries(usage)) {
            if (typeof value === 'number' && Number.isFinite(value) && value >= 0) {
              this.usageTotal[key] = (this.usageTotal[key] || 0) + value;
            }
          }
        }
      }
      if (event.type === 'turn_end' && this.turns >= this.maxTurns && event.message?.stopReason === 'toolUse') {
        this.limit();
      }
      if (event.type === 'agent_settled') {
        this.settled = true;
        this.aborted = Boolean(event.aborted);
      }
    }
  }

  stats() {
    return { turns: this.turns, tool_calls: this.toolCalls, tool_calls_by_name: this.toolCallsByName,
      tool_seconds_sum: this.timedToolCalls ? this.toolDurationMs / 1000 : null, timed_tool_calls: this.timedToolCalls,
      assistant_messages: this.assistantMessages,
      messages_with_usage: this.messagesWithUsage, usage_total: this.messagesWithUsage ? this.usageTotal : null };
  }

  finish() {
    if (this.limitReached) throw new PiOutputError(`Pi 超过 ${this.maxTurns} 轮，已停止`, 53);
    if (this.pending.trim()) throw new PiOutputError('Pi 的 JSONL 事件不完整');
    if (!this.settled || this.aborted || !this.lastMessage) {
      throw new PiOutputError('Pi 未正常完成扫描');
    }
    const msg = this.lastMessage;
    if (msg.stopReason === 'length') throw new PiOutputError('模型输出达到长度上限', 55);
    if (msg.stopReason !== 'stop') {
      throw new PiOutputError(`Pi 扫描失败：${String(msg.errorMessage || msg.stopReason || '无最终结果').slice(0, 500)}`);
    }
    const text = (msg.content || []).filter((part) => part.type === 'text').map((part) => part.text).join('\n');
    if (!text) throw new PiOutputError('Pi 没有返回最终文本');
    return { type: 'result', result: text, session_id: this.sessionId, usage: msg.usage || null, stats: this.stats() };
  }
}

module.exports = { PiEventCollector, PiOutputError };
