# -*- coding: utf-8 -*-
"""采集你账号下全部收藏夹 → 冻结快照。

一次跑完两件事，缺一不可：
  1. /resource/list  逐夹拉元数据（标题/UP/时长/播放/封面/简介）
  2. /resource/ids   逐夹拉**权威全集**，补齐 list 静默漏掉的「幽灵条目」

用法：
    BILI_SESSDATA=... <python> scripts/collect.py [--workdir DIR] [--mid MID]

产物（全在 workdir 下）：
    snapshot.json    本次采集的完整快照，后续所有步骤都读它
    api_log.jsonl    请求日志（只记 path/code/耗时，不含凭据）

快照里两个条目清单的用途不同，别混：
    folder_items                list 接口所见，可用于取元数据
    folder_items_authoritative  ids 接口所见，**终态断言的唯一基准**（见 references/api-behavior.md）
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import bili_api as bili  # noqa: E402


def _workdir(cli: str | None) -> Path:
    raw = cli or os.environ.get("BILI_ORG_WORKDIR") or "./bili_org_work"
    p = Path(raw).expanduser().resolve()
    p.mkdir(parents=True, exist_ok=True)
    return p


def _record(it: dict) -> dict:
    """把 /resource/list 的一条 medias 元素规范化成快照记录。"""
    up = it.get("upper") or {}
    cnt = it.get("cnt_info") or {}
    return {
        "key": f"{it.get('id')}:{it.get('type', 2)}",
        "aid": it.get("id"), "type": it.get("type", 2),
        "bvid": it.get("bvid") or "",
        "title": it.get("title") or "",
        "cover": it.get("cover") or "",
        "intro": (it.get("intro") or "").strip(),
        "page": it.get("page") or 1,
        "duration": it.get("duration") or 0,
        "up_mid": up.get("mid"), "up_name": up.get("name") or "",
        "play": cnt.get("play") or 0,
        "collect": cnt.get("collect") or 0,
        "danmaku": cnt.get("danmaku") or 0,
        "pubtime": it.get("pubtime") or 0,
        "fav_time": it.get("fav_time") or 0,
        "attr": it.get("attr"),
        "is_invalid": (not it.get("bvid")),
        "folders": [],
    }


def _blank(key: str, aid, typ) -> dict:
    """幽灵条目的占位记录（list 没返回过，字段待 view 接口补）。"""
    return {
        "key": key, "aid": aid, "type": typ, "bvid": "", "title": "", "cover": "",
        "intro": "", "page": 1, "duration": 0, "up_mid": None, "up_name": "",
        "play": 0, "collect": 0, "danmaku": 0, "pubtime": 0, "fav_time": 0,
        "attr": None, "is_invalid": False, "folders": [], "from_ids_only": True,
    }


def _fill_by_view(rec: dict, aid) -> None:
    """幽灵条目补元数据。补不到就如实标成不可见，不猜、不跳过。"""
    try:
        d = bili.call("/x/web-interface/view", {"aid": aid})
        rec.update({
            "bvid": d.get("bvid") or "", "title": d.get("title") or "",
            "cover": d.get("pic") or "",
            "intro": (d.get("desc") or "").strip()[:500],
            "page": d.get("videos") or 1, "duration": d.get("duration") or 0,
            "up_mid": (d.get("owner") or {}).get("mid"),
            "up_name": (d.get("owner") or {}).get("name") or "",
            "pubtime": d.get("pubdate") or 0,
            "is_invalid": (d.get("state", 0) != 0),
        })
    except bili.BiliError as e:
        rec["is_invalid"] = True
        rec["title"] = f"【接口不可见】aid={aid}"
        rec["note"] = f"view 接口返回 {e.code}: {e.message}"
    except Exception as e:  # noqa: BLE001
        rec["is_invalid"] = True
        rec["title"] = f"【接口不可见】aid={aid}"
        rec["note"] = str(e)[:120]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--workdir", default=None)
    ap.add_argument("--mid", type=int, default=None)
    args = ap.parse_args()

    wd = _workdir(args.workdir)
    bili.MIN_INTERVAL = 1.1          # 0.5 实测触发 HTTP 412 风控，不要调低
    bili.set_logfile(wd / "api_log.jsonl")

    mid = args.mid or bili.my_mid()
    print(f"工作目录 {wd}\n登录 mid={mid}")

    print("\n== 列出收藏夹 ==")
    folders = bili.list_folders(mid)
    default = [f for f in folders if bili.is_default_folder(f)]
    for f in folders:
        mark = "  ← 默认收藏夹" if bili.is_default_folder(f) else ""
        print(f"  id={f.get('id'):>12}  attr={f.get('attr'):>3}  "
              f"声明 {f.get('media_count'):>4} 条  {f.get('title')}{mark}")
    if not default:
        # 兜底：attr 语义若被 B站改掉，按标题找一次，总比直接失败好
        default = [f for f in folders
                   if (f.get("title") or "").strip() in ("默认收藏夹", "默认")]
        if default:
            print("  注意：attr 未能识别出默认夹，已按标题兜底命中。请人工确认。")
    if not default:
        print("错误：没找到默认收藏夹（attr 恰好为 1 的夹）。"
              "若 B站改了 attr 语义，请在 collect.py 里显式指定夹名。", file=sys.stderr)
        return 2
    if len(default) > 1:
        print(f"  警告：识别出 {len(default)} 个候选默认夹，取第一个："
              f"{default[0].get('title')}（请人工确认）")
    print(f"默认收藏夹 = {default[0].get('title')} (id={default[0].get('id')})")

    print("\n== 逐夹采集（元数据 + 权威全集）==")
    videos: dict[str, dict] = {}
    folder_items: dict[str, list[str]] = {}
    authoritative: dict[str, list[str]] = {}
    ghosts: dict[str, list[dict]] = {}
    t0 = time.time()

    for f in folders:
        fid, title = int(f["id"]), f.get("title") or ""
        seen: list[str] = []
        for it in bili.folder_items(fid):
            rec = _record(it)
            k = rec["key"]
            seen.append(k)
            cur = videos.get(k)
            if cur is None:
                videos[k] = rec
            if title not in videos[k]["folders"]:
                videos[k]["folders"].append(title)

        ids = bili.folder_ids(fid)
        auth = [f"{x['aid']}:{x['type']}" for x in ids]
        listed = set(seen)
        ghost = []
        for x in ids:
            k = f"{x['aid']}:{x['type']}"
            if k in listed:
                continue
            rec = videos.get(k)
            if rec is None:
                rec = _blank(k, x["aid"], x["type"])
                _fill_by_view(rec, x["aid"])
                videos[k] = rec
            if title not in rec["folders"]:
                rec["folders"].append(title)
            ghost.append({"key": k, "aid": x["aid"], "bvid": rec.get("bvid") or "",
                          "title": rec.get("title") or "",
                          "invalid": bool(rec.get("is_invalid"))})

        folder_items[title] = seen
        authoritative[title] = auth
        ghosts[title] = ghost
        gap = f"  ⚠ 少 {len(ghost)} 条" if ghost else ""
        print(f"  {title:<12} list {len(seen):>4} | ids {len(auth):>4} | "
              f"声明 {f.get('media_count'):>4}{gap}")
        for g in ghost:
            print(f"      + {g['key']:<16} {g['bvid'] or '-':<14} "
                  f"invalid={g['invalid']}  {str(g['title'])[:40]}")

    dup = {k: v["folders"] for k, v in videos.items() if len(v["folders"]) > 1}
    snapshot = {
        "collected_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "mid": mid,
        "folders": folders,
        "default_folder_title": default[0].get("title") or "",
        "default_folder_id": int(default[0]["id"]),
        "folder_items": folder_items,
        "folder_items_authoritative": authoritative,
        "ghosts": ghosts,
        "videos": videos,
        "stats": {
            "n_folders": len(folders),
            "list_rows": sum(len(v) for v in folder_items.values()),
            "authoritative_rows": sum(len(v) for v in authoritative.values()),
            "unique_videos": len(videos),
            "cross_folder_dups": len(dup),
            "declared_total": sum(int(f.get("media_count") or 0) for f in folders),
            "ghost_total": sum(len(v) for v in ghosts.values()),
            "elapsed_sec": round(time.time() - t0, 1),
            "requests": bili.stats(),
        },
    }
    p = wd / "snapshot.json"
    p.write_text(json.dumps(snapshot, ensure_ascii=False, indent=1), encoding="utf-8")

    s = snapshot["stats"]
    print("\n== 汇总 ==")
    print(f"收藏夹 {s['n_folders']} 个 | 声明 {s['declared_total']} 条 | "
          f"list 实拉 {s['list_rows']} | ids 权威 {s['authoritative_rows']}")
    print(f"去重后视频 {s['unique_videos']} | 跨夹重复 {s['cross_folder_dups']} | "
          f"幽灵条目 {s['ghost_total']}")
    print(f"请求 {s['requests']} 次 | 耗时 {s['elapsed_sec']}s")
    print(f"→ {p}")
    if s["ghost_total"]:
        print("\n注意：list 与 ids 条数不一致是**预期行为**，不是 bug。"
              "终态断言用 folder_items_authoritative。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
