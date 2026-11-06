-- 005 · 原文不可变（M6.3）
--
-- events.raw 是"唯一真相源"。整个系统的可信度建立在一条前提上：
-- 你三个月后看到的那句话，是用户当时真的说过的那句话。
-- 一旦允许 UPDATE，任何一次"顺手润色"都会让这条前提失效，
-- 而且**事后无法分辨**哪条被改过。
--
-- 所以用数据库层拒绝，而不是靠代码自觉。
-- 代码层还能改，数据库层改不动——这是"铁律"和"约定"的区别。

-- ------------------------------------------------------------
-- 1. 拒绝 UPDATE raw
--    `IS NOT` 而不是 `<>`：NULL 与非 NULL 比较时 `<>` 为 NULL（假），
--    会漏掉"把 raw 改成 NULL"这一种破坏。
-- ------------------------------------------------------------
CREATE TRIGGER trg_events_raw_immutable
BEFORE UPDATE OF raw ON events
WHEN OLD.raw IS NOT NEW.raw
BEGIN
    SELECT RAISE(ABORT, 'events.raw is immutable');
END;

-- ------------------------------------------------------------
-- 2. 拒绝 DELETE
--    正常业务不做物理删除：过期走 archived=1，被推翻走 superseded_by。
--    但维护操作（清空测试库、用户主动 wipe）确实需要删。
--    用一个显式开关放行，而不是留个后门表——开关本身也会被审计。
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS maint_flags (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TRIGGER trg_events_no_delete
BEFORE DELETE ON events
WHEN coalesce((SELECT value FROM maint_flags WHERE key = 'allow_event_delete'), '0') <> '1'
BEGIN
    SELECT RAISE(ABORT, 'events is append-only: use superseded_by/archived (set allow_event_delete=1 for maintenance)');
END;

-- ------------------------------------------------------------
-- 3. 摘要截断标记
--    超长摘要被截断时必须留痕，否则"摘要只有半句"会被当成模型写得差，
--    而实际是我们的截断逻辑吃掉了后半句。
-- ------------------------------------------------------------
ALTER TABLE events ADD COLUMN summary_truncated INTEGER NOT NULL DEFAULT 0;

-- ------------------------------------------------------------
-- 4. 摘要"已知不合格"标记
--    和 truncated 分开，因为两者的含义完全不同：
--      truncated=1  → 我们砍掉了后半句（**信息丢了**）
--      flagged=1    → 模型两次都没写好，摘要留着但质量存疑（**信息可能不准**）
--    合成一个字段的话，统计时你会永远分不清"是截断多了"还是"是模型变差了"。
-- ------------------------------------------------------------
ALTER TABLE events ADD COLUMN summary_flagged INTEGER NOT NULL DEFAULT 0;
