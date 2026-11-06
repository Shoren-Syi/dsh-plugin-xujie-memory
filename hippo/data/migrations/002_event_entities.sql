-- ============================================================
-- 002_event_entities.sql  ·  事件↔实体 关联表
--
-- 为什么必须补这张表
-- ------------------
-- 001 里 events.entities 是 JSON 数组，查询只能 LIKE '%3%'。
-- 实体 3 会命中 13、23、30…，把张三的记忆挂到李四身上。
-- 这是"越用越错"的那类 bug：单测上百个实体全是 id<10 时永远发现不了，
-- 上线三个月后用户说"它把我同事记成我老婆了"。
--
-- 设计取舍
-- --------
-- 1. **event_entities 是唯一真相源**，events.entities 降级为冗余缓存。
--    冗余列留着的原因：渲染记忆块时一次 SELECT 就能拿到，不用 JOIN；
--    且历史归档导出格式不变。
-- 2. 用触发器保持两者同步，而不是靠调用方自觉。
--    自觉这件事，在写了 40 个 DAO 方法之后就不存在了。
-- 3. 回填只认 JSON 里的整数，字符串/浮点一律跳过——
--    宁可少关联，不可错关联。
-- ============================================================

CREATE TABLE event_entities (
    event_id  INTEGER NOT NULL REFERENCES events(id)   ON DELETE CASCADE,
    entity_id INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    PRIMARY KEY (event_id, entity_id)
) WITHOUT ROWID;

-- 反查"某实体的所有事件"走这个索引（M7 角色卡、M10 检索都用）
CREATE INDEX idx_ee_entity ON event_entities(entity_id, event_id);

-- ------------------------------------------------------------
-- 回填历史数据
-- json_each(e.entities) 对非数组返回空集；json_valid 挡掉脏值
-- ------------------------------------------------------------
INSERT OR IGNORE INTO event_entities(event_id, entity_id)
SELECT e.id, CAST(j.value AS INTEGER)
FROM events e, json_each(e.entities) j
WHERE json_valid(e.entities)
  AND j.type = 'integer';

-- ------------------------------------------------------------
-- 同步触发器
-- 只监听 entities 列：weight/recall_count 的高频更新不该触发重写关联
-- ------------------------------------------------------------
CREATE TRIGGER events_ee_ai AFTER INSERT ON events
WHEN json_valid(new.entities)
BEGIN
    INSERT OR IGNORE INTO event_entities(event_id, entity_id)
    SELECT new.id, CAST(j.value AS INTEGER)
    FROM json_each(new.entities) j
    WHERE j.type = 'integer';
END;

CREATE TRIGGER events_ee_au AFTER UPDATE OF entities ON events
WHEN json_valid(new.entities)
BEGIN
    DELETE FROM event_entities WHERE event_id = new.id;
    INSERT OR IGNORE INTO event_entities(event_id, entity_id)
    SELECT new.id, CAST(j.value AS INTEGER)
    FROM json_each(new.entities) j
    WHERE j.type = 'integer';
END;

-- DELETE 靠 ON DELETE CASCADE（需 PRAGMA foreign_keys=ON，db.connect 已开）
