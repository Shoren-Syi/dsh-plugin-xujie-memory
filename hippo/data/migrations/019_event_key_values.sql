-- M49：事件的关键值（数字/日期/金额/比例/专名）单独存一列。
--
-- 为什么不在摘要里硬挤：摘要 40~60 字，塞进所有关键值就变流水账；
-- 而检索和严格比对靠的是**字面**（"3:4" 不是"三比四"，"3月份" 不是 "3 月"）。
-- 单独存一列之后：
--   1. 渲染时优先给（token 很小，但命中率很高）；
--   2. 同时进关键词索引，数字/日期也能被检索到（原来纯数字会被当成噪声丢掉）。
ALTER TABLE events ADD COLUMN key_values TEXT NOT NULL DEFAULT '';

CREATE INDEX IF NOT EXISTS idx_events_has_values
    ON events(session) WHERE key_values <> '';
