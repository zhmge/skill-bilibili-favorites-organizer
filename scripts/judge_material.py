# -*- coding: utf-8 -*-
"""把待判定视频整理成「阅读素材」，供模型逐条判定用。

判定之所以不写成脚本，是因为它依赖语义理解（标题 + 简介 + 封面），规则只能给候选。
本脚本只负责**把信息摆整齐**，并留下 idx → key 的映射，让判定结果能可靠地并回去。

用法（两遍判定：文本在先，封面按需）：
    # 第一遍：只要文本素材，不出接触表
    <python> scripts/judge_material.py --workdir DIR --no-sheet

    # 第二遍：只为「文本判不动」的那些条目出接触表
    <python> scripts/judge_material.py --workdir DIR --sheet-idx judge/need_cover.json

    通用参数：[--folder 默认收藏夹] [--batch 115] [--history history.json]
              [--no-sheet] [--cols 4] [--rows 4]

产物：
    <workdir>/judge/batch_NN.txt       阅读素材，分批（每批约 batch 条），**恒含全部条目**
    <workdir>/judge/batch_NN_sheet_NN.jpg  全量接触表（未给 --sheet-idx 时）
    <workdir>/judge/batch_NN_need_NN.jpg   按需接触表（给了 --sheet-idx 时）
    <workdir>/judge/index.json         {"folder":…, "rows":[{"idx","key","bvid"}]}

索引必须保留：判定结果按 idx 回填，idx 与视频的对应关系只有这里知道。

**封面按需看，不逐条看。** 读图要花 token，而多数条目看标题就够。默认只读文本；
仅当文本判不动时才升级看图（触发条件见 references/classification-rules.md）。
--sheet-idx 接受两种输入：
    [12, 45, 300]                              显式 idx 数组
    {"12": ["杂项", "理由", "low"], "45": …}    判定文件，取置信度为 low 的
封面必须已经由 `covers.py fetch` 落盘到 <workdir>/covers/<bvid>.webp。

history.json（可选，用来复用上一次的结论）：
    {"<bvid>": {"dest": "游戏开发", "why": "…", "source": "沿用历史稿"}}
"""
from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path

CELL_W, CELL_H = 300, 188      # 封面绘制区（保持 320x200 的比例）
LABEL_H = 26                   # 编号条
GAP = 6
BG = (255, 255, 255)
BORDER = (222, 226, 230)
LABEL_FG = (28, 32, 38)
LABEL_MUTED = (150, 156, 165)


def fmt_dur(s) -> str:
    try:
        s = int(s)
    except (TypeError, ValueError):
        return "-"
    if s <= 0:
        return "-"
    h, m, sec = s // 3600, (s % 3600) // 60, s % 60
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"


def _font(size: int):
    from PIL import ImageFont
    for p in ("C:/Windows/Fonts/msyh.ttc", "C:/Windows/Fonts/simhei.ttf",
              "/System/Library/Fonts/PingFang.ttc",
              "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"):
        try:
            return ImageFont.truetype(p, size)
        except Exception:
            continue
    return ImageFont.load_default(size=size)


def build_sheet(items, covers_dir: Path, cols: int, rows: int, dest: Path) -> tuple[int, int]:
    """把一批封面拼成接触表。items: [(idx, bvid)]。返回 (成功图数, 缺失图数)。"""
    from PIL import Image, ImageDraw

    per = cols * rows
    ok = miss = 0
    w = cols * CELL_W + (cols + 1) * GAP
    h = rows * (CELL_H + LABEL_H) + (rows + 1) * GAP
    sheet = Image.new("RGB", (w, h), BG)
    dr = ImageDraw.Draw(sheet)
    f_big, f_sm = _font(19), _font(15)

    for n, (idx, bv) in enumerate(items[:per]):
        r, c = divmod(n, cols)
        x = GAP + c * (CELL_W + GAP)
        y = GAP + r * (CELL_H + LABEL_H + GAP)
        box = (x, y, x + CELL_W, y + CELL_H)

        src = covers_dir / f"{bv}.webp" if bv else None
        drawn = False
        if src and src.is_file():
            try:
                im = Image.open(src).convert("RGB")
                im.thumbnail((CELL_W, CELL_H), Image.LANCZOS)
                sheet.paste(im, (x + (CELL_W - im.width) // 2,
                                 y + (CELL_H - im.height) // 2))
                drawn, ok = True, ok + 1
            except Exception:
                pass
        if not drawn:
            dr.rectangle(box, fill=(244, 246, 248))
            dr.text((x + 10, y + CELL_H // 2 - 10), "无封面 / 加载失败",
                    font=f_sm, fill=LABEL_MUTED)
            miss += 1
        dr.rectangle(box, outline=BORDER, width=1)

        # 编号条：★ 判定结果按这个 idx 回填，不要认错
        dr.text((x + 2, y + CELL_H + 3), f"#{idx}", font=f_big, fill=LABEL_FG)
        if bv:
            dr.text((x + 62, y + CELL_H + 6), bv, font=f_sm, fill=LABEL_MUTED)

    sheet.save(dest, "JPEG", quality=88)
    return ok, miss


def parse_need_idx(raw) -> set:
    """解析 --sheet-idx 的输入，返回需要看封面的 idx 集合。

    接受：
        [12, 45, 300]                            显式 idx 数组
        {"12": ["杂项", "理由", "low"], …}        判定文件 → 取置信度为 low 的
        {"12": true, …}                          显式打标
    """
    out = set()
    if isinstance(raw, list):
        for x in raw:
            try:
                out.add(int(x))
            except (TypeError, ValueError):
                pass
    elif isinstance(raw, dict):
        for k, v in raw.items():
            try:
                i = int(k)
            except (TypeError, ValueError):
                continue
            if v is True:
                out.add(i)
            elif isinstance(v, str) and v.lower() == "low":
                out.add(i)
            elif isinstance(v, list) and len(v) >= 3 and str(v[2]).lower() == "low":
                out.add(i)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--workdir", default=None)
    ap.add_argument("--folder", default="",
                    help="要生成判定素材的收藏夹名；留空 = 默认收藏夹")
    ap.add_argument("--batch", type=int, default=115)
    ap.add_argument("--history", default=None)
    ap.add_argument("--no-sheet", action="store_true",
                    help="不生成封面接触表（第一遍判定用这个）")
    ap.add_argument("--sheet-idx", default=None,
                    help="只给这些 idx 出接触表；JSON 数组或判定文件路径。"
                         "不给 = 全量出表")
    ap.add_argument("--cols", type=int, default=4)
    ap.add_argument("--rows", type=int, default=4)
    a = ap.parse_args()

    wd = Path(a.workdir or os.environ.get("BILI_ORG_WORKDIR") or "./bili_org_work").expanduser().resolve()
    snap_p = wd / "snapshot.json"
    if not snap_p.is_file():
        raise SystemExit(f"错误：{snap_p} 不存在。请先跑 collect.py。")
    snap = json.loads(snap_p.read_text(encoding="utf-8"))

    folder = a.folder or snap.get("default_folder_title") or "默认收藏夹"
    auth = snap.get("folder_items_authoritative") or {}
    if folder not in auth:
        raise SystemExit(f"错误：收藏夹「{folder}」不在快照里。"
                         f"现有：{list(auth.keys())}")

    hist = {}
    if a.history:
        hist = json.loads(Path(a.history).read_text(encoding="utf-8"))
    elif (wd / "history.json").is_file():
        hist = json.loads((wd / "history.json").read_text(encoding="utf-8"))

    videos = snap["videos"]
    keys = auth[folder]
    out, index = [], []
    for idx, k in enumerate(keys, 1):
        v = videos.get(k) or {}
        bv = v.get("bvid") or ""
        h = hist.get(bv) or {}
        intro = (v.get("intro") or "").replace("\n", " ").strip()
        if len(intro) > 160:
            intro = intro[:160] + "…"
        also = [f for f in (v.get("folders") or []) if f != folder]
        tags = []
        if v.get("from_ids_only"):
            tags.append("[接口不可见/疑似失效]")
        if v.get("is_invalid"):
            tags.append("[已失效]")
        if not (wd / "covers" / f"{bv}.webp").is_file():
            tags.append("[无封面]")
        line = (
            f"[{idx:>3}] {bv or '(无BVID)'} ｜ {fmt_dur(v.get('duration'))} ｜ "
            f"{v.get('page') or 1}P ｜ 播放 {int(v.get('play') or 0):,}"
            + (" " + " ".join(tags) if tags else "") + "\n"
            f"      标题: {v.get('title') or '(空)'}\n"
            f"      UP: {v.get('up_name') or '(未知)'}"
            + (f" ｜ 历史结论: {h.get('dest')}" +
               (f"（{h.get('why')}）" if h.get("why") else "") if h else " ｜ 历史结论: （无）")
            + (f" ｜ 同时也在: {'/'.join(also)}" if also else "") + "\n"
            f"      简介: {intro or '(空)'}"
        )
        out.append(line)
        index.append({"idx": idx, "key": k, "bvid": bv})

    need = None
    if a.sheet_idx:
        if a.no_sheet:
            raise SystemExit("错误：--sheet-idx 与 --no-sheet 不能同时用。")
        raw = json.loads(Path(a.sheet_idx).read_text(encoding="utf-8"))
        need = parse_need_idx(raw)
        known = {x["idx"] for x in index}
        unknown = sorted(need - known)
        if unknown:
            print(f"  ! --sheet-idx 里有 {len(unknown)} 个 idx 不在本次范围：{unknown[:10]}")
        need &= known
        if not need:
            raise SystemExit("错误：--sheet-idx 解析后为空，没有需要看图的条目。")

    jd = wd / "judge"
    jd.mkdir(parents=True, exist_ok=True)
    covers_dir = wd / "covers"
    files, sheets, n_ok, n_miss = [], 0, 0, 0
    for i in range(0, len(out), a.batch):
        bno = i // a.batch + 1
        p = jd / f"batch_{bno:02d}.txt"
        p.write_text("\n\n".join(out[i:i + a.batch]), encoding="utf-8")
        files.append(p.name)
        if not a.no_sheet:
            chunk = index[i:i + a.batch]
            tag = "sheet"
            if need is not None:
                chunk = [x for x in chunk if x["idx"] in need]
                tag = "need"
            per = a.cols * a.rows
            for s, j in enumerate(range(0, len(chunk), per), 1):
                sheet_p = jd / f"batch_{bno:02d}_{tag}_{s:02d}.jpg"
                try:
                    ok, miss = build_sheet(
                        [(x["idx"], x["bvid"]) for x in chunk[j:j + per]],
                        covers_dir, a.cols, a.rows, sheet_p)
                    n_ok, n_miss, sheets = n_ok + ok, n_miss + miss, sheets + 1
                except Exception as e:
                    print(f"  ! 接触表 {sheet_p.name} 生成失败：{type(e).__name__}: {e}")
    (jd / "index.json").write_text(json.dumps(
        {"folder": folder, "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
         "total": len(index), "rows": index}, ensure_ascii=False, indent=1),
        encoding="utf-8")

    print(f"收藏夹「{folder}」共 {len(out)} 条 → {len(files)} 批：{files}")
    print(f"总字符 {sum(len(x) for x in out):,}")
    if need is not None:
        print(f"按需看图：{len(need)}/{len(index)} 条需要封面 → 接触表 {sheets} 张")
    if sheets:
        print(f"封面接触表 {sheets} 张（每张 {a.cols}x{a.rows}）→ {n_ok} 张有图 / {n_miss} 张缺图")
        if n_miss and not covers_dir.is_dir():
            print("  ! covers/ 目录不存在，请先跑 covers.py fetch")
    elif a.no_sheet or need is not None:
        print("未出接触表：按文本判定即可；判不动的条目收集 idx 后用 --sheet-idx 补图。")
    print(f"索引 → {jd / 'index.json'}")
    print("判定时 素材 txt 与 接触表 jpg 同名配对，按表上的 #idx 回填结果。")
    if hist:
        hit = sum(1 for r in index if hist.get(r["bvid"]))
        print(f"命中历史结论 {hit}/{len(index)}（判定时优先沿用，口径不一致再改）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
