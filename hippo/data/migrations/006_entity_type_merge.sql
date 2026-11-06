-- @rebuild: entities
-- ============================================================
-- 006_entity_type_merge.sql  ·  实体同名跨类型 + 合并留痕
--
-- 为什么必须重建表
-- ----------------
-- 001 的约束是 UNIQUE(session, canonical)，名字在一个会话里只能有一个实体。
-- 可是"朝阳"既是北京的一个区，也是常见人名；
-- "建国"是名字，也是路名。一旦先建了 person 朝阳，
-- 后面所有 "搬到朝阳" 的事件都会被挂到那个"人"身上，
-- 角色卡随之长出"朝阳是什么性格"——而且**永远修不回来**，
-- 因为 upsert 只会命中旧行，类型不会改。
--
-- SQLite 不能 ALTER 约束，只能重建。重建又必须关外键，
-- 否则 DROP TABLE entities 会顺着 ON DELETE CASCADE
-- 把 event_entities / card_traits / facts 全删掉。
-- 关外键必须在事务之外（PRAGMA 在事务内是空操作），
-- 所以由 migrate.py 通过文件头 `-- @rebuild:` 接管，
-- 不允许在 SQL 里自己写 PRAGMA——迁移是批量执行的，
-- 一个漏掉的 ON 会让后续所有迁移在无外键保护下运行。
--
-- merged_into 也是这一轮加的：合并实体是**不可逆**操作，
-- 留一个指针，至少能回答"这个人的记忆是从哪来的"。
-- ============================================================

CREATE TABLE entities_new (
    id          INTEGER PRIMARY KEY,
    session     TEXT    NOT NULL,
    canonical   TEXT    NOT NULL,
    aliases     TEXT    NOT NULL DEFAULT '[]',
    type        TEXT    NOT NULL DEFAULT 'person',
    confidence  REAL    NOT NULL DEFAULT 0.5,
    first_seen  INTEGER,
    last_seen   INTEGER,
    merged_into INTEGER REFERENCES entities_new(id),  -- 非空 = 已并入别人，不再参与匹配
    UNIQUE (session, canonical, type),
    CHECK (type IN ('person', 'place', 'thing', 'org')),
    CHECK (confidence >= 0 AND confidence <= 1)
);

INSERT INTO entities_new(id, session, canonical, aliases, type, confidence,
                         first_seen, last_seen, merged_into)
SELECT id, session, canonical, aliases, type, confidence, first_seen, last_seen, NULL
FROM entities;

DROP TABLE entities;

ALTER TABLE entities_new RENAME TO entities;

CREATE INDEX idx_entities_session ON entities(session, type);
