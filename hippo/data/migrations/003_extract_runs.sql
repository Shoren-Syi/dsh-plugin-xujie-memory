-- ============================================================
-- 003_extract_runs.sql  ·  抽取登记簿
--
-- 为什么单独一张表，而不是只靠 turns.extracted_at
-- ----------------------------------------------
-- extracted_at 只回答"抽没抽过"，回答不了"抽成什么样"。
-- 而 M5 之后必然会被问到的三个问题都需要后者：
--   1. 成功率到底多少？（degraded 的那几次不算成功）
--   2. 这个窗口为什么一条记忆都没出？（空数组 vs 解析失败 vs 全被锚定校验丢弃）
--   3. 钱花在哪了？（哪个 session、哪个窗口、几次重试）
--
-- 用日志也能回答，但日志按天切、会滚掉；这张表几万行才几 MB，能一直留着。
--
-- UNIQUE(session,start_idx,end_idx)：同一窗口重跑只留最后一次结果。
-- 不保留历史是有意的——重跑通常是为了修 bug，留一堆旧记录反而干扰判断。
-- ============================================================

CREATE TABLE extract_runs (
    id                INTEGER PRIMARY KEY,
    session           TEXT    NOT NULL,
    start_idx         INTEGER NOT NULL,
    end_idx           INTEGER NOT NULL,

    -- ok       正常抽出（可能 0 条，那是模型判断没有可记的）
    -- degraded 抽取失败，已写 raw 兜底事件
    -- skipped  窗口不完整/已抽过，没调模型
    -- error    连兜底都失败（库写不进去），需要人看
    status            TEXT    NOT NULL,
    n_events          INTEGER NOT NULL DEFAULT 0,
    n_facts           INTEGER NOT NULL DEFAULT 0,
    n_traits          INTEGER NOT NULL DEFAULT 0,
    n_plans           INTEGER NOT NULL DEFAULT 0,
    n_doubts          INTEGER NOT NULL DEFAULT 0,

    -- raw 逐字校验没过的条数。这个数长期偏高说明 prompt 在退化。
    verbatim_fail     INTEGER NOT NULL DEFAULT 0,
    attempts          INTEGER NOT NULL DEFAULT 1,
    prompt_tokens     INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    cost              REAL    NOT NULL DEFAULT 0,
    latency_ms        INTEGER NOT NULL DEFAULT 0,
    error             TEXT,
    created           INTEGER NOT NULL,

    UNIQUE (session, start_idx, end_idx),
    CHECK (status IN ('ok', 'degraded', 'skipped', 'error')),
    CHECK (n_events >= 0 AND n_facts >= 0 AND n_traits >= 0
           AND n_plans >= 0 AND n_doubts >= 0),
    CHECK (verbatim_fail >= 0),
    CHECK (attempts >= 1)
);

CREATE INDEX idx_runs_session ON extract_runs(session, created DESC);
CREATE INDEX idx_runs_status  ON extract_runs(status, created DESC);
