#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""封面缩略图流水线：B站缩略图 → 本地 → 资料库图床托管直链。

为什么必须「先落本地再上传」，而不是让后端直接抓 B站外链：
  B站图片 CDN 有 Referer 防盗链。实测
    - 不带 Referer          → 200
    - Referer: bilibili.com → 200
    - Referer: workbuddy.cn → 403
  资料库页面发出的请求带的是 workbuddy 域 Referer，所以 B站直链写进页面必然 403。
  必须以字节形式上传到资料库自己的图床，拿到 codebuddy 域的托管直链。

为什么用 @320w_200h_1c.webp 后缀：
  同一张图，原图 551,699 B → 缩略图 8,804 B（约 1/63）。

用法：
  <python> scripts/covers.py fetch  --workdir DIR [--limit N]
  <python> scripts/covers.py upload --workdir DIR [--workers 4]
  <python> scripts/covers.py run    --workdir DIR [--workers 4]
  <python> scripts/covers.py stats  --workdir DIR

需要环境变量：
  WORKBUDDY_LIBRARY_SKILL_DIR   资料库 skill 目录（含 _common.py）。
                                本机在 <WorkBuddy 安装目录>/resources/app.asar.unpacked/
                                resources/plugins/workbuddy-builtin/skills/library

产物（workdir 下）：
  covers/<bvid>.webp    本地缩略图
  covers.json           账本：{bvid: {file, bytes, url, w, h}}，同时是**断点续跑依据**
  covers_failed.json    失败明细
"""
from __future__ import annotations

import argparse
import base64
import concurrent.futures as cf
import json
import os
import sys
import time
import urllib.request
from pathlib import Path

SUFFIX = "@320w_200h_1c.webp"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
UPLOAD_PATH = "/space/api/agent/v1/upload-image"

_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _lib_dir() -> Path:
    raw = os.environ.get("WORKBUDDY_LIBRARY_SKILL_DIR", "").strip()
    if not raw:
        raise SystemExit(
            "错误：环境变量 WORKBUDDY_LIBRARY_SKILL_DIR 未设置。\n"
            "请指向资料库 skill 目录（其中含 _common.py），例如：\n"
            "  Windows: <WorkBuddy 安装目录>/resources/app.asar.unpacked/resources/"
            "plugins/workbuddy-builtin/skills/library\n"
            "  macOS:   <WorkBuddy 安装目录>/Contents/Resources/app.asar.unpacked/resources/"
            "plugins/workbuddy-builtin/skills/library")
    p = Path(raw).expanduser()
    if not (p / "_common.py").is_file():
        raise SystemExit(f"错误：{p} 下找不到 _common.py，WORKBUDDY_LIBRARY_SKILL_DIR 指向有误。")
    return p


def _workdir(raw: str | None) -> Path:
    p = Path(raw or os.environ.get("BILI_ORG_WORKDIR") or "./bili_org_work").expanduser().resolve()
    p.mkdir(parents=True, exist_ok=True)
    return p


LIB = _lib_dir()
sys.path.insert(0, str(LIB))
from _common import HttpError, http_request, unwrap_data  # noqa: E402

WD = Path(".")
COVERS = Path("covers")
LEDGER = Path("covers.json")
FAILED = Path("covers_failed.json")


def load(path: Path, default):
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return default


def save(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)


def thumb_url(pic: str) -> str:
    """把 B站封面地址换成小尺寸变体。"""
    base = pic.split("@", 1)[0]
    if base.startswith("http://"):
        base = "https://" + base[len("http://"):]
    return base + SUFFIX


def pics_from_snapshot() -> dict[str, str]:
    """从 snapshot.json 取 bvid → cover，作为下载清单（唯一数据源，不另存一份）。"""
    snap = WD / "snapshot.json"
    if not snap.is_file():
        raise SystemExit(f"错误：{snap} 不存在。请先跑 collect.py。")
    data = json.loads(snap.read_text(encoding="utf-8"))
    out = {}
    for v in data["videos"].values():
        bv, pic = (v.get("bvid") or "").strip(), (v.get("cover") or "").strip()
        if bv and pic:
            out[bv] = pic
    return out


def download(bv: str, pic: str):
    dest = COVERS / f"{bv}.webp"
    if dest.exists() and dest.stat().st_size > 0:
        return bv, {"file": str(dest.relative_to(WD)), "bytes": dest.stat().st_size}, ""
    last = ""
    for attempt in range(3):
        try:
            req = urllib.request.Request(thumb_url(pic), headers={"User-Agent": UA})
            with _opener.open(req, timeout=30) as r:
                data = r.read()
            if not data:
                last = "empty body"
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)
            return bv, {"file": str(dest.relative_to(WD)), "bytes": len(data)}, ""
        except Exception as e:  # noqa: BLE001
            last = f"{type(e).__name__}: {e}"
            time.sleep(0.8 * (attempt + 1))
    return bv, None, last


def upload_one(bv: str):
    path = COVERS / f"{bv}.webp"
    if not path.exists():
        return bv, None, "local file missing"
    body = {"data": base64.b64encode(path.read_bytes()).decode("ascii"),
            "fileName": f"{bv}.webp", "contentType": "image/webp"}
    last = ""
    for attempt in range(3):
        try:
            data = unwrap_data(http_request("POST", UPLOAD_PATH, body=body, timeout=60.0))
            url = (data.get("url") or "").strip()
            if not url:
                last = "empty url from server"
                continue
            return bv, {"url": url, "w": int(data.get("width") or 0),
                        "h": int(data.get("height") or 0)}, ""
        except HttpError as e:
            last = f"HttpError: {e}"
            time.sleep(1.0 * (attempt + 1))
        except Exception as e:  # noqa: BLE001
            last = f"{type(e).__name__}: {e}"
            time.sleep(1.0 * (attempt + 1))
    return bv, None, last


def cmd_fetch(limit: int) -> None:
    pics = pics_from_snapshot()
    ledger = load(LEDGER, {})
    todo = [bv for bv in pics if not ledger.get(bv, {}).get("file")]
    if limit:
        todo = todo[:limit]
    print(f"[fetch] 待下载 {len(todo)} / 快照有封面 {len(pics)}")
    ok = 0
    for i, bv in enumerate(todo, 1):
        bv2, info, err = download(bv, pics[bv])
        if info:
            ledger.setdefault(bv2, {}).update(info)
            ok += 1
        else:
            print(f"  FAIL {bv}: {err}")
        if i % 50 == 0:
            save(LEDGER, ledger)
            print(f"  ... {i}/{len(todo)}（成功 {ok}）")
    save(LEDGER, ledger)
    print(f"[fetch] 完成：成功 {ok}，失败 {len(todo) - ok}；账本 {len(ledger)} 条")


def cmd_upload(limit: int, workers: int) -> None:
    ledger = load(LEDGER, {})
    todo = [bv for bv, v in ledger.items() if v.get("file") and not v.get("url")]
    if limit:
        todo = todo[:limit]
    print(f"[upload] 待上传 {len(todo)}；并发 {workers}")
    ok, fail, t0 = 0, {}, time.time()
    with cf.ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(upload_one, bv): bv for bv in todo}
        for done, fut in enumerate(cf.as_completed(futs), 1):
            bv, info, err = fut.result()
            if info:
                ledger.setdefault(bv, {}).update(info)
                ok += 1
            else:
                fail[bv] = err
                print(f"  FAIL {bv}: {err}")
            if done % 50 == 0:
                save(LEDGER, ledger)
                print(f"  ... {done}/{len(todo)}（成功 {ok}）")
    save(LEDGER, ledger)
    if fail:
        save(FAILED, fail)
    dt = time.time() - t0
    rate = (ok + len(fail)) / dt if dt else 0
    print(f"[upload] 完成：成功 {ok}，失败 {len(fail)}，耗时 {dt:.1f}s（{rate:.1f}/s）")


def cmd_stats() -> None:
    ledger, pics = load(LEDGER, {}), pics_from_snapshot()
    print(json.dumps({
        "snapshot_coverable": len(pics),
        "ledger_total": len(ledger),
        "downloaded": sum(1 for v in ledger.values() if v.get("file")),
        "uploaded": sum(1 for v in ledger.values() if v.get("url")),
        "pending_download": len([b for b in pics if not ledger.get(b, {}).get("file")]),
        "pending_upload": len([b for b, v in ledger.items()
                               if v.get("file") and not v.get("url")]),
    }, ensure_ascii=False))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["fetch", "upload", "run", "stats"])
    ap.add_argument("--workdir", default=None)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--workers", type=int, default=4)
    a = ap.parse_args()

    global WD, COVERS, LEDGER, FAILED
    WD = _workdir(a.workdir)
    COVERS, LEDGER, FAILED = WD / "covers", WD / "covers.json", WD / "covers_failed.json"
    print(f"工作目录 {WD}")

    if a.cmd == "fetch":
        cmd_fetch(a.limit)
    elif a.cmd == "upload":
        cmd_upload(a.limit, a.workers)
    elif a.cmd == "run":
        cmd_fetch(a.limit)
        cmd_upload(a.limit, a.workers)
    else:
        cmd_stats()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
