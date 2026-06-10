# 阳光高考院校库 Playwright 最小抓取器

## 功能

本项目使用 Playwright 通过真实 Chromium 浏览器访问阳光高考 WAP 院校库页面，自动执行页面内 JS，获取阿里云/站点 Cookie，然后在同一个 BrowserContext 内通过 `fetch(..., {credentials: 'include'})` 请求：

```text
https://gaokao.chsi.com.cn/wap/sch/schsearch?start=0
https://gaokao.chsi.com.cn/wap/sch/schsearch?start=10
...
```

最终输出：

```text
output/chsi_colleges.json             标准化后的院校列表
output/chsi_school_pages_raw.json     每页原始 JSON
output/chsi_network_log.json          每次请求/响应日志
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
| START | 0 | 起始分页偏移量 |
| MAX_PAGES | 0 | 最大抓取页数，0 表示不限制 |
| REQUEST_INTERVAL_SECONDS | 1.0 | 每页请求间隔 |

示例：只抓前 3 页：

```bash
HEADLESS=false MAX_PAGES=3 python crawler.py
```

从第 2 页开始抓：

```bash
START=10 python crawler.py
```

## 输出字段说明

`output/chsi_colleges.json` 中每条记录结构：

```json
{
  "sch_id": "1",
  "sch_info_id": "99617187",
  "school_code": "10001",
  "name": "北京大学",
  "province": "北京",
  "authority": "教育部",
  "school_type": "综合院校",
  "education_level": "本科",
  "is_double_first_class_university": true,
  "is_double_first_class_subject": false,
  "has_master_degree": true,
  "has_doctor_degree": true,
  "satisfaction_score": 4.6,
  "source": "chsi_gaokao_schsearch",
  "source_url": "https://gaokao.chsi.com.cn/wap/sch/schsearch?start={start}",
  "raw": {}
}
```

## 原理说明

### 为什么先访问 schlist？

直接访问 `schsearch` 可能缺少 Cookie，容易出现：

```text
400 Bad Request
403 Forbidden
空 HTML
阿里云 JS 挑战页
```

所以程序先访问：

```text
https://gaokao.chsi.com.cn/wap/sch/schlist
```

让真实 Chromium 自动执行页面 JS，完成 Cookie 写入。

### 为什么不用 page.goto 访问 schsearch？

`page.goto()` 会把接口当作页面导航请求：

```text
resource_type=document
is_navigation_request=true
```

更推荐在页面上下文里执行：

```javascript
fetch(url, { credentials: 'include' })
```

这样会自动携带当前 BrowserContext Cookie，更接近页面内 AJAX 请求。

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

### 1. 返回 400 / 403 怎么办？

优先尝试：

```bash
HEADLESS=false python crawler.py
```

然后观察浏览器是否出现验证页。程序会自动重新 warmup 并重试一次。

### 2. JSON 解析失败怎么办？

查看：

```text
logs/crawler.log
output/chsi_network_log.json
```

重点看 `bodyText` 前几百个字符，判断是 HTML、空响应还是挑战页。

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
  sch_id VARCHAR(32) UNIQUE NOT NULL,
  sch_info_id VARCHAR(32),
  school_code VARCHAR(32),
  name VARCHAR(128) NOT NULL,
  province VARCHAR(32),
  authority VARCHAR(128),
  school_type VARCHAR(64),
  education_level VARCHAR(32),
  is_double_first_class_university BOOLEAN,
  is_double_first_class_subject BOOLEAN,
  has_master_degree BOOLEAN,
  has_doctor_degree BOOLEAN,
  satisfaction_score NUMERIC(3,1),
  source VARCHAR(64),
  source_url TEXT,
  raw JSONB,
  created_at TIMESTAMP DEFAULT now(),
  updated_at TIMESTAMP DEFAULT now()
);
```
