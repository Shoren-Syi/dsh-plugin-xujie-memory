/** 用宿主已有的内核鉴权通道读写 QQ 预算账本。 */
export class QQBudgetLedger {
  constructor(request) {
    if (typeof request !== 'function') throw new TypeError('缺少内核请求通道')
    this.request = request
  }

  async reserve(scope, activity, inputLimit, outputLimit) {
    const result = await this.request('/qq/budget/reserve', {
      scope_id: scope, activity, input_limit: inputLimit, output_limit: outputLimit,
    })
    if (result?.ok !== true || typeof result.call_id !== 'string' || !result.call_id) throw new Error('预算预留未确认')
    return { scope, id: result.call_id }
  }

  async settle(call, inputTokens, outputTokens, cachedTokens) {
    const result = await this.request('/qq/budget/settle', {
      scope_id: call.scope, call_id: call.id, input_tokens: inputTokens,
      output_tokens: outputTokens, cached_tokens: cachedTokens,
    })
    if (result?.ok !== true || !Number.isSafeInteger(result.amount)) throw new Error('预算结算未确认')
    return result.amount
  }
}
