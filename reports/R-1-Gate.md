# R-1 · P0 阶段 Gate（M1 数据库层）

| 项 | 内容 |
|---|---|
| 里程碑 | M1 数据库层 |
| 日期 | 2026-11-04 |
| 环境 | Python 3.11.1 / SQLite 3.45.1 / Linux |
| 结论 | **通过** |

## 一、子任务汇总

| 子任务 | 内容 | 用例 | 结论 | 报告 |
|---|---|---|---|---|
| M1.1 ★ | DDL 与迁移 | 11 | ✅ 通过 | R-1.1 |
| M1.2 | FTS5 与触发器 | 7 | ✅ 通过 | R-1.2 |
| M1.3 | DAO 层 | 25 | ✅ 通过 | R-1.3 |
| **合计** | | **43** | **43 通过 / 0 失败** | |

## 二、主干项（★）检查

| 主干项 | 结果 |
|---|---|
| 迁移幂等、失败整体回滚、版本号只前进 | ✅ |
| 13 张表 + 9 索引 + 3 触发器 | ✅ |
| 10 万条批量导入 2.27s（< 5s） | ✅ |
| 业务层无裸 SQL | ✅ |
| 20 线程并发写入不丢不重 | ✅ |

## 三、这一阶段实际产出的东西

```
hippo/
├── __init__.py
├── cli.py                   init / doctor / stats / remember / recall
├── config.py                配置模型 + 环境变量覆盖
├── core/
│   ├── db.py                连接工厂 + 显式事务 + trigram 探测
│   ├── migrate.py           事务化迁移执行器
│   ├── models.py            9 个行模型
│   └── store.py             42 个方法，唯一的 SQL 出口
└── data/
    ├── migrations/001_init.sql
    └── config.default.yaml
tests/                       43 条用例
reports/                     R-1.1 / R-1.2 / R-1.3 / 本文件
tools/verify.py              make verify M=x.y
```

## 四、开工即被验证的三条设计判断

1. **FTS5 trigram 不能当主力检索**——2 字中文词索引不到。`kw_index` 不是备选方案，是必需品。已写成常驻回归用例。
2. **批量导入必须有专用路径**——逐行 7.11s，摘触发器 + 重建 1.42s，差 5 倍。已作为正式 API（`bulk_import_events`）而非测试技巧。
3. **UPDATE 触发器必须限定列**——否则每次 `recall_count += 1` 都重建一次索引。已用 `AFTER UPDATE OF summary, keywords` 锁住。

## 五、与手册的偏差

| # | 偏差 | 原因 |
|---|---|---|
| 1 | 7 个后续里程碑才用到的列直接进了 001 | 避免新建库反复 ALTER；均为 M8/M14/M17 公式的硬依赖 |
| 2 | `migrate.py` / `db.py` 与 `store.py` 并列为 SQL 合法出口 | 迁移执行器必须写版本表；连接工厂必须发 PRAGMA |
| 3 | M0 只做了 M0.1 的必要部分（包结构 + CLI 骨架） | M1 需要宿主；M0.2 的完整配置校验与 M0.3 录制回放尚未做 |

## 六、进入下一阶段前必须知道的

- **M10.1 之前必须修**：`Store.events_of_entity()` 用的 `LIKE '%id%'` 会把实体 1 误匹配到 11、21。M9 开工第一件事。
- **M5 之前要补**：录制回放（M0.3）。没有它，抽取器的每次调参都要真花钱，评测跑不动。

## 七、验收命令

```bash
make test                          # 43 passed
hippo init --dir /tmp/h1           # 幂等
hippo doctor                       # 5/5
hippo remember "..." --keywords a,b
hippo recall 老王                   # 调试检索
```
