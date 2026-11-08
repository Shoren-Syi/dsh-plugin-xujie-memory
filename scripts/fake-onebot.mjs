// 假 OneBot 端：HTTP 动作接口 + WebSocket 事件推送（零依赖手写帧）
import { createServer } from 'node:http'
import { createHash } from 'node:crypto'

const PORT = Number(process.env.FAKE_PORT || 8890)
const TOKEN = process.env.FAKE_TOKEN || 'secret-token'
const ACTIONS = []
const sockets = new Set()

function accept(key) {
  return createHash('sha1').update(key + '258EAFA5-E914-47DA-95CA-C5AB0DC85B11').digest('base64')
}

function encodeFrame(text) {
  const payload = Buffer.from(text, 'utf8')
  const len = payload.length
  let header
  if (len < 126) {
    header = Buffer.from([0x81, len])
  } else if (len < 65536) {
    header = Buffer.alloc(4)
    header[0] = 0x81; header[1] = 126; header.writeUInt16BE(len, 2)
  } else {
    header = Buffer.alloc(10)
    header[0] = 0x81; header[1] = 127; header.writeBigUInt64BE(BigInt(len), 2)
  }
  return Buffer.concat([header, payload])
}

const server = createServer((req, res) => {
  const url = new URL(req.url, `http://127.0.0.1:${PORT}`)
  if (req.method === 'POST' && url.pathname.startsWith('/')) {
    const auth = req.headers.authorization || ''
    let body = ''
    req.on('data', (chunk) => { body += chunk })
    req.on('end', () => {
      const action = url.pathname.slice(1)
      let params = {}
      try { params = JSON.parse(body || '{}') } catch { /* keep empty */ }
      ACTIONS.push({ action, params, auth })
      if (TOKEN && auth !== `Bearer ${TOKEN}`) {
        res.writeHead(401, { 'content-type': 'application/json' })
        res.end(JSON.stringify({ status: 'failed', retcode: 403, data: null }))
        return
      }
      res.writeHead(200, { 'content-type': 'application/json' })
      res.end(JSON.stringify({ status: 'ok', retcode: 0, data: { message_id: 42 } }))
    })
    return
  }
  if (url.pathname === '/dump') {
    res.writeHead(200, { 'content-type': 'application/json' })
    res.end(JSON.stringify({ actions: ACTIONS, clients: sockets.size }))
    return
  }
  res.writeHead(404); res.end()
})

server.on('upgrade', (req, socket) => {
  const url = new URL(req.url, `http://127.0.0.1:${PORT}`)
  const key = req.headers['sec-websocket-key']
  const token = url.searchParams.get('access_token') || ''
  if (!key || (TOKEN && token !== TOKEN)) {
    socket.write('HTTP/1.1 401 Unauthorized\r\n\r\n')
    socket.destroy()
    return
  }
  socket.write([
    'HTTP/1.1 101 Switching Protocols',
    'Upgrade: websocket',
    'Connection: Upgrade',
    `Sec-WebSocket-Accept: ${accept(key)}`,
    '\r\n',
  ].join('\r\n'))
  sockets.add(socket)
  socket.on('close', () => sockets.delete(socket))
  socket.on('error', () => sockets.delete(socket))

  // 连上后推一条私聊消息，模拟机器人收到消息
  setTimeout(() => {
    socket.write(encodeFrame(JSON.stringify({
      post_type: 'message', message_type: 'private', self_id: 10001,
      user_id: 20002, message_id: 9001,
      message: [{ type: 'text', data: { text: '你好，看看你在不在' } }],
    })))
  }, 300)
  socket.on('data', () => { /* 客户端帧不解析：本测试只验证下行 */ })
})

server.listen(PORT, '127.0.0.1', () => {
  console.log(JSON.stringify({ ready: true, port: PORT }))
})

setTimeout(() => {
  console.log(JSON.stringify({ final: true, actions: ACTIONS, clients: sockets.size }))
  process.exit(0)
}, Number(process.env.FAKE_LIFE_MS || 15000))
