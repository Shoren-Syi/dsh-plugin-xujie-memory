-- ============================================================
-- 010_session_state.sql  ·  会话级 KV
--
-- 为什么需要它
-- ------------
-- 有些状态"跨轮但跨不了会话"：上一轮 pack 用了哪些关键词（M12 话题切换）、
-- 上一轮注入了哪几条记忆（M12 的"话题不变就复用"）、存疑池的冷却计数（M18）。
-- 它们既不该进 events（不是记忆），也不该进 metrics（不是计量）。
--
-- 为什么不放 turns：turns 是"对话原文"的主键，一行一轮，schema 由 M2 定死。
-- 往里面加列会让 M2 的写入路径变成"顺便维护一堆无关状态"，一处失败全轮失败。
--
-- 为什么 value 是 TEXT 不是 JSON：调用方自己决定序列化方式。
-- 用 JSON 列会引诱人往里面塞结构化对象，然后这张表就变成了第二套 schema。
-- ============================================================

CREATE TABLE session_state (
    session     TEXT    NOT NULL,
    key         TEXT    NOT NULL,
    value       TEXT    NOT NULL,
    updated_at  INTEGER NOT NULL,
    PRIMARY KEY (session, key)
);
