# 阳光高考院校库 Playwright 最小抓取器

## 功能

本项目使用 Playwright 通过真实 Chromium 浏览器访问阳光高考 WAP 院校库页面，自动执行页面内 JS，获取阿里云/站点 Cookie。当前抓取策略已调整为：

1. 访问学校列表页并从页面 DOM/链接/脚本中提取学校 ID：

```text
https://gaokao.chsi.com.cn/wap/sch/schlist
```

2. 根据学校 ID 逐个访问院校库详情页，并从渲染后的页面中提取院校库信息：

```text
https://gaokao.chsi.com.cn/wap/sch/schinfomain/{school_id}
```

最终输出：

```text
output/chsi_colleges.json             标准化后的院校库信息
output/chsi_school_pages_raw.json     列表页 ID 与每个详情页原始信息
output/chsi_network_log.json          每次页面访问日志
logs/crawler.log                      全流程运行日志
storage_state.json                    Playwright Cookie/状态缓存
```

## 目录结构

```text
chsi_playwright_min_crawler/
├── crawler.py
├── requirements.txt
├── .env.example
├── README.md
├── output/
└── logs/
```

## 环境要求

建议：

```text
Python 3.10+
macOS / Linux / Windows
Chrome/Chromium 由 Playwright 自动安装
```

## 安装流程

### 1. 解压项目

```bash
unzip chsi_playwright_min_crawler.zip
cd chsi_playwright_min_crawler
```

### 2. 创建虚拟环境

macOS / Linux：

```bash
python3 -m venv .venv
source .venv/bin/activate
```

Windows PowerShell：

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

### 3. 安装 Python 依赖

普通安装：

```bash
pip install -r requirements.txt
```

国内网络建议使用清华源：

```bash
pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
```

### 4. 安装 Chromium

```bash
python -m playwright install chromium
```

Linux 服务器如果缺少系统依赖，可执行：

```bash
python -m playwright install --with-deps chromium
```

## 运行

首次运行建议显示浏览器：

```bash
HEADLESS=false python crawler.py
```

如果使用 Windows PowerShell：

```powershell
$env:HEADLESS="false"
python crawler.py
```

首次成功后，会生成：

```text
storage_state.json
```

后续可以尝试无头运行：

```bash
HEADLESS=true python crawler.py
```

## 配置项

可以通过环境变量控制：

| 变量 | 默认值 | 含义 |
|---|---:|---|
| HEADLESS | false | 是否无头运行，首次建议 false |
| START | 0 | 从提取到的学校 ID 列表中跳过多少个，便于断点续跑 |
| MAX_SCHOOLS | 0 | 最多抓取多少个学校，0 表示不限制 |
| MAX_PAGES | 0 | 兼容旧变量；未设置 MAX_SCHOOLS 时作为 MAX_SCHOOLS 使用 |
| LIST_SCROLL_ROUNDS | 80 | 在 schlist 列表页最多滚动多少轮以触发懒加载 |
| REQUEST_INTERVAL_SECONDS | 1.0 | 每个详情页请求间隔 |

示例：只抓前 3 个学校详情页：

```bash
HEADLESS=false MAX_SCHOOLS=3 python crawler.py
```

从提取到的第 11 个学校开始抓：

```bash
START=10 python crawler.py
```

## 输出字段说明

`output/chsi_colleges.json` 中每条记录结构：

```json
{
  "school_id": "1",
  "name": "北京大学",
  "source": "chsi_gaokao_schinfomain",
  "source_url": "https://gaokao.chsi.com.cn/wap/sch/schinfomain/1",
  "final_url": "https://gaokao.chsi.com.cn/wap/sch/schinfomain/1",
  "title": "北京大学_阳光高考",
  "fields": {
    "院校所在地": "北京",
    "院校类型": "综合"
  },
  "text": "页面正文文本",
  "raw": {}
}
```

## 原理说明

### 为什么不主动调用 schsearch？

`schsearch?start=...` 在部分环境下会被风控重写为随机参数 URL，并返回 `400 Bad Request` 或空 HTML。当前策略改为先从 WAP 列表页：

```text
https://gaokao.chsi.com.cn/wap/sch/schlist
```

提取页面中出现的学校 ID，再逐个访问：

```text
https://gaokao.chsi.com.cn/wap/sch/schinfomain/{school_id}
```

这样每个详情页都由真实 Chromium 以页面导航方式打开，能执行详情页上的 JS 和可能出现的挑战脚本。WAP 页面自身的 `mounted/getSchList` 仍可能自动请求 `/wap/sch/schsearch`，但程序不再在页面刚加载时额外重复调用它；如果首次请求早于挑战 Cookie 完成而弹出“服务异常”，会关闭弹窗，在 Cookie 预热完成后重新进入 WAP 列表页，让页面自身再自动请求一次。若自动请求仍失败，程序会在同源页面上下文中用 `schlist.html` 的同一套 `postdata` 补发 WAP `schsearch`，并把成功结果写回 `#app.__vue__.list`，便于后续统一提取。

### 如何从 schlist 获取学校 ID？

程序会访问 `schlist`，等待 DOMContentLoaded、networkidle 和额外渲染时间，然后观察 `schlist.html` 源码中 `mounted` 自动触发的 `getSchList()` 是否把结果写入 `#app.__vue__.list`。程序不会再额外主动调用 `getSchList()`，避免在 `/wap/sch/schsearch` 异常时重复弹出“服务异常”。每轮都会从以下位置提取 ID：

- Vue 实例 `#app.__vue__.list` 中的 `item.schId` 字段（优先来源）；
- `a[href]`、`url` 中的 `/wap/sch/schinfomain/{id}`、`/wap/sch/schinfo/{id}` 等链接；
- `onclick`、`data-href`、`data-url` 等属性；
- 页面 HTML / 内联脚本 / JSON 文本中的 `schId` 字段。

翻页时会优先滚动页面，让 Vant `van-list` 自然触发源码里的 `onLoad`；如果懒加载没有触发，也会用同源 WAP `schsearch` 按当前 `startOfNextPage` 补取下一页。程序不会加入桌面端列表 HTML 兜底，以免掩盖 WAP 列表页自身的问题；如果仍未拿到 ID，会保留 `chsi_network_log.json`、`chsi_warmup_goto.curl.sh`、`chsi_warmup_diagnostics.json` 和 `chsi_warmup_page.html` 供定位。

### 如何定位 warmup 时的“服务异常”？

`page.goto` 成功只代表 `schlist` 主文档返回成功；弹窗通常来自页面随后自动发起的 `/wap/sch/schsearch`、`/wap/sch/querycondition` 等 XHR/fetch。程序会在 warmup 导航前注册请求、响应、requestfailed、console、pageerror 和 dialog 监听，并把以下信息写入 `output/chsi_warmup_diagnostics.json`：

- 关键请求/响应的 URL、方法、资源类型、状态码、POST data 和响应正文预览；
- Vant 弹窗文本、Vue `#app.__vue__` 状态、`listLength/loading/finished/nextPageAvailable/startOfNextPage`；
- `snapshot.getSchListEvents`：页面脚本执行前注入的探针会包装根 Vue 实例的 `this.getSchList()`，记录它是否被自动触发、触发时完整 `postdata`、调用前后 Vue 状态，以及内部 `api.syncAjax` 的完整响应/异常；
- 当前 Cookie 名称和关键 Performance ResourceTiming；
- `probable_cause` 字段会根据弹窗、`getSchList` 调用和 `schsearch` 响应粗略判断原因。

同时会保存 `output/chsi_warmup_page.html` 作为页面现场快照。优先查看 diagnostics 中 `snapshot.getSchListEvents`：如果没有 `getSchList.call`，说明 `this.getSchList()` 没有自动触发；如果有 `getSchList.call`，继续看同一 `callId` 下的 `api.syncAjax.call` 和 `api.syncAjax.resolved/rejected`，其中会包含完整参数和响应结果。再结合 `/wap/sch/schsearch` 的 `body_preview` 判断是否为 `flag=false`、`服务异常`、非 JSON 或风控页。

如果 diagnostics 中出现 `/wap/sch/schsearch?随机参数=...` 或 `/wap/sch/querycondition?随机参数=...` 且状态码为 400，原因是页面/风控脚本把 WAP 接口 URL 从源码中的原始路径改写成了带随机 query 的地址，而服务端不接受这个变形后的接口 URL。程序会在 Playwright route 层记录 `wap_api_url_normalized` 事件，并把这两个 WAP 接口规范化回无 query 的原始 URL 后继续请求，便于验证是否就是 URL 改写导致的 400。

### Cookie 如何持久化？

Playwright 的：

```python
context.storage_state(path="storage_state.json")
```

会保存 Cookie 与 localStorage。下次启动时：

```python
browser.new_context(storage_state="storage_state.json")
```

即可复用。

## 常见问题

### 1. 返回 400 / 403 或出现验证页怎么办？

优先尝试：

```bash
HEADLESS=false python crawler.py
```

然后观察浏览器是否出现验证页。程序会在抓取列表页和每个详情页前后保存 Cookie 状态，便于后续复用。

### 2. 没有提取到学校 ID 怎么办？

查看：

```text
logs/crawler.log
output/chsi_network_log.json
```

重点确认 `schlist` 是否正常渲染、是否出现验证页，以及是否需要调大 `LIST_SCROLL_ROUNDS` 或先用 `HEADLESS=false` 完成一次挑战。

### 3. 服务器部署怎么跑？

Linux 上建议：

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m playwright install --with-deps chromium
HEADLESS=true python crawler.py
```

如果无头模式触发风控，可以先在本地 `HEADLESS=false` 跑一次生成 `storage_state.json`，再上传到服务器复用。

## 数据入库建议

PostgreSQL 表结构可参考：

```sql
CREATE TABLE colleges (
  id BIGSERIAL PRIMARY KEY,
  school_id VARCHAR(32) UNIQUE NOT NULL,
  name VARCHAR(128),
  source VARCHAR(64),
  source_url TEXT,
  final_url TEXT,
  title TEXT,
  fields JSONB,
  text TEXT,
  raw JSONB,
  created_at TIMESTAMP DEFAULT now(),
  updated_at TIMESTAMP DEFAULT now()
);
```
