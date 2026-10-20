# -*- coding: utf-8 -*-
"""分页回读数据表全部记录，并对照预期做**缺失核验**。

为什么必须回读：资料库的 batch_add 会「假成功」—— 接口返回 success=true，
但记录其实没落库（实测 606 条写入后回读只有 602 条，缺的 4 条是因为某个
select 选项文本与表内定义差了一个空格，被服务端静默拒收）。
所以写库之后**不回读就等于没写**。

用法：
    <python> scripts/db_read.py --database-id ID --workdir DIR
        [--expect-total N] [--expect-seq-from-file records/records_batch_*.json]

产物：
    <workdir>/db_records.json     全量记录（原样）
    <workdir>/db_read_report.json 统计与缺失明细

需要环境变量：
    WORKBUDDY_LIBRARY_SKILL_DIR   资料库 skill 目录（含 _common.py）
"""
from __future__ import annotations

import argparse
import glob as globmod
import json
import os
import sys
from collections import Counter
from pathlib import Path

PATH_Q = "/space/api/agent/v1/query-database"
PAGE_SIZE = 200      # 服务端硬上限，调大无效
MAX_PAGES = 60


def _lib_dir() -> Path:
    raw = os.environ.get("WORKBUDDY_LIBRARY_SKILL_DIR", "").strip()
    if not raw:
        raise SystemExit(
            "错误：环境变量 WORKBUDDY_LIBRARY_SKILL_DIR 未设置。\n"
            "请指向资料库 skill 目录（其中含 _common.py）。")
    p = Path(raw).expanduser()
    if not (p / "_common.py").is_file():
        raise SystemExit(f"错误：{p} 下找不到 _common.py。")
    return p


LIB = _lib_dir()
sys.path.insert(0, str(LIB))
from _common import http_request, unwrap_data  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--database-id", required=True)
    ap.add_argument("--workdir", default=None)
    ap.add_argument("--expect-total", type=int, default=0)
    ap.add_argument("--expect-seq-from-file", default="",
                    help="records_batch_*.json 的 glob，从里面的「序号」取预期全集")
    ap.add_argument("--key-field", default="序号",
                    help="用于比对缺失的字段名（默认 序号）")
    a = ap.parse_args()

    wd = Path(a.workdir or os.environ.get("BILI_ORG_WORKDIR") or "./bili_org_work").expanduser().resolve()
    wd.mkdir(parents=True, exist_ok=True)

    rows, cursor = [], ""
    for i in range(MAX_PAGES):
        body = {"databaseId": a.database_id, "pageSize": PAGE_SIZE}
        if cursor:
            body["startCursor"] = cursor
        d = unwrap_data(http_request("POST", PATH_Q, body=body, timeout=30.0))
        if i == 0:
            print("  响应字段:", list(d.keys()))
        rs = d.get("results") or []
        rows.extend(rs)
        # 响应是驼峰；同时兼容 snake_case，别只读一种
        more = d.get("hasMore", d.get("has_more"))
        cursor = d.get("nextCursor") or d.get("next_cursor") or ""
        print(f"  页 {i + 1}: +{len(rs)}（累计 {len(rows)}）hasMore={more} "
              f"cursor={str(cursor)[:12]}")
        if not more and not cursor:
            break
        if rs and len(rows) >= 10000:
            print("  已达单表 1 万行上限，停止翻页")
            break

    (wd / "db_records.json").write_text(json.dumps(rows, ensure_ascii=False),
                                        encoding="utf-8")

    report = {"database_id": a.database_id, "total": len(rows),
              "by_整理状态": dict(Counter(r.get("整理状态") or "" for r in rows))}

    fails = []
    if a.expect_total and len(rows) != a.expect_total:
        fails.append(f"总数不符：期望 {a.expect_total}，实际 {len(rows)}")
    if a.expect_seq_from_file:
        want = set()
        for p in globmod.glob(a.expect_seq_from_file):
            for r in json.loads(Path(p).read_text(encoding="utf-8")):
                v = (r.get(a.key_field) or {}).get("number")
                if v is not None:
                    want.add(int(v))
        got = {int(r[a.key_field]) for r in rows
               if isinstance(r.get(a.key_field), (int, float))}
        missing = sorted(want - got)
        report.update({"expected": len(want), "got": len(got),
                       "missing": missing[:200], "n_missing": len(missing)})
        if missing:
            fails.append(f"缺失 {len(missing)} 条（{a.key_field}）：{missing[:40]}")

    (wd / "db_read_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")

    print(f"\n总计 {len(rows)} 条 → {wd / 'db_records.json'}")
    print("整理状态:", report["by_整理状态"])
    if fails:
        print("\n核验未通过：")
        for x in fails:
            print("  ✗", x)
        print("常见原因：某个 select 选项文本与表内的定义不完全一致（多空格/少空格/"
              "异体字），服务端会静默拒收那一条。核对选项文本后补录。")
        return 2
    if a.expect_total or a.expect_seq_from_file:
        print("\n核验通过。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
