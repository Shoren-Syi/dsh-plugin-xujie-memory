-- ============================================================
-- 009_kw_lowercase.sql  ·  倒排键统一小写
--
-- 起因：M10.3 的错别字/中英混排评测里，"Go" 这一条怎么都查不到。
-- 库里 17 号事件的关键词是 `推荐系统 召回 Go`，用户输 "go" 命中 0 条。
--
-- 根因不是查询侧：`lexicon.query_tokens` 已经把整句小写了。
-- 是**写入侧没归一**——`events.keywords` 存原文（这是对的，要显示），
-- `kw_index.token` 也存原文（这是错的，它是索引键）。
--
-- 后果比"多一条命中不了"更麻烦：`AI` 和 `ai` 是两条互不相干的倒排项，
-- 同一条事件在两个键下各占一行。总量看不见，检索会莫名其妙地漏。
-- `clean_keywords` 里早就写了"小写去重：AI / ai 在 kw_index 里是两条
-- 互不命中的倒排项"——那句话只在清洗阶段生效，落库时没做，意图没落地。
--
-- 为什么重建表而不是 UPDATE：`PRIMARY KEY(token, event_id)` 决定
-- `UPDATE kw_index SET token = lower(token)` 会在
-- "Go"/"go" 同时存在时撞主键。WITHOUT ROWID 表也不支持 UPDATE OR REPLACE
-- 的原地合并语义，只能整表搬一次。
-- ============================================================

-- 重建表时**每一个约束都要抄过来**，包括外键。
-- 第一版漏了 `REFERENCES events(id) ON DELETE CASCADE`，后果是
-- `delete_event` 之后倒排里留下孤儿行——而 M1 的 DAO 测试正好会查这个，
-- 它在合并前是绿的（事故 62）。这类"重建丢东西"是迁移最典型的坑：
-- 表结构看起来对，只是少了一条约束，运行时不报错。
CREATE TABLE kw_index_new (
    token    TEXT    NOT NULL,
    event_id INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    -- CHECK 把"索引键必须小写"变成结构性保证，而不是每个写入点各自的自觉。
    -- 代码里的自觉在 M7 已经出过一次事（type 被整列丢掉，300 个测试没抓到）。
    CHECK (token = lower(token)),
    PRIMARY KEY (token, event_id)
) WITHOUT ROWID;

-- OR IGNORE：原来 "Go" 和 "go" 两行会压成一行，这正是目的
INSERT OR IGNORE INTO kw_index_new(token, event_id)
    SELECT lower(token), event_id FROM kw_index;

DROP TABLE kw_index;
ALTER TABLE kw_index_new RENAME TO kw_index;

-- DROP TABLE 会连索引一起删，重建
CREATE INDEX idx_kw_event ON kw_index(event_id);
