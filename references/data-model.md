# 数据模型与资料库读写

## 为什么用数据表而不是 JSON 文件

JSON 快照是**过程产物**（采一次用一次），数据表是**长期账本**：

- 页面上改一下去向就直接落库，不需要重新跑一遍脚本；
- 下一轮整理时能读到上一轮的结论，不用从头判；
- 可以筛、可以统计、可以给别的页复用。

所以 `snapshot.json` 可以随时重采，数据表不要重建。

## 资料库数据表的能力边界

| 项 | 值 |
|---|---|
| 单表行上限 | 10,000 |
| 单表列上限 | 50 |
| 单行上限 | 1 MB |
| 单表写入 | 30 QPS |
| 页面 SDK | 20 QPS |
| `db.query` 单次上限 | **200 条**，必须自行分页 |
| 统计 | 用 `db.aggregate`，不要全量拉下来在算 |

按每轮新增几十到几百条的节奏，1 万行的余量足够用很多年。

## 字段

按顺序（27 列）：

| 字段 | 类型 | 说明 |
|---|---|---|
| 序号 | number | 全表唯一，用于回读核验缺失 |
| BV号 | text | 接口不可见的条目为空 |
| 视频标题 | text | 接口不可见的统一写「【已失效/接口不可见】」 |
| UP主 | text | 接口不可见的写「—」 |
| 视频链接 | url | |
| 封面 | image | 托管直链，**不是** B站直链 |
| 时长 | text | `H:MM:SS` / `M:SS` |
| 分P数 | number | |
| 播放量 | number | |
| 收藏时间 | date | `YYYY-MM-DD` |
| 所属收藏夹 | multi_select | 当前实际所在的所有分类夹 |
| 二级子类 | select | 21 个选项，仅技术类视频有 |
| 建议去向 | select | AI 判定的结果 |
| 最终去向 | select | **用户可改**，页面上的下拉框；执行移动读它 |
| 判定来源 | select | 沿用历史稿 / 规则 / AI / 你 |
| 拿不准 | checkbox | 判定置信度低时为 true，页面上重点标出 |
| 判定理由 | text | 每条都有，不许留空 |
| 内容描述 | text | 100-160 字，仅技术类视频 |
| 字幕状态 | select | 有字幕 / ASR生成 / 无字幕 / 视频失效 / 待定 —— **语义是「有没有字幕」，不是覆盖率**，见下 |
| 字幕来源 | select | B站原生 / 本地ASR / 无 |
| 收录分P | text | `已解决/该视频分P总数` —— **分母是总数，不是应取数** |
| 整理状态 | select | 待整理 / 已归档 / 已提取字幕 / 已入清单页 |
| 审阅批注 | text | **用户写下的理由**，用来沉淀判定经验，见 classification-rules.md |
| B站字幕数 | number | 范围内的原生字幕分P 数 |
| ASR字幕数 | number | 范围内的 ASR 分P 数 |
| 已解决分P | number | = B站字幕数 + ASR字幕数（仅计「应取范围」内） |
| 应取分P | number | 本轮约定范围：多分P = `min(n, 5)`；单P / 合集单集 = 1 |

后 4 列是阶段 6 的工程指标，不在原始采集里、由流水线自己算出来，但要作为**正式列**写回表
（"数据表是唯一事实源"）。口径细节与「为什么覆盖率不能塞进 `字幕状态`」见 `subtitle-stage.md`。

`整理状态` 的 `待整理` 就等于「还在默认收藏夹里」。审阅页只筛这一档。

## 写入侧：PropertyValue

每个字段值都是一个 oneof，写法不能混：

```json
{"text": "字符串"}                                  // text / email / phone_number
{"number": 123}                                     // number
{"select": "选项文本或选项 id"}                      // select
{"multi_select": ["A", "B"]}                        // multi_select
{"date": "2026-10-07"}                              // date
{"checkbox": true}                                  // checkbox
{"url": {"text": "显示文字", "link": "https://…"}}   // url
{"image": {"images": [{"title": "封面",
                       "imageUrl": "https://…",
                       "width": 320, "height": 200}]}}  // image
```

**空值不要写。** 缺字段和显式写 null 不是一回事，前者会被当成「没设置」。

## 读取侧：返回结构按类型不同

`db.query` 与 `query-database` 返回的是**扁平值**，与写入侧的形状不一样：

| 类型 | 读回来是什么 |
|---|---|
| text / select / email / phone_number | string |
| number | number |
| date | string，ISO 8601 |
| checkbox | boolean |
| multi_select | string[] |
| url | `{ text, link }` |
| image | `[{ imageUrl, title, width, height }]` ← **扁平数组**，不是 `{image:{images:[…]}}` |
| 空 | null |

写 `{"image": {"images": [...]}}`，读回来是 `[{…}]`。第一次写图片字段的代码十有八九会在这里翻车。

渲染前判空：

```javascript
var imgs = row["封面"];
var src = imgs && imgs[0] ? imgs[0].imageUrl : "";
```

## 假成功：写完必须回读

**实测**：606 条记录写入，接口对每一批都返回成功，回读只有 602 条。

原因：其中 4 条的 `二级子类` 值写成了 `"AI 辅助开发与工具"`（**带空格**），而表里定义的选项是 `"AI辅助开发与工具"`（无空格）。服务端**静默拒收**那一条，整批仍返回 success。

所以：

```bash
<python> scripts/db_read.py --database-id <id> --workdir <wd> \
  --expect-seq-from-file "<wd>/records/records_batch_*.json"
```

用 `序号` 拿全集做差集，缺谁一目了然。**不回读就等于没写。**

将来再遇到「少了若干条」，第一件事是核对该记录里每个 select 的选项文本与 schema 里的定义是否**逐字符**相同 —— 多余的空格、全角半角、异体字都会触发。

## 回读接口的坑

- `query-database` 的响应字段是**驼峰**：`results` / `nextCursor` / `hasMore`。写成 `next_cursor` 会拿到 `None` 然后原地退出循环，只拉到 200 条却毫不报错。
- 分页游标参数名是 `startCursor`，不是 `cursor` / `offset`。
- 稳妥写法是两种命名都读一次：`d.get("hasMore", d.get("has_more"))`。

## 图片托管门禁

页面里**不得残留第三方域的 `<img src>`**；只允许 `codebuddy` / `workbuddy` 域。本地路径和第三方链接都必须先转成托管直链。

B站封面直链属于第三方域 —— 既踩门禁，又必然 403（见 api-behavior.md）。必须走 `covers.py` 那条流水线。

## 写库的调用姿势

`batch_add_database_records.py` 的 stdin 模式要**完整包装对象**，不是裸数组：

```json
{"database_id": "…", "records": [ {…}, {…} ]}
```

传裸数组会得到 `{"error": "输入 JSON 为空或格式非法"}`。

**但 `records` 数组里每一项的形态，两个接口不一样 —— 这是最容易踩的一处：**

| 接口 | 每个元素的形态 |
|---|---|
| `batch_add_database_records.py` | **属性映射本身**：`{"序号": {"number": 1}, "BV号": {"text": "BV…"}}` |
| `batch_update_database_records.py` | `{"record_id": "…", "properties": {…}}` |

新增时若照抄更新的形状（多套一层 `{"properties": …}`），校验会**逐条拒绝**并报
`records[0] 校验后为空，无有效字段` —— 报错位置指向"校验"，很容易误以为是类型写错，
实际是结构套错了一层。2026-10-07 实采。

`get_database_record.py` 必须同时传 `--database-id` 与 `--record-id`，少一个就报缺失。

## 加列 / 建表 / 回填

字幕工程指标（`.srt` 数、分P 已解决/应取）不在原始采集里，是流水线自己算出来的；
它们同样属于"数据表是唯一事实源"的范畴，**要作为正式列写回表**，页面才能实时聚合。

```bash
# 加一列（number）
echo '{"database_id":"<id>","property":{"name":"B站字幕数","config":{"number":{"decimalPlaces":0}}}}' \
  | <python> "$LIB/database/add_database_field.py" --stdin

# 建一张新表（parent_id 指向目标目录；字段用 {name, config} 数组）
echo '{"title":"字幕待定分P","parent_id":"<folderId>","properties":[
        {"name":"序号","config":{"number":{"decimalPlaces":0}}},
        {"name":"分P链接","config":{"url":{}}}]}' \
  | <python> "$LIB/database/create_database.py" --stdin
```

两个坑：

- `batch_add` 一次最多 **100 条**（`records` 长度校验 1–100），超过要自己切片。
- `create_database` 的 stdout **不止一行**（后面还跟一行 `KS_USER_REPLY`）。
  用 `json.loads(整个 stdout)` 会抛异常、被自己的兜底吞掉，表现成"建表失败"，
  而**表其实已经建出来了** —— 于是重试一次就多一张空表。解析时取第一行 JSON，
  或先按行找第一个以 `{` 开头的行。

字段粒度要对齐：**视频粒度**（每支视频一行）和**分P粒度**（每个没拿到字幕的分P一行）
混进同一张表会毁掉聚合口径。分P 级的待定清单单独建表，用 `BV号` 与主表关联。

