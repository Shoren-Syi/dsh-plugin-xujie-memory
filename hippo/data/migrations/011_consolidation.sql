-- ============================================================
-- 011_consolidation.sql  ·  巩固日志（M15）
--
-- 为什么要单独一张表，而不是只写 events.superseded_by
-- ------------------------------------------------------
-- `superseded_by` 只回答"这条被谁取代了"，回答不了：
--   1. 是哪一次巩固做的？（要能整体回滚一批）
--   2. 用的是强判据还是弱判据？（弱判据需要提供撤销入口）
--   3. 摘要改成什么样了？改之前是什么？（改错了要能比对）
--
-- 前两条直接决定"合并可不可逆"。合并是不可逆操作里最危险的一种：
-- 原文还在（raw 不可变），但**检索层再也看不见那条独立的记忆**。
-- 所以每次合并都必须留下"谁合了谁、凭什么合、改了什么"。
--
-- 为什么不复用 metrics
-- -------------------
-- metrics 是成本埋点，按天滚；这张表是**操作审计**，要一直留着。
-- 混在一起的话，清 metrics 会把合并记录一起清掉，出问题时无法追溯。
-- ============================================================

CREATE TABLE consolidation_log (
    id           INTEGER PRIMARY KEY,
    ts           INTEGER NOT NULL,
    session      TEXT    NOT NULL,

    -- 主记录：簇里 weight 最高那条，raw 保持它自己的原文不动
    master_id    INTEGER NOT NULL REFERENCES events(id),
    -- 被合并掉的其余成员（JSON 数组，不含 master）
    member_ids   TEXT    NOT NULL,

    n_merged     INTEGER NOT NULL,

    -- 1 = 强判据（词面完全包含 / 原文逐字相同），自动合并、不提供撤销
    -- 0 = 弱判据（覆盖率过线但样本小），合并但可用 unmerge 撤销
    strong       INTEGER NOT NULL,
    score        REAL    NOT NULL,

    -- 摘要改了什么。改错了要比对，不能只留新值。
    summary_old  TEXT    NOT NULL DEFAULT '',
    summary_new  TEXT    NOT NULL DEFAULT '',

    -- 摘要重生成的开销（走模型时才有）
    tokens       INTEGER NOT NULL DEFAULT 0,
    cost         REAL    NOT NULL DEFAULT 0,

    -- 撤销时间。非空 = 已撤销，检索时应把它们当独立事件看。
    -- 不删日志行：撤销本身也是需要留痕的操作。
    undone_at    INTEGER,

    CHECK (strong IN (0, 1)),
    CHECK (n_merged >= 1)
);

CREATE INDEX idx_consol_master ON consolidation_log(master_id, undone_at);
CREATE INDEX idx_consol_ts     ON consolidation_log(ts DESC);
