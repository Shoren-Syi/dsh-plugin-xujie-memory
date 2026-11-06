#!/usr/bin/env bash
# 海马体代理最小示例（curl）。先 hippo init && hippo serve。
set -e

BASE="${HIPPO_BASE_URL:-http://127.0.0.1:8787/v1}"
KEY="${DEEPSEEK_API_KEY:-sk-placeholder}"

echo "== 1) 说事实 =="
curl -s "$BASE/chat/completions" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $KEY" \
  -H "X-Hippo-Session: demo" \
  -d '{"model":"deepseek-chat","messages":[{"role":"user","content":"我叫张三，住在杭州，对花生过敏。"}]}'
echo
echo
echo "== 2) 问回来 =="
curl -s "$BASE/chat/completions" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $KEY" \
  -H "X-Hippo-Session: demo" \
  -d '{"model":"deepseek-chat","messages":[{"role":"user","content":"我住哪？过敏什么？"}]}'
echo
