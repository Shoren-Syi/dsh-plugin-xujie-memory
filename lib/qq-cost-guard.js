/** 为已绑定的 QQ 会话拦截每一次模型请求，未知价格或用量时保留预算。 */
export function guardQQStream({ llm, ledger, sessions, onError }) {
  if (typeof onError !== 'function') throw new TypeError('必须提供预算错误处理函数')
  return (options, next) => {
    const binding = sessions.get(String(options.sessionId ?? ''))
    if (!binding) return next()
    return (async function* () {
      if (options.provider !== binding.provider || options.model !== binding.model) throw new Error('QQ 会话的模型与预算配置不匹配')
      if (!Number.isSafeInteger(options.maxTokens) || options.maxTokens <= 0) throw new Error('模型输出上限未知，已停止调用')
      if (!Number.isSafeInteger(binding.maxTokens) || options.maxTokens > binding.maxTokens) throw new Error('模型输出超过 QQ 设置上限，已停止调用')
      const info = await llm.resolveModelInfo(options.provider, options.model, options.signal)
      const inputLimit = info?.context?.contextWindow
      if (!Number.isSafeInteger(inputLimit) || inputLimit <= 0) throw new Error('模型上下文上限未知，已停止调用')
      const call = await ledger.reserve(binding.scope, binding.activity, inputLimit, options.maxTokens)
      let usage = null
      try {
        for await (const chunk of next()) {
          if (chunk.type === 'usage') usage = chunk.usage
          yield chunk
        }
      } finally {
        const input = usage?.inputTokens, output = usage?.outputTokens, cached = usage?.cacheReadTokens ?? 0
        const unsupportedCache = (usage?.cacheWriteTokens ?? 0) !== 0
        if (unsupportedCache || ![input, output, cached].every(value => Number.isSafeInteger(value) && value >= 0)) {
          onError(new Error('模型用量不完整，预算预留未释放'))
        } else {
          try { await ledger.settle(call, input, output, cached) }
          catch (error) { onError(error) }
        }
      }
    })()
  }
}
