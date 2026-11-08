# -*- coding: utf-8 -*-
"""旧库迁移：只跑到 014 的库，能否被当前代码平滑升到 019 且不丢数据。"""
from __future__ import annotations

import io
import shutil
import sys
from pathlib import Path

sys.path.insert(0, r"C:\h\hippo")

from hippo.config import Config  # noqa: E402
from hippo.core.db import connect  # noqa: E402
from hippo.core.migrate import migrate  # noqa: E402
from hippo.core.store import Store  # noqa: E402

SRC = Path(r"C:\h\hippo\hippo\data\migrations")
TMP = Path(r"C:\h\stress\mig-old")
DB = Path(r"C:\h\stress\old-schema.db")


def main() -> int:
    if TMP.exists():
        shutil.rmtree(TMP)
    TMP.mkdir(parents=True)
    files = sorted(SRC.glob("*.sql"))
    for f in files:
        if f.name.split("_")[0] <= "014":
            shutil.copyfile(f, TMP / f.name)
    if DB.exists():
        DB.unlink()
    c = connect(DB)
    applied = migrate(c, directory=TMP)
    cols = [r[1] for r in c.execute("pragma table_info(events)")]
    print(f"旧库建好：迁移 {len(applied)} 个，events 有 importance={'importance' in cols} key_values={'key_values' in cols}")
    c.execute("INSERT INTO turns (session, idx, role, content, ts) VALUES ('old-1', 0, 'user', '我妈下个月十号过生日', 1760000000)")
    c.commit()
    c.close()

    cfg = Config()
    cfg.db_path = str(DB)
    with Store.open(DB, cfg) as store:
        cols = [r[1] for r in store.conn.execute("pragma table_info(events)")]
        turns = store.conn.execute("select count(*) from turns").fetchone()[0]
        runs = store.conn.execute("select count(*) from extract_runs").fetchone()[0]
        print(f"升级后：importance={'importance' in cols} key_values={'key_values' in cols} turns={turns} extract_runs 表可查({runs})")
        eid = store.add_event(session="old-1", raw="我妈下个月十号过生日",
                              summary="用户妈妈下个月十号过生日", keywords=["生日"],
                              importance=5, values=["下个月十号"], event_time=1760000000)
        ev = store.get_event(eid)
        print(f"新字段可写：importance={ev.importance} values={ev.value_list}")
    print("OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
