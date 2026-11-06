-- ============================================================
-- 008_fact_card.sql  ·  事实的"槽位基数"与唯一 active
--
-- 起因：`facts` 到 M7 为止没有任何唯一约束。同一个 (session, entity, key)
-- 可以同时有 5 条 valid_to IS NULL 的记录——它们全都"当前有效"，
-- 而彼此值不同。检索时取哪一条？取 id 最小的那条，也就是**最老的**。
--
-- M8 要把"当前值只能有一个"变成数据库级的约束，而不是代码里的自觉。
-- 代码里的自觉在 M7 已经出过一次事：`type` 被整列丢掉，300 个测试没抓到。
--
-- 但"只能有一个"不能一刀切：忌口可以有多条（不吃辣 / 不吃香菜 / 不喝冰的），
-- 住在哪只能有一条。这个区别由 card 列承载，唯一索引只盖 single。
--
-- card 是**写入时从 data/fact_keys.yaml 抄下来的**，不是独立事实源。
-- 冗余一列换来的是：约束可以被 SQLite 执行。读的时候不依赖 YAML 在不在。
-- ============================================================

-- ------------------------------------------------------------
-- 1. 基数：single = 同实体同 key 只能有一个当前值；multi = 可以并存
--    默认给 single 是保守选择：老数据里 diet 这类会被暂时当成 single，
--    但唯一索引只在"同一实体同一 key 有两条以上 active"时才报错，
--    下面是先做去重再建索引，所以老库不会因为这条默认值建不起索引。
-- ------------------------------------------------------------
ALTER TABLE facts ADD COLUMN card TEXT NOT NULL DEFAULT 'single';

-- ------------------------------------------------------------
-- 2. 模型写的原始 key
--    归一之后 `居住城市`/`居住地`/`city` 都成了 city。留着原文，
--    才能统计"哪些说法没进词表"——要扩词表得先知道缺什么。
-- ------------------------------------------------------------
ALTER TABLE facts ADD COLUMN key_raw TEXT NOT NULL DEFAULT '';

-- ------------------------------------------------------------
-- 3. 建索引之前先清掉已有的重复 active
--    保留 id 最大的那条（最新的），其余用 valid_to 关掉而不是删。
--    关掉是"历史仍然可查"，删掉是"这段记忆不存在过"，两者不能混。
--    valid_to 取该条自己的 valid_from：零长度区间，语义是"从未真正生效"。
-- ------------------------------------------------------------
UPDATE facts
   SET valid_to = coalesce(valid_from, 0)
 WHERE valid_to IS NULL
   AND card = 'single'
   AND EXISTS (
        SELECT 1 FROM facts f
         WHERE f.session = facts.session
           AND coalesce(f.entity_id, 0) = coalesce(facts.entity_id, 0)
           AND f.key = facts.key
           AND f.valid_to IS NULL
           AND f.card = 'single'
           AND f.id > facts.id
   );

-- ------------------------------------------------------------
-- 4. 唯一 active
--    entity_id 可以为 NULL（全局事实，比如"用户的作息"没挂实体）。
--    SQLite 的唯一索引认为 NULL 互不相等，于是 (s, NULL, city) 能重复插入。
--    coalesce 到 0 把它变成一个可比较的值——0 不是合法 rowid，不会撞车。
-- ------------------------------------------------------------
CREATE UNIQUE INDEX idx_facts_single_active
    ON facts(session, coalesce(entity_id, 0), key)
    WHERE valid_to IS NULL AND card = 'single';

-- 查"某个人的当前事实"是最热的读路径（角色卡 + 事实注入每轮都走）
CREATE INDEX idx_facts_current
    ON facts(session, entity_id, key)
    WHERE valid_to IS NULL;
