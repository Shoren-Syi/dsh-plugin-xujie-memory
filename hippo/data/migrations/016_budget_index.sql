-- M27.3：日预算熔断每分钟都要回答"今天花了多少"。
--
-- metrics 有 idx_metrics_ts（001 就建了），extract_runs 只按 session 和
-- status 建过索引——按 created 过滤得全表扫。表不大时无所谓，但它是
-- **每轮对话都跑一次**的查询，而且会随窗口数线性长（重度用户一年 3.6 万行）。
--
-- 为什么不在内存里计数：进程重启会把当日已用量清零，用户可以通过
-- 重启服务无限刷额度。持久数字才是预算，内存数字只是缓存。
CREATE INDEX IF NOT EXISTS idx_runs_created ON extract_runs(created);
