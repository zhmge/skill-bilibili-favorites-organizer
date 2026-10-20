# -*- coding: utf-8 -*-
"""合并逐条判定结果 → 可执行的整理计划，并在合并时做**三重断言**。

判定是模型逐条读素材做的，格式自由但容易漏条目。本脚本把「漏了」变成硬错误，
而不是让一条视频悄悄留在默认收藏夹里 —— 这正是上一次整理失败的方式。

用法：
    <python> scripts/merge_verdicts.py --workdir DIR
        --destinations "计算机,游戏开发,AI,…,杂项"
        [--verdict-dir DIR] [--glob 'verdict_*.json']

判定文件格式（每个文件一个 JSON 对象，键是 idx 字符串）：
    {"1": ["游戏开发", "标题含 Spine 骨骼教程，属 2D 骨骼动画", "high"], …}

产物：
    <workdir>/reclass_plan.json
        {"folder":…, "destinations":[…],
         "plan": {"<aid:type>": {"idx","bvid","dest","why","conf"}},
         "stats": {…}}
"""
from __future__ import annotations

import argparse
import glob as globmod
import json
import os
import time
from collections import Counter
from pathlib import Path


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--workdir", default=None)
    ap.add_argument("--destinations", required=True,
                    help="允许的去向集合，逗号分隔（含兜底夹）")
    ap.add_argument("--history", default=None,
                    help="历史结论文件（{\"<bvid>\": {...}}）。命中者标注为「沿用历史稿」，"
                         "便于日后区分「模型新判的」和「以前判过的」")
    ap.add_argument("--verdict-dir", default=None, help="默认 <workdir>/judge")
    ap.add_argument("--glob", default="verdict_*.json")
    a = ap.parse_args()

    wd = Path(a.workdir or os.environ.get("BILI_ORG_WORKDIR") or "./bili_org_work").expanduser().resolve()
    vd = Path(a.verdict_dir).expanduser() if a.verdict_dir else (wd / "judge")
    idx_p = vd / "index.json"
    if not idx_p.is_file():
        raise SystemExit(f"错误：{idx_p} 不存在。请先跑 judge_material.py。")

    index = json.loads(idx_p.read_text(encoding="utf-8"))
    rows = {int(r["idx"]): r for r in index["rows"]}
    dests = [x.strip() for x in a.destinations.split(",") if x.strip()]

    hist_path = Path(a.history).expanduser() if a.history else (wd / "history.json")
    hist = json.loads(hist_path.read_text(encoding="utf-8")) if hist_path.is_file() else {}
    if hist:
        print(f"历史结论 {len(hist)} 条 ← {hist_path}")

    files = sorted(Path(p) for p in globmod.glob(str(vd / a.glob)))
    if not files:
        raise SystemExit(f"错误：{vd} 下没有匹配 {a.glob} 的判定文件。")

    raw: dict[int, list] = {}
    dup: list[int] = []
    bad_shape: list[str] = []
    for f in files:
        d = json.loads(f.read_text(encoding="utf-8"))
        n = 0
        for k, v in d.items():
            # 判定文件里允许带 _note / _folders 这类元数据键（下划线开头），跳过
            if not str(k).lstrip("-").isdigit():
                continue
            if not isinstance(v, (list, tuple)) or not v:
                bad_shape.append(f"{f.name}:{k}")
                continue
            i = int(k)
            if i in raw:
                dup.append(i)
            raw[i] = list(v)
            n += 1
        print(f"  {f.name}: {n} 条")
    if bad_shape:
        print("\n以下条目的值不是 [去向, 理由, 置信度] 形式，已中止：")
        for x in bad_shape[:20]:
            print("  ✗", x)
        return 2

    # ── 三重断言 ──────────────────────────────────────────────
    missing = sorted(set(rows) - set(raw))       # 该判没判
    extra = sorted(set(raw) - set(rows))         # 判了不存在的
    bad_dest = sorted(i for i, v in raw.items() if not v or v[0] not in dests)
    no_why = sorted(i for i, v in raw.items() if len(v) < 2 or not str(v[1]).strip())

    fails = []
    if missing:
        fails.append(f"未判定 {len(missing)} 条：{missing[:20]}{' …' if len(missing) > 20 else ''}")
    if extra:
        fails.append(f"多出 {len(extra)} 条不属于本收藏夹的 idx：{extra[:20]}")
    if dup:
        fails.append(f"重复 idx {len(dup)} 个：{sorted(set(dup))[:20]}")
    if bad_dest:
        fails.append(f"去向非法或为空 {len(bad_dest)} 条：{bad_dest[:20]}")
    if no_why:
        fails.append(f"缺判定理由 {len(no_why)} 条：{no_why[:20]}")
    if fails:
        print("\n三重断言未通过，已中止：")
        for x in fails:
            print("  ✗", x)
        return 2
    print("\n三重断言通过：未判定 0 · 多出 0 · 重复 0 · 非法去向 0 · 缺理由 0")

    plan = {}
    for i, v in raw.items():
        r = rows[i]
        src = "沿用历史稿" if hist and r.get("bvid") and r["bvid"] in hist else "AI"
        plan[r["key"]] = {"idx": i, "bvid": r["bvid"], "dest": v[0],
                          "why": str(v[1]).strip(),
                          "conf": (v[2] if len(v) > 2 else "high"),
                          "source": src}

    dist = Counter(x["dest"] for x in plan.values())
    low = sum(1 for x in plan.values() if x["conf"] == "low")
    reused = sum(1 for x in plan.values() if x["source"] == "沿用历史稿")
    out = {"folder": index["folder"], "destinations": dests, "plan": plan,
           "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
           "stats": {"total": len(plan), "low_confidence": low,
                     "reused_history": reused,
                     "by_dest": dict(dist.most_common())}}
    p = wd / "reclass_plan.json"
    p.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")

    print(f"\n共 {len(plan)} 条 | 拿不准 {low} 条 | 沿用历史稿 {reused} 条")
    for k, v in dist.most_common():
        print(f"  {k:<8} {v}")
    print(f"→ {p}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
