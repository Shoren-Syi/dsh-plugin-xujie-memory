-- M44：让模型给事件标重要性（1~5）。
--
-- 为什么要有它："重点标注层"不能只按启发式权重排。什么东西重要，
-- 模型在抽取时看一眼就知道（"我妈住院了" vs "让我改个变量名"），
-- 让它在同一次抽取里顺手打分，比事后自己猜准得多，而且**不额外花钱**
-- （同一次调用的输出里多一个字段）。
--
-- 3 = 默认（普通日常）；4~5 = 决定/承诺/健康/钱/关系变动；1~2 = 寒暄与过程。
ALTER TABLE events ADD COLUMN importance INTEGER NOT NULL DEFAULT 3;

CREATE INDEX IF NOT EXISTS idx_events_importance
    ON events(session, importance DESC, weight DESC);
