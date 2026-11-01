import assert from 'node:assert/strict'
import { spawnSync } from 'node:child_process'
import { mkdtempSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join, resolve } from 'node:path'
import test from 'node:test'

test('删除事实后画像立即与状态重新对账', (t) => {
  const dir = mkdtempSync(join(tmpdir(), 'xujie-forget-test-'))
  t.after(() => rmSync(dir, { recursive: true, force: true }))
  const python = process.env.PYTHON || (process.platform === 'win32' ? resolve('runtime/win32-x64/python.exe') : 'python3')
  const script = `
from pathlib import Path
from xujie_memory.server import MemoryService
import sys
s = MemoryService(sys.argv[1], Path('specs'), auto_confirm=True, scope_mode='user')
try:
    s.turn('s1', '我不吃辣')
    hits = s.memory_search('s1', '不吃辣')['hits']
    assert hits, '记忆没有写入'
    assert '不吃辣' in s.context('s1', '今天吃什么')['context']
    assert s.memory_forget('s1', hits[0]['trace_id'])['erased']
    assert '不吃辣' not in s.context('s1', '今天吃什么')['context']
    assert '不吃辣' not in s.turn('s1', '你好')['context']
    assert s.memory_forget('s1', hits[0]['trace_id'])['already_erased']
finally:
    s.close()
`
  const result = spawnSync(python, ['-c', script, join(dir, 'memory.db')], {
    cwd: resolve('python'), encoding: 'utf8', env: { ...process.env, PYTHONDONTWRITEBYTECODE: '1' },
  })
  assert.equal(result.status, 0, `${result.stdout}\n${result.stderr}`)
})

