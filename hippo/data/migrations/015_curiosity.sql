-- M25.1：好奇队列。
--
-- 为什么不像 doubts 那样复用一张表：doubts 是"这句话我没听懂"，
-- curiosity 是"我想问你点什么"。前者由抽取产生、在对话里配额度问；
-- 后者由心跳产生、在对话之外找机会开口。两者的额度、静默、冷遇规则
-- 完全不同（doubts 每轮最多 1 条挂在回复里，curiosity 每天最多 3 条
-- 且要避开深夜）。塞进一张表，每个查询都得先写一句
-- `WHERE source = 'x'`，迟早会有人漏。
--
-- gap_ref 是去重键（doubt:12 / entity:3 / trait:7 / fact:9）：同一个缺口
-- 只问一次，问完换下一个。只按 question 文本去重的话，同一件事换个
-- 说法就能再问一遍。
CREATE TABLE IF NOT EXISTS curiosity (
    id          INTEGER PRIMARY KEY,
    session     TEXT    NOT NULL,
    question    TEXT    NOT NULL,
    gap_kind    TEXT    NOT NULL,             -- doubt / entity / trait / fact
    gap_ref     TEXT    NOT NULL,             -- 来源行，去重用
    impact      REAL    NOT NULL DEFAULT 0,
    urgency     REAL    NOT NULL DEFAULT 0,
    novelty     REAL    NOT NULL DEFAULT 0,
    priority    REAL    NOT NULL DEFAULT 0,   -- impact × urgency × novelty
    created     INTEGER NOT NULL,             -- 入队时刻
    spoken_at   INTEGER,                      -- 真正说出口的时刻（NULL = 还在队里）
    answered_at INTEGER,                      -- 用户接话的时刻
    -- pending  = 排队中，还没找到开口机会
    -- spoken   = 已经说给用户了，等回应
    -- answered = 用户接了这个话头
    -- ignored  = 说了没人接（连续两次就闭嘴）
    outcome     TEXT    NOT NULL DEFAULT 'pending'
);

CREATE INDEX IF NOT EXISTS idx_curiosity_queue
    ON curiosity(session, outcome, priority DESC, created);

CREATE INDEX IF NOT EXISTS idx_curiosity_spoken
    ON curiosity(session, spoken_at);
