-- ============================================================
-- 001_init.sql  ·  海马体插件 初始库结构
-- 对应《完整手册》第一部分 §3
--
-- 约定：
--   1. 所有时间列一律存 UTC epoch 秒（INTEGER）。
--   2. 迁移文件只增不改；已发布的文件禁止编辑。
--   3. schema_meta 由本文件创建，版本行由执行器在同一事务内追加。
-- ============================================================

CREATE TABLE schema_meta (
    version    INTEGER PRIMARY KEY,
    name       TEXT    NOT NULL,
    applied_at INTEGER NOT NULL
);

-- ------------------------------------------------------------
-- 1. events —— 记忆主表（唯一真相源）
-- ------------------------------------------------------------
CREATE TABLE events (
    id             INTEGER PRIMARY KEY,
    session        TEXT    NOT NULL,

    -- 三重时间戳：发生 / 得知 / 最后访问（手册 §3.1）
    event_time     INTEGER NOT NULL,
    event_time_end INTEGER,
    ingest_time    INTEGER NOT NULL,
    last_access    INTEGER,

    -- 时间可信度（M8.3 / M17.1）
    time_precision TEXT    NOT NULL DEFAULT 'day',
    time_guessed   INTEGER NOT NULL DEFAULT 0,

    -- 权重与分层（M14）
    tier           INTEGER NOT NULL DEFAULT 2,
    weight         REAL    NOT NULL DEFAULT 0.5,
    w0             REAL    NOT NULL DEFAULT 0.5,
    arousal        REAL    NOT NULL DEFAULT 0,
    recall_count   INTEGER NOT NULL DEFAULT 0,

    -- 三级表示：关键词 / 摘要 / 原文（手册 §5.3）
    keywords       TEXT    NOT NULL DEFAULT '',
    summary        TEXT    NOT NULL DEFAULT '',
    raw            TEXT    NOT NULL,

    entities       TEXT    NOT NULL DEFAULT '[]',   -- JSON array of entity_id
    source         TEXT    NOT NULL DEFAULT 'user',
    kind           TEXT    NOT NULL DEFAULT 'event',
    pinned         INTEGER NOT NULL DEFAULT 0,
    superseded_by  INTEGER REFERENCES events(id),
    archived       INTEGER NOT NULL DEFAULT 0,

    CHECK (tier BETWEEN 1 AND 3),
    CHECK (source IN ('user', 'ai', 'third')),
    CHECK (kind IN ('event', 'fact', 'regret', 'doubt', 'giveup', 'plan', 'idle', 'raw')),
    CHECK (pinned    IN (0, 1)),
    CHECK (archived  IN (0, 1)),
    CHECK (time_guessed IN (0, 1)),
    CHECK (arousal  >= 0 AND arousal  <= 1),
    CHECK (weight   >= 0 AND weight   <= 1),
    CHECK (w0       >= 0 AND w0       <= 1),
    CHECK (length(raw) > 0)
);

CREATE INDEX idx_events_session_tier ON events(session, tier, weight DESC);
CREATE INDEX idx_events_time         ON events(event_time);
CREATE INDEX idx_events_kind         ON events(session, kind);
CREATE INDEX idx_events_superseded   ON events(superseded_by);
CREATE INDEX idx_events_ingest       ON events(ingest_time);

-- ------------------------------------------------------------
-- 2. entities —— 角色卡主体（M7.1）
-- ------------------------------------------------------------
CREATE TABLE entities (
    id         INTEGER PRIMARY KEY,
    session    TEXT    NOT NULL,
    canonical  TEXT    NOT NULL,
    aliases    TEXT    NOT NULL DEFAULT '[]',   -- JSON array
    type       TEXT    NOT NULL DEFAULT 'person',
    confidence REAL    NOT NULL DEFAULT 0.5,
    first_seen INTEGER,
    last_seen  INTEGER,
    UNIQUE (session, canonical),
    CHECK (type IN ('person', 'place', 'thing', 'org')),
    CHECK (confidence >= 0 AND confidence <= 1)
);

CREATE INDEX idx_entities_session ON entities(session, type);

-- ------------------------------------------------------------
-- 3. card_traits —— 角色卡特征，必须挂证据（M7.2）
-- ------------------------------------------------------------
CREATE TABLE card_traits (
    id         INTEGER PRIMARY KEY,
    entity_id  INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    trait      TEXT    NOT NULL,
    status     TEXT    NOT NULL DEFAULT 'observed',
    evidence   TEXT    NOT NULL DEFAULT '[]',   -- JSON array of event_id，不可为空
    confidence REAL    NOT NULL DEFAULT 0.5,
    valid_from INTEGER,
    valid_to   INTEGER,                          -- 非空 = 已失效，不删
    updated_at INTEGER,
    CHECK (status IN ('observed', 'inferred')),
    CHECK (confidence >= 0 AND confidence <= 1)
);

CREATE INDEX idx_traits_entity ON card_traits(entity_id, valid_to);

-- ------------------------------------------------------------
-- 4. facts —— 会过期的事实（M8）
-- ------------------------------------------------------------
CREATE TABLE facts (
    id           INTEGER PRIMARY KEY,
    session      TEXT    NOT NULL,
    entity_id    INTEGER REFERENCES entities(id) ON DELETE SET NULL,
    key          TEXT    NOT NULL,
    value        TEXT    NOT NULL,
    valid_from   INTEGER,
    valid_to     INTEGER,                        -- 非空 = historical
    source_event INTEGER REFERENCES events(id),
    confidence   REAL    NOT NULL DEFAULT 0.5,
    CHECK (confidence >= 0 AND confidence <= 1)
);

CREATE INDEX idx_facts_lookup ON facts(session, entity_id, key, valid_to);

-- ------------------------------------------------------------
-- 5. doubts —— 存疑池（M18）
-- ------------------------------------------------------------
CREATE TABLE doubts (
    id              INTEGER PRIMARY KEY,
    session         TEXT    NOT NULL,
    question        TEXT    NOT NULL,
    context         TEXT    NOT NULL DEFAULT '',
    confidence      REAL    NOT NULL DEFAULT 0.5,
    cost            REAL    NOT NULL DEFAULT 0.5,
    impact          REAL    NOT NULL DEFAULT 0.5,
    created         INTEGER NOT NULL,
    expires         INTEGER NOT NULL,
    asked_count     INTEGER NOT NULL DEFAULT 0,
    last_asked_turn INTEGER,
    resolved        INTEGER NOT NULL DEFAULT 0,
    CHECK (confidence >= 0 AND confidence <= 1),
    CHECK (cost       >= 0 AND cost       <= 1),
    CHECK (impact     >= 0 AND impact     <= 1),
    CHECK (resolved IN (0, 1))
);

CREATE INDEX idx_doubts_active ON doubts(session, resolved, expires);

-- ------------------------------------------------------------
-- 6. plans —— 计划型记忆（M19）
-- ------------------------------------------------------------
CREATE TABLE plans (
    id        INTEGER PRIMARY KEY,
    session   TEXT    NOT NULL,
    due_at    INTEGER NOT NULL,          -- 绝对时间戳，写入时一次换算好
    content   TEXT    NOT NULL,
    raw_hint  TEXT,
    recur     TEXT,                       -- NULL / daily / weekly
    fired     INTEGER NOT NULL DEFAULT 0,
    fired_at  INTEGER,
    missed    INTEGER NOT NULL DEFAULT 0,
    attempts  INTEGER NOT NULL DEFAULT 0,
    created   INTEGER NOT NULL,
    CHECK (fired  IN (0, 1)),
    CHECK (missed IN (0, 1)),
    CHECK (recur IS NULL OR recur IN ('daily', 'weekly'))
);

CREATE INDEX idx_plans_due ON plans(fired, due_at);

-- ------------------------------------------------------------
-- 7. affect —— 情绪状态（M20）
-- ------------------------------------------------------------
CREATE TABLE affect (
    session  TEXT PRIMARY KEY,
    joy      REAL NOT NULL DEFAULT 60,
    sadness  REAL NOT NULL DEFAULT 10,
    anger    REAL NOT NULL DEFAULT 5,
    fear     REAL NOT NULL DEFAULT 5,
    disgust  REAL NOT NULL DEFAULT 5,
    surprise REAL NOT NULL DEFAULT 20,
    updated  INTEGER NOT NULL
);

-- ------------------------------------------------------------
-- 8. ledger —— 奖励账簿（M23）。数值绝不进 prompt。
-- ------------------------------------------------------------
CREATE TABLE ledger (
    action_id TEXT PRIMARY KEY,
    session   TEXT    NOT NULL,
    action    TEXT    NOT NULL,
    predicted REAL    NOT NULL DEFAULT 0,
    actual    REAL,
    rpe       REAL,
    ts        INTEGER NOT NULL,
    settled   INTEGER NOT NULL DEFAULT 0,   -- 0 待结算 / 1 已结算 / -1 过期作废
    CHECK (predicted >= -1 AND predicted <= 1)
);

CREATE INDEX idx_ledger_open ON ledger(session, settled, ts);

-- ------------------------------------------------------------
-- 9. 辅助表
-- ------------------------------------------------------------

-- 9.1 turns —— 最近 N 轮原文（抽象窗口的来源）
CREATE TABLE turns (
    session      TEXT    NOT NULL,
    idx          INTEGER NOT NULL,
    role         TEXT    NOT NULL,
    content      TEXT    NOT NULL,
    ts           INTEGER NOT NULL,
    partial      INTEGER NOT NULL DEFAULT 0,
    extracted_at INTEGER,
    PRIMARY KEY (session, idx),
    CHECK (role IN ('user', 'assistant', 'system')),
    CHECK (partial IN (0, 1))
);

-- 9.2 kw_index —— 关键词倒排。2 字中文词的唯一可行方案（M10.1）
CREATE TABLE kw_index (
    token    TEXT    NOT NULL,
    event_id INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    PRIMARY KEY (token, event_id)
) WITHOUT ROWID;

CREATE INDEX idx_kw_event ON kw_index(event_id);

-- 9.3 metrics —— 成本与性能埋点
CREATE TABLE metrics (
    id                INTEGER PRIMARY KEY,
    ts                INTEGER NOT NULL,
    session           TEXT,
    req_id            TEXT,
    t0                INTEGER,
    t1                INTEGER,
    t2                INTEGER,
    t3                INTEGER,
    prompt_tokens     INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    cached_tokens     INTEGER NOT NULL DEFAULT 0,
    cost              REAL    NOT NULL DEFAULT 0
);

CREATE INDEX idx_metrics_ts ON metrics(ts);

-- ------------------------------------------------------------
-- 10. FTS5 全文索引（摘要模糊匹配专用）
--     注意：trigram 分词器只索引 ≥3 字符的片段，
--           2 字中文词请走 kw_index（M10.1）。
-- ------------------------------------------------------------
CREATE VIRTUAL TABLE events_fts USING fts5(
    summary,
    keywords,
    content    = 'events',
    content_rowid = 'id',
    tokenize   = 'trigram'
);

CREATE TRIGGER events_ai AFTER INSERT ON events BEGIN
    INSERT INTO events_fts(rowid, summary, keywords)
    VALUES (new.id, new.summary, new.keywords);
END;

CREATE TRIGGER events_ad AFTER DELETE ON events BEGIN
    INSERT INTO events_fts(events_fts, rowid, summary, keywords)
    VALUES ('delete', old.id, old.summary, old.keywords);
END;

-- 只监听 summary/keywords 两列：
-- 权重、last_access 的高频更新不应触发 FTS 重建。
CREATE TRIGGER events_au AFTER UPDATE OF summary, keywords ON events BEGIN
    INSERT INTO events_fts(events_fts, rowid, summary, keywords)
    VALUES ('delete', old.id, old.summary, old.keywords);
    INSERT INTO events_fts(rowid, summary, keywords)
    VALUES (new.id, new.summary, new.keywords);
END;
