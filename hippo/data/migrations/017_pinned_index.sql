-- M27.1：钉住事件的查询每轮至少跑两次（`l2_search` 要保证钉住的必须进，
-- pack 的 C 段要渲染），而 `pinned = 1` 在 8000 条事件里通常只有个位数。
--
-- 没有这个索引时是 `WHERE session=? AND pinned=1` 走 session 索引扫全部
-- 8000 行：实测 3ms × 2 次 = 6ms，占 pack 的 40%。部分索引只存钉住的行，
-- 查询代价和"钉了几条"成正比，而不是和"记得多少事"成正比。
CREATE INDEX IF NOT EXISTS idx_events_pinned
    ON events(session, pinned) WHERE pinned = 1;
