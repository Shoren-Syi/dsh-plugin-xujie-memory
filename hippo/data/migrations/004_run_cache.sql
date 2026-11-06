-- 004：把缓存命中与思考 token 记进 extract_runs（M5.5）
--
-- 为什么值得加两列：
--   D13 把"固定前缀排前面"作为省钱手段，推断能命中 89% 前缀、省 36% 成本。
--   这是**推断**，不是实测。没有这两列，就永远只能拿 cost 去反推，
--   而 cost 里混了输出 token 的影响，反推不出来。
--   cached_tokens / prompt_tokens 才是能直接读出命中率的两个数。
--
-- 用 ALTER TABLE 而不是重建：extract_runs 是纯记账表，
-- 重建会丢掉已有的调试记录，而那些记录正是"上次为什么抽空了"的唯一线索。

ALTER TABLE extract_runs ADD COLUMN cached_tokens    INTEGER NOT NULL DEFAULT 0;
ALTER TABLE extract_runs ADD COLUMN reasoning_tokens INTEGER NOT NULL DEFAULT 0;
