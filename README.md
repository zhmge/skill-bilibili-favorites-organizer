# Bilibili Favorites Organizer

把 B 站「默认收藏夹」里积压的视频按主题分到各分类收藏夹，为新增的技术类视频补字幕，并让数据驱动的视频清单页与字幕报告页随之扩充。分类结果先落成可审阅的去向表，确认后才执行移动。

> An agent skill that classifies newly saved Bilibili videos into category folders, backfills subtitles for technical videos, and extends a data-driven index page.

## 功能

- 采集默认收藏夹全集，并用 `/resource/ids` 的权威清单补齐列表接口看不见的条目
- 判定分两遍：先只读文本（标题、简介、UP 主、历史结论），文本判不动才升级看封面
- 合并判定结果时做三重断言：漏判、多判、重复、非法去向、缺理由，任一非零即中止
- 移动前生成预演单，移动后按 `ids` 接口回读验收，账本支持断点续跑与回滚
- 字幕阶段先取原生字幕，确实没有时按视频体量决定是否本地 ASR 兜底
- 清单页与报告页不存内容，只存「怎么读表」，新增视频进表后页面自动多出条目

## 依赖

| 类别 | 要求 |
|---|---|
| 运行时 | Python 3；`judge_material.py` 出接触表需要 Pillow，其余脚本仅用标准库 |
| 凭据 | `BILI_SESSDATA`（读接口必需）、`BILI_JCT`（移动与建夹必需），只从环境变量读取 |
| 伴生 skill | `bilibili-subtitle-fetch`（原生字幕）、`video-audio-to-srt`（无字幕时本地转写） |
| 资料库 | 阶段 3、4、7 通过 `WORKBUDDY_LIBRARY_SKILL_DIR` 指向的资料库 skill 读写数据表与页面 |

本 skill 不实现字幕能力，只调用上面两个伴生 skill。

`SESSDATA` 是 HttpOnly cookie，页面脚本读不到，只能在浏览器 `F12` → Application → Cookies 里手工复制。它等同于账号凭据，不应写入文件或提交进版本控制。

## 快速开始

```bash
export BILI_SESSDATA='<值>'
export BILI_JCT='<值>'

<python> scripts/collect.py --workdir <wd>
<python> scripts/judge_material.py --workdir <wd> --no-sheet
<python> scripts/merge_verdicts.py --workdir <wd> --destinations "计算机,游戏开发,游戏相关,AI,杂项"
```

判定结果落在 `<wd>/judge/verdict_*.json`，需要 agent 逐条填写。之后的建库、审阅页、移动、字幕、清单页各阶段见 `SKILL.md`。

## 产物

| 路径 | 内容 |
|---|---|
| `<wd>/snapshot.json` | 默认收藏夹快照，含两个接口的并集与历史结论 |
| `<wd>/judge/batch_NN.txt` | 判定素材，恒含全部条目 |
| `<wd>/judge/verdict_*.json` | 逐条去向、理由、置信度 |
| `<wd>/records/` | 数据表记录分片 |
| `<wd>/move_plan.md` | 给人看的移动预演单 |
| `<wd>/apply_state.json`、`apply_log.jsonl` | 移动账本，支持幂等续跑与回滚 |
| `<wd>/subs_index.json` | 字幕结果索引，供下一轮建库读取 |

`<wd>` 默认 `./bili_org_work`，可用 `--workdir` 指定。

## 三个反直觉之处

**列表接口会静默少给数据。** 不报错，只是安静地少给。因此采集必须同时跑 `/resource/list` 与 `/resource/ids`，以 `ids` 为全集；只信列表接口会留下一批永远归不了类、验收时又看不见的条目。

**写库成功不等于已落库。** `batch_add` 返回 `success=true` 但记录未写入是发生过的。写后必须用 `--expect-seq-from-file` 回读核验。

**限流不能硬碰。** 请求最小间隔 1.1 秒，调到 0.5 秒必然触发 HTTP 412；命中风控后继续重试只会延长封禁。

细节分别见 `references/api-behavior.md`、`references/data-model.md`、`references/state-and-ledger.md`。

## 已知限制

- 阶段 3 起依赖宿主平台的资料库 skill。缺少该环境时只能跑到阶段 2（判定）为止，无法建库、出审阅页与清单页。
- 分类体系是个人口径（计算机、游戏开发、游戏相关、AI、数字美术等），换领域需改 `references/classification-rules.md`。
- 只处理 B 站收藏夹；不处理关注列表，也不做单支视频的去向判断。
- 移动是「加入目标夹 + 从源夹移除」，不做去重；同一视频已在别的夹且去向不同时会列出来交给人决定。
- 阅读门槛偏高：七个阶段里有两个人机停机点，不适合无人值守全自动运行。

## 项目结构

```
.
├── SKILL.md                      agent 入口：七阶段流程与硬约束
├── README.md                     本文件
├── LICENSE                       MIT
├── references/
│   ├── api-behavior.md           接口行为、静默少给、防盗链、风控
│   ├── classification-rules.md   分类体系与校准经验
│   ├── data-model.md             表结构、字段类型、写入与回读
│   ├── page-build.md             审阅页与清单页的构建门禁
│   ├── state-and-ledger.md       幂等、续跑、回滚、验收
│   └── subtitle-stage.md         字幕口径与兜底判定
└── scripts/                      采集、判定、建库、移动、字幕的十支脚本
```

整个目录放入 skills 目录即可，`SKILL.md` 是入口。

## License

[MIT](LICENSE) © 2026 zhmge
