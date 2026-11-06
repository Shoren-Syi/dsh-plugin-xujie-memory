-- 012 —— M18.3 存疑池生命周期
--
-- converted：这条 doubt 是否已经转成过 kind=doubt 的 cold 事件。
--
-- 为什么需要这一列：巩固是每天跑的，过期 doubt 如果不标记，
-- 第二天巩固还会再扫到它，然后写第二条一模一样的"没搞清楚"。
-- 用户视角就是"它翻来覆去忏悔同一件事"——比忘掉更糟。
-- resolved 的 doubt 不会转（resolve 意味着答案已经有了），所以
-- converted 只管"既没 resolve 又过期"的那一批，保证只转一次。
ALTER TABLE doubts ADD COLUMN converted INTEGER NOT NULL DEFAULT 0;
