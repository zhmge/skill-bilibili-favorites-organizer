# -*- coding: utf-8 -*-
"""B站 API 公共封装（读/写均为同一套）。

凭据只从环境变量读取，绝不落盘：
    BILI_SESSDATA   登录态
    BILI_JCT        csrf token（写操作必填）

设计要点：
- 强制直连（ProxyHandler({})），不走本机代理
- 统一 UA / Referer / Origin 头
- POST 自动注入 csrf
- 限流：请求间最小间隔
- 熔断：-352 / -412 / -799 视为风控，抛 CircuitBreak（调用方应停止 30 分钟）
- 鉴权失效：-101 / -111 抛 AuthError
- 重试：网络异常重试 3 次（指数退避）
"""
from __future__ import annotations

import json
import os
import random
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

API = "https://api.bilibili.com"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

# 熔断码（触发即停止，等 30 分钟）
BREAK_CODES = {-352, -412, -799}
# 鉴权失效码
AUTH_CODES = {-101, -111}

_req_count = 0
_last_ts = 0.0
MIN_INTERVAL = 1.1           # 全局最小请求间隔（秒）—— 0.5 实测会触发 HTTP 412
HTTP_THROTTLE = {412, 403, 429}   # HTTP 层风控码
LOGFILE: Path | None = None  # 由 set_logfile 指定


class BiliError(Exception):
    def __init__(self, code, message, path=""):
        super().__init__(f"[{code}] {message} ({path})")
        self.code = code
        self.message = message
        self.path = path


class AuthError(BiliError):
    """-101 / -111 凭据失效"""


class CircuitBreak(BiliError):
    """-352 / -412 / -799 风控熔断"""


def cred():
    """读取凭据，缺失即报错并给指引。"""
    sd = os.environ.get("BILI_SESSDATA", "").strip()
    jct = os.environ.get("BILI_JCT", "").strip()
    if not sd:
        raise SystemExit("错误：环境变量 BILI_SESSDATA 未设置。\n"
                         "请在浏览器 F12 → Application → Cookies → https://www.bilibili.com 复制 SESSDATA，\n"
                         "然后以环境变量方式注入本次命令（不要写入任何文件）。")
    return sd, jct


def set_logfile(p: str | Path | None):
    global LOGFILE
    LOGFILE = Path(p) if p else None


def _headers(with_cookie=True):
    h = {
        "User-Agent": UA,
        "Referer": "https://www.bilibili.com",
        "Origin": "https://space.bilibili.com",
        "Accept": "application/json, text/plain, */*",
    }
    if with_cookie:
        sd, _ = cred()
        h["Cookie"] = f"SESSDATA={sd}"
    return h


def _throttle():
    global _last_ts
    now = time.time()
    wait = MIN_INTERVAL - (now - _last_ts)
    if wait > 0:
        time.sleep(wait + random.uniform(0, 0.15))
    _last_ts = time.time()


def _log(path, code, ms, note=""):
    if not LOGFILE:
        return
    LOGFILE.parent.mkdir(parents=True, exist_ok=True)
    rec = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "path": path, "code": code,
           "ms": int(ms), "note": note}
    with open(LOGFILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def call(path: str, params: dict | None = None, post: dict | None = None,
         with_cookie=True, retries=3, timeout=25) -> dict:
    """调用 B站接口。返回 data 字段（code==0 时）。失败抛异常。"""
    global _req_count
    params = dict(params or {})
    url = API + path
    body = None

    if post is not None:
        sd, jct = cred()
        if not jct:
            raise SystemExit("错误：环境变量 BILI_JCT 未设置，无法执行写操作。")
        body = dict(post)
        body["csrf"] = jct
        data = urllib.parse.urlencode(body).encode("utf-8")
    else:
        data = None
        if params:
            url += "?" + urllib.parse.urlencode(params)

    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    last_err = None
    for attempt in range(retries):
        _throttle()
        t0 = time.time()
        try:
            req = urllib.request.Request(url, data=data, headers=_headers(with_cookie))
            with opener.open(req, timeout=timeout) as r:
                raw = r.read().decode("utf-8", "replace")
            ms = (time.time() - t0) * 1000
            _req_count += 1
            d = json.loads(raw)
            code = d.get("code", -999)
            _log(path, code, ms)
            if code == 0:
                return d.get("data")
            if code in AUTH_CODES:
                raise AuthError(code, d.get("message", ""), path)
            if code in BREAK_CODES:
                raise CircuitBreak(code, d.get("message", ""), path)
            raise BiliError(code, d.get("message", ""), path)
        except (AuthError, CircuitBreak):
            raise
        except BiliError:
            raise
        except urllib.error.HTTPError as e:
            last_err = e
            _log(path, f"HTTP{e.code}", (time.time() - t0) * 1000)
            if e.code in HTTP_THROTTLE:
                # 风控：长时间退避后再试，累计仍失败则熔断
                if attempt < retries - 1:
                    wait = 20 * (attempt + 1)
                    print(f"  [风控] HTTP {e.code}，退避 {wait}s 后重试（{attempt+1}/{retries-1}）",
                          file=sys.stderr)
                    time.sleep(wait)
                    continue
                raise CircuitBreak(e.code, f"HTTP {e.code} 风控，退避后仍失败", path)
        except Exception as e:  # noqa: BLE001
            last_err = e
            _log(path, "ERR", (time.time() - t0) * 1000, str(e)[:120])
        if attempt < retries - 1:
            time.sleep(1.2 * (attempt + 1))
    raise BiliError(-999, f"请求失败（重试 {retries} 次）：{last_err}", path)


# ---------- 业务封装 ----------

def my_mid() -> int:
    d = call("/x/web-interface/nav")
    return int(d["mid"])


def list_folders(mid: int) -> list[dict]:
    """全部收藏夹。默认收藏夹靠 attr 第 0 位识别，见 is_default_folder()。"""
    d = call("/x/v3/fav/folder/created/list-all", {"up_mid": mid, "platform": "web"})
    return (d or {}).get("list") or []


def is_default_folder(f: dict) -> bool:
    """默认收藏夹判定：**attr 恰好等于 1**。

    ★ 不要写成 `attr & 1`。实测 attr=3（摄影 / 篮球）与 attr=23（数学 / 计算机）
      的第 0 位同样是 1，位与会把四个普通夹误判成默认夹。
      2026-10-07 实采：默认收藏夹 attr=1，健身康复/AI/游戏开发/音乐 attr=22，
      数学/计算机 attr=23，摄影/篮球 attr=3。
    """
    return f.get("attr") == 1


def folder_items(media_id: int, ps: int = 20, max_pages: int = 200) -> list[dict]:
    """拉一个收藏夹的内容（分页）。**会静默漏条目，不要当全集用** —— 见 folder_ids()。"""
    out, pn = [], 1
    while pn <= max_pages:
        d = call("/x/v3/fav/resource/list", {
            "media_id": media_id, "pn": pn, "ps": ps, "platform": "web",
            "order": "mtime", "type": 0, "tid": 0,
        })
        medias = (d or {}).get("medias") or []
        out.extend(medias)
        if not (d or {}).get("has_more"):
            break
        pn += 1
    return out


def folder_ids(media_id: int) -> list[dict]:
    """收藏夹的**权威全集**（aid / type / bvid）。

    /resource/list 会静默漏掉「接口不可见」条目（bvid 存在但 view 接口 state≠0）：
    实测默认收藏夹声明 456 条，list 用 mtime 和 view 两种排序都只回 446 条，
    本接口回 456 条。因此**终态断言一律以本接口为准**，list 只用来取元数据。
    """
    d = call("/x/v3/fav/resource/ids", {"media_id": media_id, "platform": "web"})
    rows = d if isinstance(d, list) else (d or {}).get("list") or []
    out = []
    for x in rows:
        if isinstance(x, dict):
            out.append({"aid": x.get("id"), "type": x.get("type", 2),
                        "bvid": x.get("bvid") or ""})
        else:
            out.append({"aid": x, "type": 2, "bvid": ""})
    return out


def move_resources(src_media_id: int, tar_media_id: int, mid: int, resources: list[str]) -> dict:
    """把一个收藏夹里的条目移到另一个收藏夹。resources: ['aid:type', ...]

    语义是「加入目标夹 + 从源夹移除」。**单次能带几条没有官方说明**，
    首次使用必须先小批试跑确认（plan_moves.py 的 --probe 会做这件事）。
    """
    return call("/x/v3/fav/resource/move", post={
        "src_media_id": src_media_id,
        "tar_media_id": tar_media_id,
        "mid": mid,
        "resources": ",".join(resources),
    })


def add_folder(title: str, intro: str = "", privacy: int = 0) -> dict:
    """新建收藏夹。返回体里 data.id 即新的 media_id。"""
    return call("/x/v3/fav/folder/add", post={"title": title, "intro": intro,
                                              "privacy": privacy})


def del_folders(media_ids: list[int]) -> dict:
    return call("/x/v3/fav/folder/del", post={
        "media_ids": ",".join(str(i) for i in media_ids)})


def view(bvid: str) -> dict:
    return call("/x/web-interface/view", {"bvid": bvid}, with_cookie=True)


def stats() -> int:
    return _req_count
