-- ============================================================
-- 007_trait_slot.sql  ·  角色卡特征的"槽位"
--
-- 手册 M7.2 要求"矛盾 trait 正确失效"：用户换了工作，
-- "在字节做后端" 和 "在创业公司做后端" 必须只有一条是 active。
--
-- 可是这两句话字面毫无关系，字符串比不出矛盾。能比的是**槽位**：
-- 两句话都在回答"在哪工作"。所以给 trait 加一个 slot，
-- 同一实体同一槽位只允许一条 active。
--
-- slot 为空 = 不占槽，永远不触发失效。
-- 这条留白很关键：模型给不出槽位的特征（"话少但心细"）如果也默认占一个槽，
-- 它们会互相覆盖，角色卡最后只剩一条"特征"。
-- ============================================================

ALTER TABLE card_traits ADD COLUMN slot TEXT NOT NULL DEFAULT '';

-- 同一实体、同一槽位、active 的记录最多一条。部分索引把 slot='' 排除在外。
CREATE UNIQUE INDEX idx_traits_slot_active
    ON card_traits(entity_id, slot)
    WHERE valid_to IS NULL AND slot <> '';
