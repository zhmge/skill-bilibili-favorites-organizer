# -*- coding: utf-8 -*-
"""把整理计划翻译成可执行的移动方案，并出一份人来读的预演单。

这一步**不改任何东西**，只算清楚要动哪些条目、动到哪、有哪些冲突。
动手前先把预演单给人看，是因为 B站的批量移动没有预览也没有确认框，
脚本跑错就是几百条错位。

用法：
    <python> scripts/plan_moves.py --workdir DIR [--destinations "计算机,…,杂项"]

产物：
    <workdir>/move_plan.json   机器读：分批方案 + 需要新建的夹
    <workdir>/move_plan.md     人读：逐条预演单 + 冲突清单
"""
from __future__ import annotations

import argparse
import json
import os
from collections import Counter, defaultdict
from pathlib import Path


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--workdir", default=None)
    ap.add_argument("--src", default="", help="源夹名，留空 = 快照里的默认收藏夹")
    ap.add_argument("--destinations", default="",
                    help="允许的去向，逗号分隔；留空 = 计划里出现过的全部去向")
    ap.add_argument("--batch", type=int, default=20, help="每批移动条数（首次用前必须试跑）")
    a = ap.parse_args()

    wd = Path(a.workdir or os.environ.get("BILI_ORG_WORKDIR") or "./bili_org_work").expanduser().resolve()
    snap = json.loads((wd / "snapshot.json").read_text(encoding="utf-8"))
    plan_doc = json.loads((wd / "reclass_plan.json").read_text(encoding="utf-8"))
    plan = plan_doc["plan"]

    src_title = a.src or snap.get("default_folder_title") or "默认收藏夹"
    by_title = {f.get("title"): f for f in snap["folders"]}
    if src_title not in by_title:
        raise SystemExit(f"错误：快照里没有收藏夹「{src_title}」")
    src_id = int(by_title[src_title]["id"])

    declared = [x.strip() for x in a.destinations.split(",") if x.strip()]
    used = sorted({v["dest"] for v in plan.values()})
    dests = declared or used

    # 目标夹：快照里已有的直接用；缺的先占位，由 apply_moves.py --create 建出来
    targets, create = {}, []
    for d in dests:
        f = by_title.get(d)
        if f:
            targets[d] = {"title": d, "media_id": int(f["id"]), "exists": True, "keys": []}
        else:
            targets[d] = {"title": d, "media_id": 0, "exists": False, "keys": []}
            create.append(d)

    # 冲突：视频当前已在别的分类夹里，且本次去向与之不同
    # 注意 move 是「加入目标夹 + 从源夹移除」，它**不会**把你原有的归属一并抹掉；
    # 所以这里只提示、不自动清理，是否去重交给人决定。
    conflicts, unknown = [], []
    for k, v in plan.items():
        rec = snap["videos"].get(k) or {}
        if v["dest"] not in targets:
            unknown.append((k, v))
            continue
        targets[v["dest"]]["keys"].append(k)
        also = [f for f in rec.get("folders", []) if f != src_title]
        if also and v["dest"] not in also:
            conflicts.append({"key": k, "bvid": v["bvid"],
                              "title": rec.get("title") or "", "dest": v["dest"],
                              "already_in": also})
    for d, v in targets.items():
        v["keys"].sort(key=lambda k: plan[k]["idx"])

    n_move = sum(len(v["keys"]) for v in targets.values())
    batches = {d: [v["keys"][i:i + a.batch] for i in range(0, len(v["keys"]), a.batch)]
               for d, v in targets.items() if v["keys"]}

    doc = {
        "src": src_title, "src_id": src_id, "batch": a.batch,
        "targets": targets, "create": create,
        "total": n_move, "conflicts": conflicts,
        "stats": {"by_dest": dict(Counter(v["dest"] for v in plan.values()).most_common()),
                  "batches": {d: len(b) for d, b in batches.items()}},
    }
    (wd / "move_plan.json").write_text(json.dumps(doc, ensure_ascii=False, indent=1),
                                       encoding="utf-8")

    lines = [f"# 预演单（未执行）", "",
             f"- 源收藏夹：**{src_title}**（id={src_id}）",
             f"- 待移动：**{n_move}** 条",
             f"- 目标夹：{len(targets)} 个（其中需新建 {len(create)} 个）",
             f"- 每批条数：{a.batch}（首次执行前用 apply_moves.py --probe 确认上限）", ""]
    if create:
        lines += [f"## 需要新建的收藏夹", ""] + [f"- {d}" for d in create] + [""]
    lines += ["## 逐条去向", "",
              "| # | BV | 标题 | → 去向 | 判定 |", "|---|---|---|---|---|"]
    for k, v in sorted(plan.items(), key=lambda kv: kv[1]["idx"]):
        rec = snap["videos"].get(k) or {}
        t = (rec.get("title") or "")[:44].replace("|", "/")
        mark = "拿不准" if v.get("conf") == "low" else ""
        lines.append(f"| {v['idx']} | {v['bvid'] or '—'} | {t} | {v['dest']} | {mark} |")
    if conflicts:
        lines += ["", "## 冲突（这些视频已在别的夹里，去向与之不同）", "",
                  "> move 只做「加入目标夹 + 从源夹移除」，**不会**删掉原有归属。",
                  "> 是否去重请自行决定，本工具不自动处理。", ""]
        for c in conflicts[:200]:
            lines.append(f"- #{c['key']} {c['bvid']} 已在 {'/'.join(c['already_in'])}，"
                         f"本次去向 {c['dest']}　{(c['title'] or '')[:40]}")
    if unknown:
        lines += ["", "## 未纳入的去向（不在允许集合里，已跳过）", ""]
        for k, v in unknown[:200]:
            lines.append(f"- #{v['idx']} {v['bvid']} → {v['dest']}")
    (wd / "move_plan.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    print(f"源「{src_title}」(id={src_id}) → {n_move} 条 / {len(targets)} 个目标夹")
    print("去向分布:", doc["stats"]["by_dest"])
    if create:
        print("需新建:", "、".join(create))
    print(f"冲突 {len(conflicts)} 条（已在别的夹且去向不同，不自动去重）")
    if unknown:
        print(f"⚠ 去向不在允许集合内、已跳过 {len(unknown)} 条")
    print(f"\n预演单 → {wd / 'move_plan.md'}")
    print("请先把预演单交给人过目，得到确认后再跑 apply_moves.py。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
