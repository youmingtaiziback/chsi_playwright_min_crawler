"""
阳光高考院校库最小 Playwright 抓取器。

目标：
1. 访问 https://gaokao.chsi.com.cn/wap/sch/schlist，让浏览器自动执行页面 JS 并从列表页提取学校 ID。
2. 逐个访问 https://gaokao.chsi.com.cn/wap/sch/schinfomain/{school_id}，让详情页 JS 执行后提取院校库信息。
3. 自动保存 Cookie / storage_state，后续启动复用。
4. 输出院校库信息 JSON、原始详情页 JSON、网络日志 JSON、运行日志。

注意：
- 本程序仅用于你有权访问的公开网页数据采集与调试。
- 首次运行建议 HEADLESS=false，便于浏览器自然完成 JS 挑战。
"""

# 导入异步 IO 库，用于运行 Playwright 的 async API。
import asyncio
# 导入 JSON 库，用于解析接口响应与写出 JSON 文件。
import json
# 导入 logging 库，用于输出全流程日志。
import logging
# 导入 os 库，用于读取环境变量。
import os
# 导入 re 库，用于从链接、脚本和 HTML 中提取学校 ID。
import re
# 导入 time 库，用于耗时统计。
import time
# 导入 datetime，用于生成日志时间与文件元数据时间。
from datetime import datetime
# 导入 Path，用于跨平台处理文件路径。
from pathlib import Path
# 导入类型注解，方便理解参数与返回值结构。
from typing import Any

# 导入 Playwright 异步入口。
from playwright.async_api import async_playwright
# 导入 Playwright 的 BrowserContext 类型，用于类型注解。
from playwright.async_api import BrowserContext
# 导入 Playwright 的 Page 类型，用于类型注解。
from playwright.async_api import Page
# 导入 Playwright 的 TimeoutError，并重命名，避免和 Python 内置 TimeoutError 混淆。
from playwright.async_api import TimeoutError as PlaywrightTimeoutError


# 项目根目录，即 crawler.py 所在目录。
BASE_DIR = Path(__file__).resolve().parent
# 输出目录，所有结果文件都写入这里。
OUTPUT_DIR = BASE_DIR / "output"
# 日志目录，运行日志写入这里。
LOG_DIR = BASE_DIR / "logs"
# Playwright 持久化登录态 / Cookie 文件。
STATE_FILE = BASE_DIR / "storage_state.json"
# 原始详情页响应输出文件。
RAW_PAGES_FILE = OUTPUT_DIR / "chsi_school_pages_raw.json"
# 合并后的院校库信息输出文件。
COLLEGES_FILE = OUTPUT_DIR / "chsi_colleges.json"
# 网络请求调试日志输出文件。
NETWORK_LOG_FILE = OUTPUT_DIR / "chsi_network_log.json"
# 学校列表页：用于执行 JS 挑战并提取学校 ID。
SCHOOL_LIST_URL = "https://gaokao.chsi.com.cn/wap/sch/schlist"
# 院校库详情页：通过学校 ID 获取院校库信息。
SCHOOL_INFO_URL = "https://gaokao.chsi.com.cn/wap/sch/schinfomain/{school_id}"
# 兼容旧命名：列表页同样承担预热 Cookie 与执行 JS 挑战的职责。
WARMUP_URL = SCHOOL_LIST_URL


# 创建输出目录，如果已经存在则不报错。
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
# 创建日志目录，如果已经存在则不报错。
LOG_DIR.mkdir(parents=True, exist_ok=True)


# 配置日志格式：同时输出到控制台和文件。
logging.basicConfig(
    # 日志级别为 INFO，DEBUG 以下不输出。
    level=logging.INFO,
    # 日志格式包含时间、级别、模块名和内容。
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    # 同时配置两个 Handler：控制台 + 文件。
    handlers=[
        # 控制台输出，便于实时观察。
        logging.StreamHandler(),
        # 文件输出，便于后续定位问题。
        logging.FileHandler(LOG_DIR / "crawler.log", encoding="utf-8"),
    ],
)
# 创建当前模块使用的 logger。
logger = logging.getLogger("chsi-crawler")


# 从环境变量读取布尔值的小工具。
def env_bool(name: str, default: bool) -> bool:
    # 读取环境变量；如果不存在，使用默认值。
    value = os.getenv(name)
    # 如果没有配置，则返回默认值。
    if value is None:
        return default
    # 将字符串转小写后判断是否属于 true 值。
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


# 从环境变量读取整数的小工具。
def env_int(name: str, default: int) -> int:
    # 读取环境变量；如果不存在，返回默认值。
    value = os.getenv(name)
    # 如果没有配置，则直接返回默认值。
    if value is None:
        return default
    # 尝试转为整数。
    return int(value)


# 从环境变量读取浮点数的小工具。
def env_float(name: str, default: float) -> float:
    # 读取环境变量；如果不存在，返回默认值。
    value = os.getenv(name)
    # 如果没有配置，则直接返回默认值。
    if value is None:
        return default
    # 尝试转为浮点数。
    return float(value)


# 定义爬虫类，把状态、日志、抓取逻辑组织在一起。
class ChsiSchoolCrawler:
    # 初始化爬虫配置。
    def __init__(self) -> None:
        # 是否使用无头模式；默认 false，更容易通过 JS 挑战。
        self.headless = env_bool("HEADLESS", False)
        # 从提取到的学校 ID 列表中跳过多少个；用于断点续跑。
        self.start = env_int("START", 0)
        # 最多处理多少个学校；0 表示不限制。
        self.max_schools = env_int("MAX_SCHOOLS", env_int("MAX_PAGES", 0))
        # 列表页最多滚动多少轮，用于触发移动端懒加载。
        self.list_scroll_rounds = env_int("LIST_SCROLL_ROUNDS", 80)
        # 每个详情页请求之间的间隔秒数。
        self.request_interval_seconds = env_float("REQUEST_INTERVAL_SECONDS", 1.0)
        # 用于保存从列表页提取到的学校 ID。
        self.school_ids: list[str] = []
        # 用于保存每个详情页的原始信息。
        self.raw_pages: list[dict[str, Any]] = []
        # 用于保存标准化后的院校库信息。
        self.colleges: list[dict[str, Any]] = []
        # 用于保存每次页面访问的调试日志。
        self.network_logs: list[dict[str, Any]] = []

    # 判断页面或响应是否像阿里云 / WAF / JS 挑战页。
    def is_challenge_text(self, text: str) -> bool:
        # 如果正文为空，通常也是异常情况，但不一定是挑战页。
        if not text:
            return False
        # 转小写，便于匹配英文关键字。
        lower = text.lower()
        # 关键字列表覆盖常见阿里云风控、acw、滑块/挑战脚本痕迹。
        keywords = [
            "aliyun",
            "acw_tc",
            "acw_sc",
            "awsc",
            "challenge",
            "captcha",
            "安全验证",
            "访问验证",
            "人机验证",
            "风险验证",
        ]
        # 只要命中任一关键字，就认为可能是挑战页。
        return any(keyword.lower() in lower for keyword in keywords)

    # 将 BrowserContext 中的 Cookie 转为便于日志观察的简要形式。
    async def log_cookie_summary(self, context: BrowserContext) -> None:
        # 获取当前 context 下所有 Cookie。
        cookies = await context.cookies()
        # 只提取 Cookie 名称，不打印完整值，避免日志过长。
        names = sorted(cookie.get("name", "") for cookie in cookies)
        # 输出 Cookie 数量和名称列表。
        logger.info("当前 Cookie 数量=%s，名称=%s", len(cookies), names)
        # 重点检查和阳光高考/阿里云风控相关的 Cookie 是否存在。
        important = ["JSESSIONID", "CHSICC01", "acw_tc", "aliyungf_tc", "CHSICC_CLIENTFLAGGAOKAO"]
        # 输出每个重点 Cookie 是否出现。
        logger.info("关键 Cookie 状态=%s", {name: name in names for name in important})

    # 访问列表页，等待 JS 挑战自动完成并写入 Cookie。
    async def warmup(self, page: Page, context: BrowserContext) -> None:
        # 输出预热开始日志。
        logger.info("开始访问学校列表页：%s", SCHOOL_LIST_URL)
        # 访问列表页，等待 DOMContentLoaded 即可，不强求 networkidle，避免某些统计请求长时间挂起。
        response = await page.goto(SCHOOL_LIST_URL, wait_until="domcontentloaded", timeout=60_000)
        # 如果拿到响应对象，则输出状态码。
        if response:
            logger.info("学校列表页响应：status=%s url=%s", response.status, response.url)
            # 记录列表页访问日志。
            self.network_logs.append(
                {
                    "request": {"url": SCHOOL_LIST_URL, "method": "GET", "mode": "page_goto"},
                    "response": {"status": response.status, "url": response.url, "ok": response.ok},
                    "recorded_at": datetime.now().isoformat(timespec="seconds"),
                }
            )
        # 尝试等待网络空闲，让挑战脚本、列表脚本、Cookie 写入有时间完成。
        try:
            await page.wait_for_load_state("networkidle", timeout=30_000)
            logger.info("学校列表页 networkidle 完成")
        except PlaywrightTimeoutError:
            logger.warning("学校列表页等待 networkidle 超时，继续后续流程")
        # 额外等待 2 秒，给阿里云 JS 挑战和列表接口写 Cookie / 渲染 DOM 的时间。
        await page.wait_for_timeout(2_000)
        # 打印 Cookie 概况，用于判断挑战是否完成。
        await self.log_cookie_summary(context)
        # 保存当前 storage_state，后续启动可复用 Cookie。
        await context.storage_state(path=str(STATE_FILE))
        # 输出保存状态日志。
        logger.info("已保存 storage_state：%s", STATE_FILE)

    # 从字符串中提取学校 ID，覆盖 schinfomain 链接、schinfo 链接和脚本中的 schId。
    def extract_school_ids_from_text(self, text: str) -> list[str]:
        # 使用列表保持发现顺序，使用集合去重。
        ids: list[str] = []
        seen: set[str] = set()
        # 正则覆盖 /wap/sch/schinfomain/123、/wap/sch/schinfo/123 等链接。
        patterns = [
            r"/wap/sch/(?:schinfomain|schinfo|schdetail)/(\d+)",
            r"schinfomain/(\d+)",
            r"schId[\"'\s:=]+(\d+)",
        ]
        # 逐个模式匹配。
        for pattern in patterns:
            for match in re.finditer(pattern, text):
                school_id = match.group(1)
                if school_id not in seen:
                    ids.append(school_id)
                    seen.add(school_id)
        # 返回保持原始顺序的学校 ID。
        return ids

    # 读取当前列表页 DOM，并从链接、onclick、HTML 中提取学校 ID。
    async def extract_school_ids_from_page(self, page: Page) -> list[str]:
        # 在页面中收集所有可能包含学校详情地址的 DOM 内容。
        snapshot = await page.evaluate(
            """
            () => {
                const attrs = [];
                for (const node of document.querySelectorAll('a[href], [onclick], [data-href], [data-url]')) {
                    for (const name of ['href', 'onclick', 'data-href', 'data-url']) {
                        const value = node.getAttribute(name);
                        if (value) attrs.push(value);
                    }
                }
                return {
                    url: location.href,
                    title: document.title,
                    attrs,
                    html: document.documentElement.outerHTML
                };
            }
            """
        )
        # 合并属性与 HTML 后统一用 Python 正则提取，便于后续维护。
        combined = "\n".join(snapshot.get("attrs") or []) + "\n" + (snapshot.get("html") or "")
        # 提取学校 ID。
        school_ids = self.extract_school_ids_from_text(combined)
        # 输出当前页面提取结果。
        logger.info("当前列表页提取学校 ID 数量=%s url=%s title=%s", len(school_ids), snapshot.get("url"), snapshot.get("title"))
        # 返回学校 ID。
        return school_ids

    # 尝试点击列表页上的加载更多按钮。
    async def click_load_more_if_present(self, page: Page) -> bool:
        # 在页面内查找文本类似“加载更多 / 下一页”的可点击元素并点击。
        return await page.evaluate(
            """
            () => {
                const candidates = Array.from(document.querySelectorAll('button, a, div, span'));
                const node = candidates.find((item) => {
                    const text = (item.innerText || item.textContent || '').trim();
                    if (!text) return false;
                    return /加载更多|更多|下一页|换一批/.test(text) && !/没有更多|暂无更多/.test(text);
                });
                if (!node) return false;
                node.click();
                return true;
            }
            """
        )

    # 从 schlist 列表页获取学校 ID。
    async def collect_school_ids(self, page: Page, context: BrowserContext) -> list[str]:
        # 先访问列表页并执行挑战。
        await self.warmup(page, context)
        # 使用列表保持顺序，使用集合去重。
        school_ids: list[str] = []
        seen: set[str] = set()
        # 连续多轮没有新增 ID 时提前停止。
        stale_rounds = 0
        # 滚动列表页，触发移动端懒加载。
        for round_index in range(1, self.list_scroll_rounds + 1):
            # 提取当前 DOM 中的学校 ID。
            current_ids = await self.extract_school_ids_from_page(page)
            # 记录本轮新增数量。
            added = 0
            for school_id in current_ids:
                if school_id not in seen:
                    school_ids.append(school_id)
                    seen.add(school_id)
                    added += 1
            # 输出列表采集进度。
            logger.info("列表页采集进度：round=%s added=%s total_ids=%s", round_index, added, len(school_ids))
            # 如果配置了最大学校数量，并且已经达到，则停止滚动。
            if self.max_schools > 0 and len(school_ids) >= self.start + self.max_schools:
                logger.info("已提取到 START + MAX_SCHOOLS 所需数量，停止滚动列表页")
                break
            # 尝试点击加载更多，再滚动到底部。
            clicked = await self.click_load_more_if_present(page)
            await page.evaluate("() => window.scrollTo(0, document.body.scrollHeight)")
            await page.wait_for_timeout(1_500)
            # 如果连续 3 轮既没有新增 ID，也没有可点击加载更多，则认为列表已到底。
            if added == 0 and not clicked:
                stale_rounds += 1
                if stale_rounds >= 3:
                    logger.info("列表页连续 %s 轮没有新增学校 ID，停止滚动", stale_rounds)
                    break
            else:
                stale_rounds = 0
        # 如果 START 不为 0，则跳过前面的学校 ID，方便断点续跑。
        selected_ids = school_ids[self.start :]
        # 如果限制了最大学校数量，则截断。
        if self.max_schools > 0:
            selected_ids = selected_ids[: self.max_schools]
        # 记录最终列表页结果。
        self.school_ids = selected_ids
        self.raw_pages.append(
            {
                "source": "chsi_gaokao_schlist",
                "source_url": SCHOOL_LIST_URL,
                "total_discovered_school_ids": len(school_ids),
                "start": self.start,
                "max_schools": self.max_schools,
                "selected_school_ids": selected_ids,
                "recorded_at": datetime.now().isoformat(timespec="seconds"),
            }
        )
        # 输出最终学校 ID 数量。
        logger.info("学校 ID 采集完成：discovered=%s selected=%s", len(school_ids), len(selected_ids))
        # 保存当前输出，方便只验证列表阶段。
        self.save_outputs()
        # 返回学校 ID。
        return selected_ids

    # 访问院校库详情页，并提取页面中的院校信息。
    async def fetch_school_info(self, page: Page, context: BrowserContext, school_id: str) -> dict[str, Any]:
        # 拼接详情页 URL。
        url = SCHOOL_INFO_URL.format(school_id=school_id)
        # 输出请求开始日志。
        logger.info("开始请求院校库详情页：school_id=%s url=%s", school_id, url)
        # 记录开始时间，用于计算耗时。
        started = time.time()
        # 使用真实页面导航访问详情页，以便页面内 JS 和挑战脚本执行。
        response = await page.goto(url, wait_until="domcontentloaded", timeout=60_000)
        # 等待详情页后续接口、脚本和 Cookie 写入完成。
        try:
            await page.wait_for_load_state("networkidle", timeout=30_000)
            logger.info("院校库详情页 networkidle 完成：school_id=%s", school_id)
        except PlaywrightTimeoutError:
            logger.warning("院校库详情页等待 networkidle 超时，继续解析：school_id=%s", school_id)
        # 额外等待，给移动端页面渲染和挑战脚本执行时间。
        await page.wait_for_timeout(1_000)
        # 计算耗时。
        elapsed = round(time.time() - started, 3)
        # 如果拿到响应，则记录网络日志。
        if response:
            self.network_logs.append(
                {
                    "request": {"url": url, "method": "GET", "mode": "page_goto", "school_id": school_id},
                    "response": {"status": response.status, "url": response.url, "ok": response.ok},
                    "elapsed_seconds": elapsed,
                    "recorded_at": datetime.now().isoformat(timespec="seconds"),
                }
            )
            logger.info("院校库详情页响应：school_id=%s status=%s ok=%s elapsed=%ss", school_id, response.status, response.ok, elapsed)
        # 在页面内提取结构化信息和全文。
        page_data = await page.evaluate(
            """
            () => {
                const clean = (value) => (value || '').replace(/\u00a0/g, ' ').replace(/[ \t]+/g, ' ').trim();
                const lines = clean(document.body ? document.body.innerText : '')
                    .split('\n')
                    .map((line) => clean(line))
                    .filter(Boolean);
                const fields = {};
                const put = (key, value) => {
                    key = clean(key).replace(/[：:]+$/, '');
                    value = clean(value);
                    if (key && value && key.length <= 30 && !fields[key]) fields[key] = value;
                };
                for (const line of lines) {
                    const match = line.match(/^([^：:]{2,30})[：:]\\s*(.+)$/);
                    if (match) put(match[1], match[2]);
                }
                for (const row of document.querySelectorAll('tr')) {
                    const cells = Array.from(row.querySelectorAll('th,td')).map((cell) => clean(cell.innerText));
                    if (cells.length >= 2) put(cells[0], cells.slice(1).join(' '));
                }
                for (const item of document.querySelectorAll('li, p, div')) {
                    const text = clean(item.innerText);
                    const match = text.match(/^([^：:]{2,30})[：:]\\s*(.+)$/);
                    if (match) put(match[1], match[2]);
                }
                const headingNode = document.querySelector('h1, h2, .school-name, .sch-name, .name');
                const title = clean(document.title);
                const heading = clean(headingNode ? headingNode.innerText : '');
                return {
                    url: location.href,
                    title,
                    heading,
                    fields,
                    text: lines.join('\n'),
                    html: document.documentElement.outerHTML
                };
            }
            """
        )
        # 检查是否疑似挑战页。
        body_text = page_data.get("text") or ""
        html = page_data.get("html") or ""
        if self.is_challenge_text(body_text + "\n" + html):
            logger.warning("院校库详情页疑似挑战页：school_id=%s preview=%s", school_id, body_text[:300])
            raise RuntimeError(f"院校库详情页疑似阿里云/风控挑战页，school_id={school_id}")
        # 如果响应异常且页面正文为空，按异常处理并交给上层重试。
        if response and not response.ok and not body_text.strip():
            logger.warning("院校库详情页为空且状态异常：school_id=%s status=%s final_url=%s", school_id, response.status, page_data.get("url"))
            raise RuntimeError(f"院校库详情页为空且状态异常，school_id={school_id}")
        # 打印并保存 Cookie，便于复用当前状态。
        await self.log_cookie_summary(context)
        await context.storage_state(path=str(STATE_FILE))
        # 返回标准化后的院校信息。
        return self.normalize_school_info(school_id, page_data)

    # 保存当前已抓取的数据，方便中途失败也能保留进度。
    def save_outputs(self) -> None:
        # 将原始列表/详情页 JSON 写入文件。
        RAW_PAGES_FILE.write_text(json.dumps(self.raw_pages, ensure_ascii=False, indent=2), encoding="utf-8")
        # 将合并后的院校库信息写入文件。
        COLLEGES_FILE.write_text(json.dumps(self.colleges, ensure_ascii=False, indent=2), encoding="utf-8")
        # 将网络调试日志写入文件。
        NETWORK_LOG_FILE.write_text(json.dumps(self.network_logs, ensure_ascii=False, indent=2), encoding="utf-8")
        # 输出保存日志。
        logger.info("已保存输出：raw_pages=%s colleges=%s network_log=%s", RAW_PAGES_FILE, COLLEGES_FILE, NETWORK_LOG_FILE)

    # 对详情页院校信息做轻量标准化，保留 raw 便于追溯。
    def normalize_school_info(self, school_id: str, page_data: dict[str, Any]) -> dict[str, Any]:
        # 提取字段字典。
        fields = page_data.get("fields") or {}
        # 优先用页面标题/标题节点推断学校名称。
        name = page_data.get("heading") or fields.get("院校名称") or fields.get("学校名称") or ""
        # 如果标题中带有“阳光高考”等后缀，则做轻量清理。
        if not name and page_data.get("title"):
            name = str(page_data.get("title", "")).split("_")[0].split("-")[0].strip()
        # 返回统一字段结构。
        return {
            "school_id": school_id,
            "name": name,
            "source": "chsi_gaokao_schinfomain",
            "source_url": SCHOOL_INFO_URL.format(school_id=school_id),
            "final_url": page_data.get("url"),
            "title": page_data.get("title"),
            "fields": fields,
            "text": page_data.get("text"),
            "raw": page_data,
        }

    # 主抓取流程。
    async def run(self) -> None:
        # 输出启动配置日志。
        logger.info(
            "启动配置：headless=%s start=%s max_schools=%s list_scroll_rounds=%s interval=%s",
            self.headless,
            self.start,
            self.max_schools,
            self.list_scroll_rounds,
            self.request_interval_seconds,
        )
        # 启动 Playwright。
        async with async_playwright() as p:
            # 启动 Chromium；首次建议 headless=False。
            browser = await p.chromium.launch(headless=self.headless)
            # 如果存在 storage_state，则复用 Cookie；否则创建全新上下文。
            if STATE_FILE.exists():
                logger.info("发现已有 storage_state，将复用：%s", STATE_FILE)
                context = await browser.new_context(storage_state=str(STATE_FILE), locale="zh-CN")
            else:
                logger.info("未发现 storage_state，将创建新上下文")
                context = await browser.new_context(locale="zh-CN")
            # 创建一个页面，列表页和详情页都在同一页面/上下文中完成。
            page = await context.new_page()
            try:
                # 从 schlist 页面提取学校 ID。
                school_ids = await self.collect_school_ids(page, context)
                # 如果没有提取到学校 ID，直接终止并提示。
                if not school_ids:
                    raise RuntimeError("未能从 schlist 页面提取到学校 ID")
                # 去重集合，避免重复写入同一 school_id。
                seen_school_ids: set[str] = set()
                # 遍历学校 ID，逐个访问 schinfomain 详情页。
                for index, school_id in enumerate(school_ids, start=1):
                    # 如果 ID 已处理则跳过。
                    if school_id in seen_school_ids:
                        continue
                    # 当前学校最多重试 2 次：失败后重新访问列表页刷新挑战状态再试。
                    last_error: Exception | None = None
                    for attempt in range(1, 3):
                        try:
                            logger.info("抓取院校库详情：index=%s/%s school_id=%s attempt=%s", index, len(school_ids), school_id, attempt)
                            # 每次详情页请求前都先访问列表页，刷新 Cookie / 挑战状态。
                            await self.warmup(page, context)
                            # 请求并解析当前学校详情页。
                            data = await self.fetch_school_info(page, context, school_id)
                            # 请求成功则跳出重试循环。
                            break
                        except Exception as exc:
                            # 记录最后一次异常。
                            last_error = exc
                            # 输出异常日志。
                            logger.exception("院校库详情抓取失败：school_id=%s attempt=%s error=%s", school_id, attempt, exc)
                    else:
                        # 如果两次都失败，则抛出最后异常并终止。
                        raise RuntimeError(f"school_id={school_id} 多次重试失败") from last_error
                    # 标记 school_id 已处理。
                    seen_school_ids.add(school_id)
                    # 保存标准化后的院校库信息。
                    self.colleges.append(data)
                    # 保存详情页原始数据摘要。
                    self.raw_pages.append(
                        {
                            "source": "chsi_gaokao_schinfomain",
                            "school_id": school_id,
                            "source_url": SCHOOL_INFO_URL.format(school_id=school_id),
                            "final_url": data.get("final_url"),
                            "title": data.get("title"),
                            "fields": data.get("fields"),
                            "text": data.get("text"),
                            "recorded_at": datetime.now().isoformat(timespec="seconds"),
                        }
                    )
                    # 每个学校保存一次，避免中途失败丢失已抓结果。
                    self.save_outputs()
                    # 请求间隔，降低触发风控概率。
                    await asyncio.sleep(self.request_interval_seconds)
                # 抓取完成后再次保存 storage_state。
                await context.storage_state(path=str(STATE_FILE))
                # 输出最终统计。
                logger.info("抓取完成：school_ids=%s colleges=%s", len(school_ids), len(self.colleges))
            finally:
                # 关闭浏览器。
                await browser.close()


# 程序入口函数。
async def main() -> None:
    # 创建爬虫实例。
    crawler = ChsiSchoolCrawler()
    # 执行爬虫。
    await crawler.run()


# 如果当前文件被直接运行，则执行 main。
if __name__ == "__main__":
    # 使用 asyncio.run 启动异步程序。
    asyncio.run(main())
