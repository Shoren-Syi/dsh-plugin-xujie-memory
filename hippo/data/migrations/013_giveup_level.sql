-- M22.3：弃权记录要能按 level 聚合。
-- 为什么加列而不是塞进 keywords：统计是 SQL 的事，把结构塞进关键词
-- 等于让后续每个查询都做字符串解析（M9 定过"结构进列不进串"）。
ALTER TABLE events ADD COLUMN giveup_level TEXT NOT NULL DEFAULT '';
