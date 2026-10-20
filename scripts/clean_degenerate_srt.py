#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""清理 SRT 里两类**非语音**产物（解码幻觉，不是口癖）。

静音期 whisper 会按帧吐 token，产物是「连续几段一模一样的超短文本」。
两条判据，满足其一即删：

  Rule A 幻觉串：连续的 ≥4 段文本完全相同且长度 ≤2 字
                 典型：idx 145–164 是 20 段 `呃`，每段 0.02–0.14s。
                 → 整串删除。
  Rule B 回声：  连续的 ≥2 段文本相同，且**第 2 段起**每段都 <0.15s
                 典型：`notepad` 2.12s + `notepad` 0.06s + `notepad` 0.02s。
                 → 保留第 1 段，删除其余。

0.15s 这个门槛是刻意的：真实音节不可能短于它，所以不会误删。
**时长达标的口癖要保留**（`对不对` 连说两遍、`点击浏览` 连说两遍都留着）。

实现上的两个必要小心（都踩过）：
  · 按「≥2 行且其中一行匹配时间行」判块，**不能用「≥3 行」** ——
    SRT 里存在「只有序号 + 时间行、文本为空」的 cue，按 3 行判会静默丢掉它。
  · 文件首块可能是来源链接（不是 cue），要原样保留。

用法：
    python clean_degenerate_srt.py <文件或目录> [...]          # 预演，只打印
    python clean_degenerate_srt.py <文件或目录> [...] --write  # 就地改写并重新编号
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

TS = re.compile(r"(\d+):(\d+):(\d+)[,.](\d+)\s*-->\s*(\d+):(\d+):(\d+)[,.](\d+)")


def _secs(g: tuple[int, ...]) -> float:
    return g[0] * 3600 + g[1] * 60 + g[2] + g[3] / 1000.0


def parse_srt(text: str) -> tuple[list[str], list[dict]]:
    """返回 (非 cue 的头部块, cue 列表)。"""
    head, cues = [], []
    for blk in re.split(r"\r?\n\s*\r?\n", text.strip()):
        lines = [x for x in blk.splitlines() if x.strip()]
        if not lines:
            continue
        hit = next((k for k, x in enumerate(lines[:2]) if TS.search(x)), None)
        if hit is None:
            head.append(blk.strip())
            continue
        g = tuple(int(x) for x in TS.search(lines[hit]).groups())
        cues.append({"a": _secs(g[:4]), "b": _secs(g[4:]),
                     "text": " ".join(lines[hit + 1:]).strip()})
    return head, cues


def _fmt(t: float) -> str:
    ms = int(round(t * 1000))
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def find_drops(cues: list[dict]) -> tuple[set[int], list[str]]:
    drop: set[int] = set()
    notes: list[str] = []
    i = 0
    while i < len(cues):
        # Rule A（空文本不参与：它在页面里只是一段留白，不是幻觉）
        if cues[i]["text"] and len(cues[i]["text"]) <= 2:
            j = i
            while j + 1 < len(cues) and cues[j + 1]["text"] == cues[i]["text"]:
                j += 1
            if j - i + 1 >= 4:
                drop.update(range(i, j + 1))
                notes.append(f"  A 幻觉串 idx {i}–{j}（{j - i + 1} 段 "
                             f"{cues[i]['text']!r}，{cues[i]['a']:.1f}s→{cues[j]['b']:.1f}s）")
                i = j + 1
                continue
        # Rule B
        j = i
        while (j + 1 < len(cues) and cues[j + 1]["text"] == cues[i]["text"]
               and cues[i]["text"] and (cues[j + 1]["b"] - cues[j + 1]["a"]) < 0.15):
            j += 1
        if j > i:
            drop.update(range(i + 1, j + 1))
            notes.append(f"  B 回声 idx {i + 1}–{j}（保留 idx {i} "
                         f"{cues[i]['text']!r} {cues[i]['b'] - cues[i]['a']:.2f}s，"
                         f"删除其后 {j - i} 段 <0.15s）")
            i = j + 1
            continue
        i += 1
    return drop, notes


def targets(args: list[str]) -> list[Path]:
    out: list[Path] = []
    for a in args:
        p = Path(a)
        if p.is_dir():
            out += sorted(p.rglob("*.srt"))
        elif p.is_file():
            out.append(p)
        else:
            print(f"跳过（不存在）: {a}", file=sys.stderr)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="清理 SRT 里的解码幻觉段")
    ap.add_argument("paths", nargs="+", help="SRT 文件或目录（目录会递归找 *.srt）")
    ap.add_argument("--write", action="store_true", help="就地改写；不带则只预演")
    a = ap.parse_args()

    total, files = 0, targets(a.paths)
    if not files:
        print("没有找到任何 .srt", file=sys.stderr)
        return 2

    for srt in files:
        head, cues = parse_srt(srt.read_text(encoding="utf-8-sig", errors="replace"))
        drop, notes = find_drops(cues)
        print(f"{srt}  原有 {len(cues)} 段 → 删 {len(drop)} 段")
        for n in notes:
            print(n)
        total += len(drop)
        if not (a.write and drop):
            continue
        keep = [c for k, c in enumerate(cues) if k not in drop]
        parts = [*head] + [f"{n}\n{_fmt(c['a'])} --> {_fmt(c['b'])}\n{c['text']}".rstrip()
                           for n, c in enumerate(keep, 1)]
        srt.write_text("\n\n".join(parts) + "\n", encoding="utf-8-sig")
        print(f"    → 已写回：{len(keep)} 段（头部 {len(head)} 块保留）")

    print(f"\n合计删除 {total} 段" + ("（已写回）" if a.write else "（预演，未写）"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
