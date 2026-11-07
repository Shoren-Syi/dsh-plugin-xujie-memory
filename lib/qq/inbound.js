/** 在调用模型之前过滤 OneBot 消息，并保留真实发言人身份。
 *
 * 移植自 0.8 实验分支（`lib/qq-inbound.js`），逻辑未改：
 * 白名单、群聊需 @、去重窗口、长度上限——这些是"别让机器人被群消息刷爆"的底线。
 */
export class QQInbound {
  constructor({ selfId, privateAllow = [], groupAllow = [], requireMention = true }) {
    if (!/^\d+$/.test(String(selfId))) throw new TypeError('机器人 QQ 号不合法')
    this.selfId = String(selfId)
    this.privateAllow = new Set(privateAllow.map(String))
    this.groupAllow = new Set(groupAllow.map(String))
    this.requireMention = requireMention
    this.seen = new Map()
  }

  accept(event, now = Date.now()) {
    if (event?.post_type !== 'message' || !['private', 'group'].includes(event.message_type)) return null
    const actor = String(event.user_id ?? ''), messageId = String(event.message_id ?? '')
    if (!/^\d{1,20}$/.test(actor) || actor === this.selfId || !messageId || messageId.length > 100
      || !Array.isArray(event.message)
      || (event.self_id != null && String(event.self_id) !== this.selfId)) return null
    if (event.message.length > 128) throw new Error('QQ 消息片段过多，已忽略')
    const group = event.message_type === 'group' ? String(event.group_id ?? '') : ''
    if (event.message_type === 'group'
      ? !/^\d+$/.test(group) || !this.groupAllow.has(group)
      : !this.privateAllow.has(actor)) return null
    if (group && this.requireMention
      && !event.message.some(segment => segment?.type === 'at' && String(segment.data?.qq) === this.selfId)) return null
    const text = event.message.filter(segment => segment?.type === 'text' && typeof segment.data?.text === 'string')
      .map(segment => segment.data.text).join('').trim()
    if (!text) return null
    if (text.length > 2000) throw new Error('QQ 消息超过 2000 字，已忽略')
    const key = `${event.message_type}:${group || actor}:${messageId}`
    for (const [id, expires] of this.seen) { if (expires > now) break; this.seen.delete(id) }
    if (this.seen.has(key)) return null
    this.seen.set(key, now + 300000)
    if (this.seen.size > 10000) this.seen.delete(this.seen.keys().next().value)
    return { channel: event.message_type, conversation: group || actor, actor, text, messageId }
  }
}
