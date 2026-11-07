/** 向用户配置的 OneBot 服务发送私聊、群聊和 QQ 空间内容。
 *
 * 移植自 0.8 实验分支（`lib/qq-transport.js`）。空间接口按协议端版本可能不可用，
 * 调用方要把失败暴露给用户，不能吞掉。
 */
export class QQTransport {
  constructor(endpoint, token = '') {
    const url = new URL(endpoint)
    if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password || url.search || url.hash) {
      throw new TypeError('OneBot 地址必须是无凭据的 HTTP 或 HTTPS 地址')
    }
    this.endpoint = url.href.replace(/\/$/, '')
    this.token = token
  }

  async call(action, payload) {
    const response = await fetch(`${this.endpoint}/${action}`, {
      method: 'POST', redirect: 'error', signal: AbortSignal.timeout(10000),
      headers: { 'content-type': 'application/json', ...(this.token ? { authorization: `Bearer ${this.token}` } : {}) },
      body: JSON.stringify(payload),
    })
    if (!response.ok) throw new Error(`OneBot 请求失败：HTTP ${response.status}`)
    const body = await response.json()
    if (body.status !== 'ok' || body.retcode !== 0) throw new Error('OneBot 未确认操作成功')
    return body.data
  }

  async message(kind, id, content) {
    if (!['private', 'group'].includes(kind) || !/^\d+$/.test(String(id))
      || typeof content !== 'string' || !content.trim()) {
      throw new TypeError('QQ 消息目标或内容不合法')
    }
    const target = kind === 'private' ? { user_id: String(id) } : { group_id: String(id) }
    return this.call(kind === 'private' ? 'send_private_msg' : 'send_group_msg',
      { ...target, message: [{ type: 'text', data: { text: content } }] })
  }

  async publish(content, { visibility = 64, targets = [], images = [] } = {}) {
    if (typeof content !== 'string' || !Array.isArray(images) || (!content.trim() && !images.length)
      || ![1, 4, 16, 64, 128].includes(visibility) || !Array.isArray(targets)
      || !targets.every(id => /^\d+$/.test(String(id)))
      || !images.every(image => typeof image === 'string' && image.length > 0)
      || ([16, 128].includes(visibility) && !targets.length)) {
      throw new TypeError('空间动态内容或可见范围不合法')
    }
    return this.call('send_qzone_msg',
      { content, ugc_right: visibility, target_uins: targets.map(String), images })
  }
}
