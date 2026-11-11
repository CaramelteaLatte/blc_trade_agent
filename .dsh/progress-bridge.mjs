// The SDK exposes settled messages, but the runtime also publishes live frames.
// Forward only public Responses reasoning summaries and activity, never payloads.
import { appendFileSync } from 'node:fs';

export const name = 'onchain-progress';

export function apply(ctx) {
  const path = process.env.RESEARCH_PROGRESS_LOG;
  if (!path) return;
  const show = process.env.RESEARCH_SHOW_REASONING !== '0';
  const states = new Map();
  const secrets = [process.env.OPENAI_API_KEY, process.env.ALCHEMY_RPC_URL,
    process.env.ALCHEMY_RPC_URL?.split('/v2/')[1]].filter(Boolean);
  function write(message) {
    for (const secret of secrets) message = message.split(secret).join('[REDACTED]');
    message = message.replace(/[\u0000-\u0008\u000b-\u001f\u007f-\u009f]/g, '');
    try { appendFileSync(path, JSON.stringify({ message }) + '\n', { mode: 0o600 }); }
    catch { /* Progress must not interrupt a research request. */ }
  }
  function flush(state) {
    if (state.buffer.trim()) write('推理摘要：' + state.buffer.trim());
    state.buffer = '';
  }
  write(show ? '模型摘要进度通道已连接，等待接口返回公开摘要…' : '模型摘要已隐藏，继续显示工具进度。');
  ctx.on('agent/assistant-stream', ({ frame }) => {
    if (frame.type === 'start') {
      states.set(frame.attemptId, { buffer: '', count: 0, text: false });
      return;
    }
    const state = states.get(frame.attemptId);
    if (!state) return;
    if (frame.type === 'end') {
      flush(state);
      states.delete(frame.attemptId);
      return;
    }
    const chunk = frame.chunk;
    // In this project's openai-responses adapter reasoning-delta is populated
    // from response.reasoning_summary_text.delta. Encrypted reasoning is ignored.
    if (chunk.type === 'reasoning-delta' && show && state.count < 16384) {
      const text = chunk.text.slice(0, 16384 - state.count);
      state.count += text.length;
      state.buffer += text;
      if (/[\n。！？]$/.test(state.buffer) || state.buffer.length >= 240) flush(state);
    } else if (chunk.type === 'text-delta' && !state.text) {
      flush(state);
      state.text = true;
      write('模型正在生成回答（数值将在完成后核验）…');
    } else if (chunk.type === 'block-end') {
      flush(state);
    }
  }, { global: true });
}
