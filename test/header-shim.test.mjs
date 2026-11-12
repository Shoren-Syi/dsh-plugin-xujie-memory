// 头部工具条兜底：防止以后有人把这行 CSS 删掉
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

const src = readFileSync(new URL('../lib/client.js', import.meta.url), 'utf8')

test('client 注入 headerActions 换行兜底', () => {
  assert.match(src, /_headerActions/, '选择器应针对宿主的 headerActions')
  assert.match(src, /flex-wrap:\s*wrap/, '必须允许换行，否则窄窗口会溢出压住标题')
  assert.match(src, /text-overflow:\s*ellipsis/, '子项过长要省略，避免撑破')
  assert.match(src, /hippo-header-shim/, '要可重复注入去重')
})

test('兜底只在 headerActions 上生效，不改其它布局', () => {
  const block = src.slice(src.indexOf('HEADER_SHIM'), src.indexOf('function installHeaderShim'))
  const selectors = [...block.matchAll(/^([^{\n]+)\{/gm)].map((m) => m[1].trim())
  assert.ok(selectors.length > 0)
  for (const sel of selectors) {
    assert.match(sel, /_headerActions/, `不该出现非 headerActions 选择器：${sel}`)
  }
})
