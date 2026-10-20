# 数据页的构建与门禁

数据页是「数据表的一个视图」：表是唯一数据源，页面只负责展示和就地编辑。**不要把数据再写进 HTML**。

## 两道必须过的门禁

上传前跑，两个都过才算完成：

```bash
<python> <page-skill>/lint_database_sdk_usage.py --html <page.html> --schema-file <schema.json>
<python> <page-skill>/lint_page_quality.py     --html <page.html>
```

（`<page-skill>` 指宿主的资料库 skill 目录。另有 `lint_schema.py` 校验 schema 描述。）

**注意参数形态**：`lint_database_sdk_usage.py` 的 schema 要用 `--schema-file <路径>`；`lint_schema.py` 的 `--schema` 收的是 **JSON 字符串本身**，不是路径 —— 传路径会报 `Expecting value: line 1 column 1`。要用 `--schema "$(cat schema.json)"` 或 `--stdin`。

### lint_schema 对数据驱动页只能跑 R1–R9（不要带 `--html`）

`lint_schema.py` 的 **R10** 会在 HTML 原文里找 `display_selector` 对应的 `class` 属性。
数据驱动页的 DOM 是**运行时**用 `createElement` 建出来的，class 只存在于 JS 字符串里 ——
静态 HTML 里一个 `.t` / `.cover` / `.meta` 都没有，于是 R10 会把每个非 null 的
`display_selector` 全判成「找不到匹配元素」。这不是页面错，是规则前提不成立。

```bash
# ✅ 只读数据页：R1–R9（顶层契约 / field_mapping 与 properties 一致 / options_map 完整）
<python> <page-skill>/lint_schema.py --schema "$(cat schema.json)"
# ❌ 带 --html 会误报一片 R10
```

R10 只对「DOM 写死在 HTML 里」的静态页有意义。

### `canonical_schema.json` 必须跟着表结构走

判定过一轮、加过列之后，旧的 `canonical_schema.json` 会与线上表脱节 ——
典型症状：`lint_database_sdk_usage.py` 报 **DSDK009**（`query` 的 `sorts` 引用了 schema 里不存在的字段，
例如排序用的 `序号` 因为旧 schema 只有 12 列而「不存在」）。
**以线上表为准重建**，别手改：`get-database` 拉列定义 → 生成 `properties` + `field_mapping` + `options_map`
（select 字段的 `options_map` 必须与线上 option id 逐一对齐，否则 R7 报 id 不一致）。
参考实现：`tools/build_gate_schema.py`。

## 五个会白改一遍的坑

### 1 门禁的正则剥离不认字符串上下文（PQ001 误报）

`lint_page_quality.py` 在查括号配平时，先用一个近似的正则字面量剥离器把 `/.../` 换成占位符。这个剥离器**不跟踪字符串上下文** —— 于是 JS 字符串里的 HTML 闭合标签会被当成正则字面量整段吞掉：

```javascript
'<span class="tag">拿不准</span>' + '</div>'   // 里面的 </span>' + '</ 会被当作 /.../ 吞掉
```

后果是括号配平被误判，报 `PQ001 内联脚本括号未配平`。

**判据**：把 `//` 注释剥掉后再数一次括号，本来就是配平的 → 属于误报。

**正确的修法不是 `--ack`，而是别在 JS 里拼 HTML 字符串**（见下一节）。这条误报其实是「你的写法本身不够稳」的一个信号。

### 2 innerHTML 拼字符串直接判失败（PQ005）

`el.innerHTML = 变量` 一律 FAIL —— 转义函数白名单只认 `escapeHtml` / `DOMPurify.sanitize` / `encodeURI` 这类**整体包裹右值**的调用。自己写一个 `esc()` 逐段转义，门禁认不出来。

改用 DOM 构建（下一节），从根上消掉这个类别。

### 3 图片 CSS 约束不能丢（PQ203）

CSS 里必须有 `img{max-width:100%;height:auto}`。重写样式时最容易漏，漏了就是移动端横向滚动。

### 4 常量名必须是 `DATABASE_ID`（DSDK002）

门禁只认字面量 `databaseId: 'xxx'` 或**恰好叫 `DATABASE_ID` 的常量**。把它重构成 `DBID`、`dbId`、`DB` 都会 FAIL。沿用这个名字，别改。

### 5 绑定标注必须是标签字面量属性（DSDK011 / DSDK012）

§1.5.5 的 `data-sp-bindable="database"` 与 `data-sp-database-id="…"` 必须：

- **成对出现**，少一个就 FAIL；
- `data-sp-bindable` 的值恒为 `"database"`；
- `data-sp-database-id` 的值要和页面里出现的 databaseId 字面量一致；
- 不要用字符串拼接生成（`'data-sp-database-id="' + ID + '"'` 会因字面量比对不一致 FAIL）。

门禁扫的是 **HTML 原文里的标签属性**。用 `setAttribute('data-sp-bindable', 'database')` 动态加的，门禁扫不到 —— 所以要保证页面上**至少有一处静态标注**（例如表头的几个统计数字），否则会触发 DSDK012「整体漏标」。

## DOM 构建范式

一份不用 innerHTML、且天然满足 PQ001 + PQ005 的写法：

```javascript
// 所有节点都这样建：数据走 textContent，永不进 HTML 解析
function el(tag, cls, text) {
  var e = document.createElement(tag);
  if (cls) { e.className = cls; }
  if (text !== undefined && text !== null) { e.textContent = String(text); }
  return e;
}

function mark(node) {                      // 需要时就地打绑定标注
  node.setAttribute('data-sp-bindable', 'database');
  node.setAttribute('data-sp-database-id', DATABASE_ID);
  return node;
}

// 下拉框：用 createElement('option')，不要拼 <option> 字符串
function opt(label, value) {
  var o = document.createElement('option');
  o.textContent = String(label);
  o.value = String(value);
  return o;
}

// 列表：拼进 DocumentFragment 后一次性挂载，避免反复重排
var frag = document.createDocumentFragment();
for (var i = 0; i < list.length; i++) { frag.appendChild(buildCard(list[i])); }
elGrid.textContent = '';                   // 清空用 textContent = ''，比 innerHTML = '' 安全
elGrid.appendChild(frag);
```

事件委托仍然可用，但注意 `className` 可能是 SVG 对象而非字符串，比较前先 `String(node.className)`。

## 读数据必须自行分页

`db.query` 单次最多 200 条。少写了分页，页面只会显示前 200 条，而且**不报错**。

```javascript
function loadAll(startCursor, acc, guard) {
  acc = acc || []; guard = guard || 0;
  if (guard > 100) { finish(acc); return; }          // 三重保险之一
  db.query({
    databaseId: DATABASE_ID,
    filter: { property: { property: '整理状态', select: { equals: '待整理' } } },
    sorts: [{ property: '序号', direction: 'ascending' }],
    pageSize: 200,
    startCursor: startCursor
  }).then(function (r) {
    var page = r.results || [];
    acc = acc.concat(page);
    if (r.hasMore && r.nextCursor && r.nextCursor !== startCursor && page.length) {
      loadAll(r.nextCursor, acc, guard + 1);
    } else { finish(acc); }
  }).catch(function (e) { console.error(e); finish(acc); });
}
```

三重保险：`guard` 上限 + 游标必须变 + 本页非空。任一失效都不会变成死循环。

**catch 里不要回填假数据** —— 读不到就显示空态并说明，塞示例数据会被门禁判 FAIL（DSDK016），也是自欺。

## 就地编辑

用户在页面上改的值要**立刻写回表**，不要攒到「提交」：

```javascript
db.updateRecord({
  databaseId: DATABASE_ID,
  recordId: rid,
  properties: { '最终去向': { select: val } }
}).then(ok).catch(fail);
```

### 记录主键是 `_id`，不是 `record_id`

`db.query` / `db.getRecord` 返回的记录是扁平对象，**主键字段名是系统字段 `_id`**（SDK 契约 §6 的返回示例：`{"_id": "2rVLfQTl3uHurZq7dfrbdG", "姓名": "小李", ...}`）。

臆造一个 `record_id` 会拿到 `undefined`：`card.setAttribute('data-rid', r.record_id)` → `data-rid=""` → `updateRecord({ recordId: '' })`。
而 **SDK 对空 recordId 是 resolve 而不是 reject** —— 页面照常显示「已保存」，数据却一次都没进库。

本项目已经实打实栽过这一跤：用户审完一轮，456 条的批注与去向改动**全部丢失**，页面上却处处写着「已保存」。

取主键一律走统一入口，不要在别处直接点字段名：

```javascript
function ridOf(r) { return (r && (r._id || r.record_id || r.recordId)) || ''; }
```

**并且主键取不到时必须让失败可见**：

```javascript
var rid = ridOf(row);
if (!rid) {                       // 宁可红字报错，也绝不假装保存成功
  setStatus(card, 'err', '主键缺失');
  console.error('[database] 记录缺少主键 _id');
  return;
}
```

### 别只靠 `change` 保存

`change` 对文本输入框**只在失焦/回车时触发**。而列表页普遍「筛选一变就整块重渲染」——元素先被销毁，`change` 就永远不会触发，**用户刚敲的字直接蒸发**。

三层保险，缺一不可：

1. **`change`** —— 正常路径，失焦即存；
2. **`input` + 防抖**（约 1.2s）—— 覆盖「敲完字直接关页面」这种没有任何失焦事件的场景；
3. **重渲染前 flush** —— `render()` 的第一行先扫一遍 DOM，把与内存值不一致的输入提交掉，再重建：

```javascript
function captureEdits() {
  var cards = elGrid.querySelectorAll('.card');
  for (var i = 0; i < cards.length; i++) {
    var card = cards[i];
    var row = rowByRid(card.getAttribute('data-rid'));
    if (!row) { continue; }
    var m = card.querySelector('.memo');
    if (m && m.value !== (row['审阅批注'] || '')) { commitNote(card, row, m.value); }
  }
}
```

事件委托里判类要用 `String(t.className).indexOf('memo') !== -1`，
**不要写 `t.className === 'memo'`** —— 保存成功后常会附加类名（如 `memo has`），
精确相等判断会让**同一个框的第二次编辑彻底失效**。

### 「成功」不等于「已落库」

`updateRecord` / `batchAdd` 的 resolve 只代表请求被接受。写完**务必回读核验**
（宿主侧 `get_database_record.py`，或在页面上下一次 `query`）。
本项目栽过两次同类问题：`batch_add` 因选项文本带空格被服务端拒收、页面因主键取空而假成功。
**凡是「页面说成功、库里没数据」，先怀疑主键和字段值合法性，别怀疑网络。**

写入侧还有一条更省事的自检：契约里 `db.updateRecord` → **`{ id }`**（§1）。别只看 resolve，顺手验返回值：

```javascript
.then(function (res) {
  if (!res || !res.id) { markFail('未确认：写入返回无 id'); return; }   // 空 recordId 也会走到这里
  markOk();
})
```

## 让「真假保存」一眼可辨

事故复盘的核心结论：**光把保存做对不够 —— 用户必须能自己看出来"我改的到底进库没有、我看的是不是最新页面"。**
四件东西缺一不可。

### 1 页面版本标识（防旧缓存）

单文件页没有构建流程，最可靠的做法是**把版本写成 HTML 静态文本，JS 只负责读它**，避免两处维护不一致：

```html
<span id="verBadge">v4 · 2026-10-07 16:40</span>
```

```javascript
var t = (document.getElementById('verBadge') || {}).textContent || '';
if (t) { document.title = '… · ' + t.trim(); }                        // 标题里也带上，随时可见
var m = document.getElementById('fMtime');
if (m) { m.textContent = '页面文件时间：' + document.lastModified; }   // 服务端来源的旁证
```

**每次更新页面必须同步改这行文字**，交付时明确要求用户「关掉旧标签页重开，确认版本号 = vX 再动数据」。
字符串里**不要出现 `/`** —— 会被 `_strip_regex_literals` 误判成正则起点，导致 PQ001 括号误报。

### 2 开屏自检（把假成功变成开屏可见的失败）

别等用户改一条才发现写不进去。数据加载完立刻自检，缺主键就红条报警并**禁用那些卡片的控件**：

```javascript
function selfCheck() {
  var miss = 0, i;
  for (i = 0; i < rows.length; i++) { if (!ridOf(rows[i])) { miss++; } }
  if (miss) { showBanner('err', '严重：' + miss + ' 条记录读不到主键 _id，已置灰无法写入。'); }
  else { showBanner('ok', '自检通过：' + rows.length + ' 条记录均带主键 _id。'); }
}
```

`getSchema` 回调里也顺手确认关键字段在不在，缺了直接报，别等写入时才炸。

### 3 状态账本 + 失败清单

维护 `saveState[rid] = 'pending' | 'ok' | 'err'` 与 `failLog[]`，状态栏显示
`已保存 N · 失败 M · 进行中 P`，失败明细可展开。**所有 catch 都要接到用户可见的地方** ——
只 `console.error` 等于没报（门禁也会给 PQ107 警告）。

### 4「重新加载并核对」——用数据本身自证

比任何「已保存」文案都可靠：先 flush 未落库的编辑，再全量重查，然后拿库里的值逐条比对本地
`ledger`（记为已保存成功的字段值）：

```javascript
function verifyLedger() {
  var checked = 0, bad = [], rid, f;
  for (rid in ledger) {
    var row = rowByRid(rid);
    if (!row) { continue; }
    for (f in ledger[rid]) {
      checked++;
      if (String(row[f] || '') !== String(ledger[rid][f] || '')) { bad.push('#' + row['序号'] + ' ' + f); }
    }
  }
  showBanner(bad.length ? 'err' : 'ok',
    bad.length ? ('核对不通过：' + bad.join('、')) : ('已确认入库 ' + checked + ' 个字段'));
}
```

**只在用户点按钮时做这一次全量比对**，不要每条保存都回读（页面 20 QPS 是硬上限）。

### 写入要串行化

用户点「立即保存全部」会一次刷出几十条写请求，用队列泵串起来，避免瞬时并发打爆限流：

```javascript
var q = [], running = false;
function enqueue(task) {
  q.push(task);
  if (running) { return; }
  running = true;
  (function pump() {
    if (!q.length) { running = false; return; }
    var p = null;
    try { p = q.shift()(); } catch (e) { p = null; }
    if (p && p.then) { p.then(pump, pump); } else { setTimeout(pump, 0); }
  })();
}
```

### 不要做的事

- ❌ 把自动保存改成「只有点按钮才入库」——多一条"忘记点就整批丢"的路，且对上面这些坑毫无帮助
- ❌ 每条保存后回读校验（QPS 翻倍）
- ❌ `localStorage` 草稿持久化；`onUpdated` 自动重刷（会清掉未落库的输入）；撤销/历史引擎

## 上传与更新

```bash
<python> <page-skill>/import_html.py <page.html> --file-name "名字.html" --databases '[{"id":"<db>"}]'
```

**更新已有页面**（不要新建节点）：

```bash
<python> <page-skill>/import_html.py <page.html> --file-name "名字.html" --node-block-id <已有的 node_block_id>
```

只传 `--parent-id` / `--space-id` 是**首次**导入用的，对它已经存在的页面不生效。要覆盖必须传 `--node-block-id`。

上传后用 `<page-skill>/page_database_relation.py --action list --page-id <id>` 复核页面与数据表仍然挂着。

## 页面的信息设计

审阅页只需要回答一个问题：「这条该去哪」。所以：

- 首屏给总量 / 拿不准数量 / 已改数量，让人知道还要看多久；
- 拿不准的用醒目左边框 + 角标，别让它淹没在 400 张卡片里；
- 支持搜索、按去向筛、只看拿不准；
- 每张卡片给出**判定理由**（人才能判断对错），以及一个**批注框**（人才能把判断依据还给你）；
- 卡片上直接放去向下拉框，改完即存。

清单页则相反：它是给人**浏览**的，按二级子类分组、突出内容描述，不需要就地编辑。

## 只读数据页范式（清单页 / 字幕报告页）

审阅页要**写**，清单页和报告页只要**读**。省掉整套保存账本之后，留下的骨架是：

1. `__SMART_PAGE__` 存在性守卫 → 没有 SDK 就直接给「请在资料库中打开本页」，别白屏；
2. 开屏先渲染**加载态**，再 `db.query`；
3. `sorts` 只用 schema 里确实存在的字段（`序号` 最稳），`pageSize` 200，**循环 `hasMore` / `nextCursor` 拉全量**；
4. 拿到全量后在**客户端**筛选、分组、聚合 —— 不赌服务端 filter 语法；
5. `layer` 三态齐全：加载态 / 空态（表里没数据时告诉人"写回字幕状态就会出现"）/ 错误态（把 err.message 显示出来，不能只 console）；
6. 所有文本走 `createElement` + `textContent`，图片才用 `img.src`；
7. 每个 `db.*` 的 promise 链都要 `.catch`。

### 双表页面的 id 可信集合

报告页同时读「收藏视频数据库」和「字幕待定分P」。门禁会收集页面里**可信的 databaseId 字面量**，
只有两种写法算数：

- `var DATABASE_ID = '<id>'`（**常量名必须正好是 `DATABASE_ID`**）
- 调用里直接写的字面量 `databaseId: '<id>'`

于是 `var PENDING_DB = '<id>'` + `databaseId: PENDING_DB` 这种"起个好名字"的写法，
会让第二张表的 id **进不了可信集合** —— 一旦你给待定表的元素加了
`data-sp-database-id="<第二张表>"`，DSDK011 直接判不一致。

解法：**第二张表的查询调用直接写字面量 id**，例如
`db.query({ databaseId: 'FE2MygjHui72lBZKH8amQt', … })`，注释说明它是什么表。
DSDK002 明确允许字面量，不必为"优雅"绕这一圈。

### 卡片里的封面

封面直接从数据表 `封面` 列取 `[0].imageUrl`（已在托管域，无防盗链），**不需要再上传一次**。
`<img>` 必须同时给 `width` / `height` 属性（PQ104）与 `loading="lazy"`（PQ101），
CSS 里 `img{max-width:100%;height:auto}` 一条都不能少（PQ203），
卡片的固定尺寸用更具体的选择器（`.item .thumb img`）压过去。

封面外面再套一层 `<a>`，点图即跳 B 站 —— 比只有标题可点更符合「看封面想点开」的直觉：

```javascript
var img = el('img', 'cover');
img.src = coverOf(r); img.alt = title; img.width = 320; img.height = 200; img.loading = 'lazy';
mark(img);
if (href) {
  var a = el('a', 'thumblink');       // 外层锚点也要 mark()，href 同样来自数据表
  a.href = href; a.target = '_blank'; a.rel = 'noopener';
  mark(a); a.appendChild(img); box.appendChild(a);
} else { box.appendChild(img); }
```

配套 CSS：锚点 `display:block;width:100%;height:100%` 撑满缩略图框，
再用 `::after` 做 hover 蒙层（`content:"▶ 去 B 站看"`），鼠标移上去才显形 —— 不占版面又能提示可点。
无链接时不要把 `<a>` 建出来，直接放 `<img>`，免得出现点了没反应的空锚点。

### 链接字段取值的坑：接口返回的是**数组**

`视频链接`、`分P链接` 这类 `url` 字段，接口返回的是 **`[{link, text}]` 数组**，不是 `{link, text}` 对象。
照 `u.link` 写会永远取到 `undefined` → 页面静默降级成纯文本，**不报错、不白屏，只是链接全没了**。
（`封面` 同理是数组，所以 `cover[0].imageUrl` 才对。）

统一用这个兼容三种形态的取值器，两类字段共用：

```javascript
function urlOf(v) {
  if (!v) { return ''; }
  if (Object.prototype.toString.call(v) === '[object Array]') { v = v[0]; }
  if (!v) { return ''; }
  if (typeof v === 'string') { return v; }
  return v.link || v.text || '';
}
function linkOf(r) { return urlOf(r['视频链接']); }   // 行内再写 urlOf(r['分P链接'])
```

改完拿真实数据自证：把 `_state/db_records.json` 灌进 Node，统计「解析出合法 `https?://` 的条数」应等于总条数。
仓里备了一个最小 DOM 桩跑真实渲染逻辑（不联网、不开浏览器）：

```bash
node tools/verify_render.js build/list.html   _state/db_records.json
node tools/verify_render.js build/report.html _state/db_records.json _state/pending_records.json
```

数出来「渲染 `<a>` N 个（其中封面锚点 M 个）/ href 非法 0 个 / ✅ 通过」才算数 ——
**链接静默消失这类 bug 不报错、不白屏**，静态检查三件套全都发现不了，只有真跑一遍才现形。

