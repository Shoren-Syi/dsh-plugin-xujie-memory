-- M23.1：自主动作要能分类查（"改文件"和"主动发消息"惹的祸不一样，
-- 懊悔记忆要按同类动作定向召回）。沿用 M22.3 giveup_level 的"加列不加串"：
-- 统计与检索都是 SQL 的事，塞进 action 文本里等于让每个查询做字符串解析。
ALTER TABLE ledger ADD COLUMN action_kind TEXT NOT NULL DEFAULT '';

-- 结算时刻。ts 是"动作发生"，settled_at 是"知道结果"，两者差几小时到几天。
-- 光有 settled 标志的话，事后查"这次反馈隔了多久才来"只能靠 action 猜。
ALTER TABLE ledger ADD COLUMN settled_at INTEGER;

-- 定向检索：同一会话、同一类动作、已结算的懊悔，按时间倒序。
CREATE INDEX idx_ledger_kind ON ledger(session, action_kind, settled, ts);
