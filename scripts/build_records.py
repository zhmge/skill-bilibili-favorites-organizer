# -*- coding: utf-8 -*-
"""把快照 + 封面账本 + 整理计划（+ 可选描述/字幕索引）组装成数据表记录。

只产出 JSON 分片，不碰网络。写库由资料库 skill 的 batch_add_database_records.py 完成。

用法：
    <python> scripts/build_records.py --workdir DIR [--batch 100]

可选输入（有就用，没有就跳过对应字段）：
    <workdir>/desc/*.json         {"<bvid>": {"subcat": "…", "summary": "…"}}
    <workdir>/subs_index.json     [{"bvid":…, "status":…, "n_resolved":…,
                                    "n_bili_srt":…, "n_asr_srt":…,
                                    "n_parts":…, "n_parts_taken":…}, …]

产物：
    <workdir>/records/records_batch_NN.json   （裸数组，需由调用方包 {database_id, records}）
"""
from __future__ import annotations

import argparse
import json
import os
import time
from collections import Counter
from pathlib import Path

# 文案：接口不可见的条目在表里必须一眼可辨，不要让它们伪装成正常标题
INVISIBLE_TITLE = "【已失效/接口不可见】"


def fmt_dur(s) -> str:
    try:
        s = int(s)
    except (TypeError, ValueError):
        return ""
    if s <= 0:
        return ""
    h, m, sec = s // 3600, (s % 3600) // 60, s % 60
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"


def load(p: Path, default):
    return json.loads(p.read_text(encoding="utf-8")) if p.is_file() else default


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--workdir", default=None)
    ap.add_argument("--batch", type=int, default=100)
    a = ap.parse_args()

    wd = Path(a.workdir or os.environ.get("BILI_ORG_WORKDIR") or "./bili_org_work").expanduser().resolve()
    snap_p = wd / "snapshot.json"
    if not snap_p.is_file():
        raise SystemExit(f"错误：{snap_p} 不存在。请先跑 collect.py。")
    snap = json.loads(snap_p.read_text(encoding="utf-8"))
    videos = snap["videos"]
    default_title = snap.get("default_folder_title") or "默认收藏夹"
    default_keys = set((snap.get("folder_items_authoritative") or {}).get(default_title, []))

    covers = load(wd / "covers.json", {})
    plan = load(wd / "reclass_plan.json", {}).get("plan", {})

    desc = {}
    for p in sorted((wd / "desc").glob("*.json")) if (wd / "desc").is_dir() else []:
        desc.update(json.loads(p.read_text(encoding="utf-8")))
    if not desc:
        print("（无 desc/*.json，跳过「二级子类」「内容描述」）")

    subs_by_bv = {r.get("bvid"): r for r in load(wd / "subs_index.json", [])}
    if not subs_by_bv:
        print("（无 subs_index.json，跳过「字幕状态」「字幕来源」「收录分P」）")

    # 夹序以快照里的实际顺序为准，不硬编码夹名
    folder_order = [f.get("title") for f in snap["folders"]
                    if f.get("title") and f.get("title") != default_title]

    def sort_key(k: str):
        v = videos[k]
        if k in default_keys:
            return (0, (plan.get(k) or {}).get("idx", 99999), 0)
        others = [f for f in v.get("folders", []) if f != default_title]
        fi = folder_order.index(others[0]) if others and others[0] in folder_order else 99
        return (1, fi, v["key"])

    records, no_cover = [], 0
    for i, k in enumerate(sorted(videos.keys(), key=sort_key), 1):
        v = videos[k]
        bv = (v.get("bvid") or "").strip()
        p = plan.get(k)
        in_default = k in default_keys
        also = [f for f in v.get("folders", []) if f != default_title]

        title = v.get("title") or ""
        up = v.get("up_name") or ""
        if v.get("from_ids_only"):
            title, up = INVISIBLE_TITLE, "—"

        props = {
            "序号": {"number": i},
            "视频标题": {"text": title},
            "时长": {"text": fmt_dur(v.get("duration"))},
            "分P数": {"number": int(v.get("page") or 1)},
            "播放量": {"number": int(v.get("play") or 0)},
        }
        if bv:
            props["BV号"] = {"text": bv}
            props["视频链接"] = {"url": {"text": "B站",
                                        "link": f"https://www.bilibili.com/video/{bv}"}}
        if up and up != "—":
            props["UP主"] = {"text": up}

        cov = (covers.get(bv) or {}).get("url")
        if cov:
            props["封面"] = {"image": {"images": [{"title": "封面", "imageUrl": cov,
                                                   "width": 320, "height": 200}]}}
        else:
            no_cover += 1

        ft = v.get("fav_time") or 0
        if ft:
            props["收藏时间"] = {"date": time.strftime("%Y-%m-%d", time.localtime(ft))}
        if also:
            props["所属收藏夹"] = {"multi_select": also}

        if p:
            props["建议去向"] = {"select": p["dest"]}
            props["最终去向"] = {"select": p["dest"]}
            props["判定来源"] = {"select": p.get("source") or "AI"}
            props["拿不准"] = {"checkbox": p.get("conf") == "low"}
            props["判定理由"] = {"text": p["why"]}

        d = desc.get(bv) or {}
        if d.get("subcat"):
            props["二级子类"] = {"select": d["subcat"]}
        if d.get("summary"):
            props["内容描述"] = {"text": d["summary"]}

        ir = subs_by_bv.get(bv)
        if ir:
            st = ir.get("status") or ""
            if "失效" in st:
                ss = "视频失效"
            elif ir.get("n_resolved", 0) > 0:
                ss = "有字幕" if ir.get("n_bili_srt", 0) > 0 else "ASR生成"
            elif "待定" in st or "未处理" in st:
                ss = "待定"
            else:
                ss = "无字幕"
            props["字幕状态"] = {"select": ss}
            props["字幕来源"] = {"select": ("B站原生" if ir.get("n_bili_srt", 0) > 0 else
                                          "本地ASR" if ir.get("n_asr_srt", 0) > 0 else "无")}
            props["收录分P"] = {"text": f"{ir.get('n_parts_taken', 0)}/{ir.get('n_parts', 1)}"}

        if in_default:
            props["整理状态"] = {"select": "待整理"}
        elif bv and desc.get(bv, {}).get("summary"):
            props["整理状态"] = {"select": "已入清单页"}
        elif ir:
            props["整理状态"] = {"select": "已提取字幕"}
        else:
            props["整理状态"] = {"select": "已归档"}

        records.append(props)

    rd = wd / "records"
    rd.mkdir(parents=True, exist_ok=True)
    for old in rd.glob("records_batch_*.json"):
        old.unlink()
    files = []
    for i in range(0, len(records), a.batch):
        chunk = records[i:i + a.batch]
        p = rd / f"records_batch_{i // a.batch + 1:02d}.json"
        p.write_text(json.dumps(chunk, ensure_ascii=False, indent=1), encoding="utf-8")
        files.append((p.name, len(chunk)))

    print(f"\n共 {len(records)} 条记录 → {len(files)} 批（每批 ≤ {a.batch}）")
    for n, c in files:
        print(f"  {n}: {c} 条")
    print(f"无封面 {no_cover} 条")
    print("整理状态:", dict(Counter(r["整理状态"]["select"] for r in records)))
    if plan:
        print("建议去向:", dict(Counter(r["建议去向"]["select"]
                                       for r in records if "建议去向" in r).most_common()))
    print("有内容描述:", sum(1 for r in records if "内容描述" in r))
    print("有二级子类:", sum(1 for r in records if "二级子类" in r))
    print(f"\n下一步是把每批包成 {{\"database_id\": …, \"records\": [...]}} 再交给资料库 skill："
          f"\n  注意 batch_add 的 success=true **不代表已落库**，写完必须用 db_read.py 回读核验。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
