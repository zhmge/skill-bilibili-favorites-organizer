# -*- coding: utf-8 -*-
"""执行收藏夹移动。幂等、可断点续跑、带限流熔断、带验收与回滚。

用法：
    <python> scripts/apply_moves.py --workdir DIR --create         # 建缺失的目标夹
    <python> scripts/apply_moves.py --workdir DIR --probe          # 试跑探批量上限
    <python> scripts/apply_moves.py --workdir DIR --run            # 正式执行（可反复跑）
    <python> scripts/apply_moves.py --workdir DIR --verify         # 验收断言
    <python> scripts/apply_moves.py --workdir DIR --rollback --yes # 反向移回
    <python> scripts/apply_moves.py --workdir DIR --status

幂等的依据有两层，缺一不可：
  1. apply_state.json  记录已成功移动的 `目标夹 → [key]`
  2. 每次 --run 开头重新拉一次源夹的**权威**内容，只移动「还在源夹里」的条目
第二层保证即使状态文件丢了或手工删了，重跑也不会重复移动。

状态文件（workdir 下）：
    apply_state.json   已完成清单
    apply_log.jsonl    每个批次的移动记录 —— 这是回滚的唯一依据
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


def _wd(raw) -> Path:
    return Path(raw or os.environ.get("BILI_ORG_WORKDIR")
                or "./bili_org_work").expanduser().resolve()


def load(p: Path, default):
    return json.loads(p.read_text(encoding="utf-8")) if p.is_file() else default


def save(p: Path, obj) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(p)


def logline(p: Path, rec: dict) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def cmd_create(wd: Path) -> int:
    doc = load(wd / "move_plan.json", None)
    if not doc:
        raise SystemExit("错误：move_plan.json 不存在。请先跑 plan_moves.py。")
    if not doc["create"]:
        print("没有需要新建的收藏夹。")
        return 0
    print(f"待新建 {len(doc['create'])} 个：{'、'.join(doc['create'])}")
    for d in list(doc["create"]):
        r = bili.add_folder(d)
        mid_new = (r or {}).get("id")
        if not mid_new:
            print(f"  ✗ {d}：返回里没有 id，跳过（{r}）")
            continue
        doc["targets"][d]["media_id"] = int(mid_new)
        doc["targets"][d]["exists"] = True
        doc["create"].remove(d)
        print(f"  ✓ {d} → media_id={mid_new}")
        time.sleep(1.0)
    save(wd / "move_plan.json", doc)
    print("注意：新建后建议重新跑一次 collect.py 让快照与线上一致。")
    return 0


def cmd_probe(wd: Path, batch: int) -> int:
    """用源夹里最靠后的几条试探单次批量上限。失败会把它们留在原地，不会丢。

    注意：probe 成功的那一批是**真移动**，所以同样写进 apply_state.json 与
    apply_log.jsonl —— 否则这批条目会落在回滚覆盖之外。
    """
    doc = load(wd / "move_plan.json", None)
    if not doc:
        raise SystemExit("错误：move_plan.json 不存在。")
    # 挑一个已存在的目标夹
    cand = [d for d, v in doc["targets"].items() if v["exists"]]
    if not cand:
        raise SystemExit("错误：还没有已存在的目标夹，先 --create。")
    dest = cand[0]
    keys = doc["targets"][dest]["keys"][:max(batch, 1)]
    if not keys:
        raise SystemExit(f"目标夹「{dest}」没有待移动条目，换个夹再试。")
    mid = doc.get("mid") or bili.my_mid()
    doc["mid"] = mid
    print(f"试跑：{len(keys)} 条 → 「{dest}」　（是真移动，会计入回滚日志）")
    for n in (len(keys), 10, 5, 1):
        if n <= 0:
            continue
        sub = keys[:n]
        try:
            bili.move_resources(doc["src_id"], doc["targets"][dest]["media_id"], mid, sub)
        except bili.CircuitBreak as e:
            print(f"  ✗ {n} 条触发风控：{e}　→ 等 30 分钟再试")
            return 2
        except bili.BiliError as e:
            print(f"  ✗ {n} 条被拒：{e}　→ 降档重试")
            continue
        print(f"  ✓ {n} 条成功 → 单批上限至少 {n}")
        st = load(wd / "apply_state.json", {"done": {}, "mid": mid})
        st["mid"] = mid
        st["done"].setdefault(dest, []).extend(sub)
        save(wd / "apply_state.json", st)
        logline(wd / "apply_log.jsonl",
                {"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "dest": dest,
                 "tar_id": doc["targets"][dest]["media_id"], "src_id": doc["src_id"],
                 "resources": sub, "ok": True, "note": "probe"})
        save(wd / "move_plan.json", doc)
        return 0
    print("  ✗ 连 1 条都失败，先查凭据与 csrf（BILI_JCT）")
    return 2


def cmd_run(wd: Path, batch: int, sleep_s: float) -> int:
    doc = load(wd / "move_plan.json", None)
    if not doc:
        raise SystemExit("错误：move_plan.json 不存在。请先跑 plan_moves.py。")
    if doc["create"]:
        raise SystemExit(f"错误：还有 {len(doc['create'])} 个目标夹没建："
                         f"{doc['create']}。先跑 --create。")
    mid = doc.get("mid") or bili.my_mid()
    doc["mid"] = mid

    st = load(wd / "apply_state.json", {"done": {}, "mid": mid})
    logf = wd / "apply_log.jsonl"
    bili.set_logfile(wd / "api_log.jsonl")

    print(f"重新拉取源夹「{doc['src']}」的权威内容…")
    still = {f"{x['aid']}:{x['type']}" for x in bili.folder_ids(doc["src_id"])}
    print(f"  源夹现有 {len(still)} 条")

    moved_batches = 0
    for dest, t in doc["targets"].items():
        todo = [k for k in t["keys"] if k in still and k not in st["done"].get(dest, [])]
        done_n = len(t["keys"]) - len([k for k in t["keys"] if k in still and
                                       k not in st["done"].get(dest, [])])
        print(f"\n→ 「{dest}」计划 {len(t['keys'])} 条，本次待移 {len(todo)} 条"
              f"（已处理 {done_n}）")
        for i in range(0, len(todo), batch):
            chunk = todo[i:i + batch]
            try:
                bili.move_resources(doc["src_id"], t["media_id"], mid, chunk)
            except bili.CircuitBreak as e:
                save(wd / "apply_state.json", st)
                print(f"\n✗ 触发风控熔断：{e}")
                print("  状态已保存。请等 30 分钟以上再跑同一条命令，会自动续跑。")
                return 2
            except bili.BiliError as e:
                logline(logf, {"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "dest": dest,
                               "resources": chunk, "ok": False, "note": str(e)[:200]})
                print(f"  ✗ 批次被拒：{e}　（已记日志，本条不再重试）")
                continue
            st["done"].setdefault(dest, []).extend(chunk)
            save(wd / "apply_state.json", st)
            logline(logf, {"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "dest": dest,
                           "tar_id": t["media_id"], "src_id": doc["src_id"],
                           "resources": chunk, "ok": True})
            moved_batches += 1
            print(f"  ✓ 批 {moved_batches}：{len(chunk)} 条 → {dest}")
            time.sleep(sleep_s)

    print(f"\n本次完成 {moved_batches} 个批次。跑 --verify 验收。")
    return 0


def cmd_verify(wd: Path) -> int:
    doc = load(wd / "move_plan.json", None)
    if not doc:
        raise SystemExit("错误：move_plan.json 不存在。")
    snap = load(wd / "snapshot.json", None)
    print("重新拉取源夹的权威内容…")
    left_ids = bili.folder_ids(doc["src_id"])
    left = [f"{x['aid']}:{x['type']}" for x in left_ids]
    planned = {k for t in doc["targets"].values() for k in t["keys"]}

    fails = []
    if left:
        fails.append(f"源夹「{doc['src']}」还剩 {len(left)} 条未移出")
    if snap:
        auth_before = snap.get("folder_items_authoritative", {}).get(doc["src"], [])
        if len(auth_before) != doc["total"]:
            fails.append(f"计划覆盖数 {doc['total']} != 采集时的源夹权威条数 "
                         f"{len(auth_before)}：说明有计划外的条目")

    # 逐目标夹核对：移过去的都在
    for dest, t in doc["targets"].items():
        now = {f"{x['aid']}:{x['type']}" for x in bili.folder_ids(t["media_id"])}
        missing = [k for k in t["keys"] if k not in now]
        if missing:
            fails.append(f"「{dest}」缺 {len(missing)} 条：{missing[:20]}")

    (wd / "verify_report.json").write_text(json.dumps(
        {"src": doc["src"], "src_left": left, "fails": fails,
         "checked_at": time.strftime("%Y-%m-%d %H:%M:%S")},
        ensure_ascii=False, indent=1), encoding="utf-8")

    if fails:
        print("\n验收未通过：")
        for x in fails:
            print("  ✗", x)
        return 2
    print(f"\n验收通过：源夹已空（{len(planned)} 条全部到位），各目标夹齐备。")
    return 0


def cmd_rollback(wd: Path, yes: bool) -> int:
    logf = wd / "apply_log.jsonl"
    if not logf.is_file():
        raise SystemExit("错误：没有 apply_log.jsonl，无法回滚。")
    recs = [json.loads(l) for l in logf.read_text(encoding="utf-8").splitlines() if l.strip()]
    oks = [r for r in recs if r.get("ok")]
    n = sum(len(r["resources"]) for r in oks)
    print(f"回滚依据：{len(oks)} 个成功批次，共 {n} 条")
    print("影响面：把这些条目从各自目标夹移回源夹。**反向执行会覆盖你在两次整理之间的手动改动。**")
    for r in oks[:20]:
        print(f"  {r['ts']}  { ', '.join(r['resources'][:4]) }… → 回到 {r.get('src_id')}")
    if len(oks) > 20:
        print(f"  …其余 {len(oks) - 20} 个批次略")
    if not yes:
        print("\n这是预览。确认无误后加 --yes 才会真的执行。")
        return 0
    for r in reversed(oks):
        try:
            bili.move_resources(r["tar_id"], r["src_id"],
                                (load(wd / "apply_state.json", {}) or {}).get("mid") or bili.my_mid(),
                                r["resources"])
            print(f"  ↺ 已回滚 {len(r['resources'])} 条")
        except bili.BiliError as e:
            print(f"  ✗ 回滚失败：{e}　（继续处理其余批次）")
        time.sleep(1.2)
    # 回滚完成后清掉状态，避免下次 --run 误判为已完成
    save(wd / "apply_state.json", {"done": {}, "mid": None})
    print("回滚完成；apply_state.json 已清空。")
    return 0


def cmd_status(wd: Path) -> int:
    doc = load(wd / "move_plan.json", {}) or {}
    st = load(wd / "apply_state.json", {"done": {}}) or {"done": {}}
    total = doc.get("total", 0)
    done = sum(len(v) for v in st.get("done", {}).values())
    print(f"源夹：{doc.get('src')}　计划 {total} 条　已完成 {done} 条")
    for d, t in (doc.get("targets") or {}).items():
        n = len(st.get("done", {}).get(d, []))
        print(f"  {d:<8} {n}/{len(t['keys'])}")
    if doc.get("create"):
        print("尚未新建:", doc["create"])
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--workdir", default=None)
    ap.add_argument("--create", action="store_true")
    ap.add_argument("--probe", action="store_true")
    ap.add_argument("--run", action="store_true")
    ap.add_argument("--verify", action="store_true")
    ap.add_argument("--rollback", action="store_true")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--batch", type=int, default=20)
    ap.add_argument("--sleep", type=float, default=2.0, help="批次之间的间隔秒数")
    ap.add_argument("--yes", action="store_true", help="回滚时确认真执行")
    a = ap.parse_args()

    wd = _wd(a.workdir)
    print(f"工作目录 {wd}")
    bili.MIN_INTERVAL = 1.1
    bili.set_logfile(wd / "api_log.jsonl")

    if a.create:
        return cmd_create(wd)
    if a.probe:
        return cmd_probe(wd, a.batch)
    if a.run:
        return cmd_run(wd, a.batch, a.sleep)
    if a.verify:
        return cmd_verify(wd)
    if a.rollback:
        return cmd_rollback(wd, a.yes)
    if a.status:
        return cmd_status(wd)
    ap.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
