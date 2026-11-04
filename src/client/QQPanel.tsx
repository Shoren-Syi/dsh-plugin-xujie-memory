import { useEffect, useState } from 'react'
import { asFailure, type MemoryAdapter } from './memory-adapter.ts'

type Policy = { per_request: number; daily: number; monthly: number; input_price: number; output_price: number; cache_price: number | null; daily_calls: number }
type Schedule = { enabled: boolean; intervalMinutes: number; prompt: string; target?: string; channel?: string; visibility?: number }
type Settings = { schedule?: { proactive: Schedule; qzone: Schedule }; enabled: boolean; wsUrl: string; httpUrl: string; selfId: string; privateAllow: string[]; groupAllow: string[]; requireMention: boolean; provider: string; model: string; maxTokens: number }
type State = { configured: boolean; has_token: boolean; settings: Settings | null; budget: { configured: boolean; policy: Policy | null; activity_limits: Record<string, number>; today: number; month: number; pending: number } }
type Form = { ws: string; http: string; self: string; token: string; privateIds: string; groupIds: string; provider: string; model: string; maxTokens: string; perRequest: string; daily: string; monthly: string; dailyCalls: string; inputPrice: string; outputPrice: string; cachePrice: string; proactiveCalls: string; qzoneCalls: string; mention: boolean; clearToken: boolean; proactiveEnabled: boolean; proactiveChannel: string; proactiveTarget: string; proactiveInterval: string; proactivePrompt: string; qzoneEnabled: boolean; qzoneInterval: string; qzonePrompt: string; qzoneVisibility: string }

const empty: Form = { ws: '', http: '', self: '', token: '', privateIds: '', groupIds: '', provider: '', model: '', maxTokens: '512', perRequest: '', daily: '', monthly: '', dailyCalls: '', inputPrice: '', outputPrice: '', cachePrice: '', proactiveCalls: '1', qzoneCalls: '1', mention: true, clearToken: false, proactiveEnabled: false, proactiveChannel: 'private', proactiveTarget: '', proactiveInterval: '1440', proactivePrompt: '', qzoneEnabled: false, qzoneInterval: '1440', qzonePrompt: '', qzoneVisibility: '64' }
const yuan = (value: number | null | undefined) => value == null ? '' : String(value / 1_000_000)
const micro = (value: string) => Math.round(Number(value) * 1_000_000)
const ids = (value: string) => value.split(/[，,\s]+/).filter(Boolean)
function formOf(next: State): Form {
  const s = next.settings, p = next.budget.policy, limits = next.budget.activity_limits
  return { ...empty, ws: s?.wsUrl ?? '', http: s?.httpUrl ?? '', self: s?.selfId ?? '',
    privateIds: s?.privateAllow.join(', ') ?? '', groupIds: s?.groupAllow.join(', ') ?? '',
    provider: s?.provider ?? '', model: s?.model ?? '', maxTokens: String(s?.maxTokens ?? 512),
    perRequest: yuan(p?.per_request), daily: yuan(p?.daily), monthly: yuan(p?.monthly), dailyCalls: p ? String(p.daily_calls) : '',
    inputPrice: yuan(p?.input_price), outputPrice: yuan(p?.output_price), cachePrice: yuan(p?.cache_price),
    proactiveCalls: String(limits.proactive ?? 1), qzoneCalls: String(limits.qzone ?? 1), mention: s?.requireMention ?? true, proactiveEnabled: s?.schedule?.proactive.enabled ?? false, proactiveChannel: s?.schedule?.proactive.channel ?? 'private', proactiveTarget: s?.schedule?.proactive.target ?? '', proactiveInterval: String(s?.schedule?.proactive.intervalMinutes ?? 1440), proactivePrompt: s?.schedule?.proactive.prompt ?? '', qzoneEnabled: s?.schedule?.qzone.enabled ?? false, qzoneInterval: String(s?.schedule?.qzone.intervalMinutes ?? 1440), qzonePrompt: s?.schedule?.qzone.prompt ?? '', qzoneVisibility: String(s?.schedule?.qzone.visibility ?? 64) }
}

export function QQPanel({ adapter }: { adapter: MemoryAdapter }) {
  const [scopeId, setScopeId] = useState<string | null>(null)
  const [form, setForm] = useState<Form>(empty), [status, setStatus] = useState<State | null>(null)
  const [error, setError] = useState(''), [busy, setBusy] = useState(false)
  const set = (key: keyof Form, value: string | boolean) => setForm(current => ({ ...current, [key]: value }))
  const load = async (id: string) => {
    const next: State = JSON.parse(await adapter.qqGet(id))
    setStatus(next)
    setForm(formOf(next))
  }
  useEffect(() => {
    let alive = true
    setStatus(null); setError(''); setScopeId(null)
    void (async () => {
      const id = await adapter.qqEnsure()
      const next: State = JSON.parse(await adapter.qqGet(id))
      if (!alive) return
      setScopeId(id); setStatus(next); setForm(formOf(next))
    })().catch(e => { if (alive) setError(asFailure(e).reason || asFailure(e).message) })
    return () => { alive = false }
  }, [adapter])
  const save = async (enabled: boolean) => {
    if (!scopeId) return
    setBusy(true); setError('')
    try {
      const policy = { per_request: micro(form.perRequest), daily: micro(form.daily), monthly: micro(form.monthly), daily_calls: Number(form.dailyCalls), input_price: micro(form.inputPrice), output_price: micro(form.outputPrice), cache_price: form.cachePrice.trim() ? micro(form.cachePrice) : null }
      const policyFields = [form.perRequest, form.daily, form.monthly, form.dailyCalls, form.inputPrice, form.outputPrice]
      const hasPolicy = policyFields.every(value => value.trim())
      if (!hasPolicy && (status?.budget.configured || policyFields.some(value => value.trim()) || form.cachePrice.trim())) throw new Error('请完整填写预算、调用次数和模型价格')
      if (enabled && !hasPolicy && !status?.budget.configured) throw new Error('启用前请填写预算和模型价格')
      const payload = { enabled, wsUrl: form.ws, httpUrl: form.http, selfId: form.self, privateAllow: ids(form.privateIds), groupAllow: ids(form.groupIds), requireMention: form.mention, provider: form.provider, model: form.model, maxTokens: Number(form.maxTokens), ...(hasPolicy ? { policy } : {}), activityLimits: { proactive: Number(form.proactiveCalls), qzone: Number(form.qzoneCalls) }, schedule: { proactive: { enabled: form.proactiveEnabled, channel: form.proactiveChannel, target: form.proactiveTarget, intervalMinutes: Number(form.proactiveInterval), prompt: form.proactivePrompt }, qzone: { enabled: form.qzoneEnabled, intervalMinutes: Number(form.qzoneInterval), prompt: form.qzonePrompt, visibility: Number(form.qzoneVisibility) } }, ...(form.clearToken ? { accessToken: '' } : form.token ? { accessToken: form.token } : {}) }
      await adapter.qqSave(scopeId, JSON.stringify(payload)); await load(scopeId)
    } catch (e) { const failure = asFailure(e); setError(failure.reason || failure.message) }
    finally { setBusy(false) }
  }
  const field = (label: string, key: keyof Form, options: { type?: string; placeholder?: string } = {}) => <label className="xjm-qq-field"><span>{label}</span><input className="xjm-input" type={options.type ?? 'text'} step={options.type === 'number' ? 'any' : undefined} value={String(form[key])} placeholder={options.placeholder} onChange={e => set(key, e.target.value)} /></label>

  if (!status && !error) return <p className="xjm-empty-line">正在读取 QQ 设置…</p>
  return <section className="xjm-qq" aria-label="QQ 接入设置">
    <h2 className="xjm-h2">QQ 接入与费用</h2>
    <p className="xjm-note">{status?.settings?.enabled ? '已启用；连接状态请查看宿主日志。' : '当前未启用。'} 需要用户自行运行兼容 OneBot 的 QQ 协议端。</p>
    <div className="xjm-qq-grid">{field('WebSocket 地址', 'ws', { placeholder: 'ws://127.0.0.1:3001' })}{field('HTTP 地址', 'http', { placeholder: 'http://127.0.0.1:3000' })}{field('机器人 QQ 号', 'self')}{field('连接令牌', 'token', { type: 'password', placeholder: status?.has_token ? '已设置，留空保留' : '未设置' })}{field('允许的私聊 QQ 号', 'privateIds', { placeholder: '逗号分隔' })}{field('允许的群号', 'groupIds', { placeholder: '逗号分隔' })}</div>
    <label className="xjm-qq-check"><input type="checkbox" checked={form.mention} onChange={e => set('mention', e.target.checked)} />群聊需要 @ 机器人才回复</label>
    {status?.has_token && <label className="xjm-qq-check"><input type="checkbox" checked={form.clearToken} onChange={e => set('clearToken', e.target.checked)} />清除已保存的令牌</label>}
    <h3 className="xjm-h2">模型与预算</h3><p className="xjm-note">价格单位为元／百万 token；预算单位为元。模型请求前按最大上下文预留，实际用量未知时保留预留。</p>
    <div className="xjm-qq-grid">{field('模型提供方 ID', 'provider')}{field('模型 ID', 'model')}{field('每次最大输出 token', 'maxTokens', { type: 'number' })}{field('每天最多调用次数', 'dailyCalls', { type: 'number' })}{field('单次预算（元）', 'perRequest', { type: 'number' })}{field('每日预算（元）', 'daily', { type: 'number' })}{field('每月预算（元）', 'monthly', { type: 'number' })}{field('输入价格（元）', 'inputPrice', { type: 'number' })}{field('输出价格（元）', 'outputPrice', { type: 'number' })}{field('缓存输入价格（元，可留空）', 'cachePrice', { type: 'number' })}{field('主动发言每日上限', 'proactiveCalls', { type: 'number' })}{field('空间动态每日上限', 'qzoneCalls', { type: 'number' })}</div>
    {status?.budget.configured && <p className="xjm-note">今日占用 ¥{yuan(status.budget.today)} · 本月 ¥{yuan(status.budget.month)} · 未结算预留 ¥{yuan(status.budget.pending)}</p>}
    <h3 className="xjm-h2">主动任务</h3>
    <p className="xjm-note">仅在连接启用期间运行，首次触发需等待完整间隔；模型调用受上述预算和次数限制。空间接口能否使用取决于协议端。</p>
    <label className="xjm-qq-check"><input type="checkbox" checked={form.proactiveEnabled} onChange={e => set('proactiveEnabled', e.target.checked)} />定时主动发言</label>
    <div className="xjm-qq-grid"><label className="xjm-qq-field"><span>发言范围</span><select className="xjm-input" value={form.proactiveChannel} onChange={e => set('proactiveChannel', e.target.value)}><option value="private">私聊</option><option value="group">群聊</option></select></label>{field('QQ 号或群号（须在对应白名单）', 'proactiveTarget')}{field('间隔（分钟，至少 30）', 'proactiveInterval', { type: 'number' })}{field('发言内容要求', 'proactivePrompt')}</div>
    <label className="xjm-qq-check"><input type="checkbox" checked={form.qzoneEnabled} onChange={e => set('qzoneEnabled', e.target.checked)} />定时发布 QQ 空间动态</label>
    <div className="xjm-qq-grid">{field('间隔（分钟，至少 30）', 'qzoneInterval', { type: 'number' })}{field('动态内容要求', 'qzonePrompt')}<label className="xjm-qq-field"><span>动态可见范围</span><select className="xjm-input" value={form.qzoneVisibility} onChange={e => set('qzoneVisibility', e.target.value)}><option value="64">仅自己</option><option value="4">QQ 好友</option><option value="1">所有人</option></select></label></div>
    {error && <p className="xjm-inline-error" role="alert">{error}</p>}
    <div className="xjm-actions"><button type="button" className="xjm-btn" disabled={busy || !scopeId} onClick={() => void save(false)}>保存并停用</button><button type="button" className="xjm-btn xjm-btn-primary" disabled={busy || !scopeId} onClick={() => void save(true)}>保存并启用</button></div>
  </section>
}
