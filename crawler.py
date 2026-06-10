"""
阳光高考院校库最小 Playwright 抓取器。

目标：
1. 先访问 https://gaokao.chsi.com.cn/wap/sch/schlist 让浏览器自动执行阿里云 JS 挑战。
2. 在同一个 BrowserContext 内通过页面 fetch 请求 /wap/sch/schsearch?start=xxx。
3. 自动保存 Cookie / storage_state，后续启动复用。
4. 输出全量院校列表 JSON、原始分页 JSON、网络日志 JSON、运行日志。

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
# 原始分页响应输出文件。
RAW_PAGES_FILE = OUTPUT_DIR / "chsi_school_pages_raw.json"
# 合并后的院校列表输出文件。
COLLEGES_FILE = OUTPUT_DIR / "chsi_colleges.json"
# 网络请求调试日志输出文件。
NETWORK_LOG_FILE = OUTPUT_DIR / "chsi_network_log.json"
# 入口页：用于预热 Cookie 与执行阿里云 JS 挑战。
WARMUP_URL = "https://gaokao.chsi.com.cn/wap/sch/schlist"
# 数据接口：通过 start 参数分页获取院校 JSON。
SEARCH_URL = "https://gaokao.chsi.com.cn/wap/sch/schsearch?start={start}"


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
        # 起始 start 参数；默认从第一页 start=0 开始。
        self.start = env_int("START", 0)
        # 最大页数；0 表示不限制。
        self.max_pages = env_int("MAX_PAGES", 0)
        # 每页请求之间的间隔秒数。
        self.request_interval_seconds = env_float("REQUEST_INTERVAL_SECONDS", 1.0)
        # 用于保存每一页原始 JSON。
        self.raw_pages: list[dict[str, Any]] = []
        # 用于保存合并后的院校记录。
        self.colleges: list[dict[str, Any]] = []
        # 用于保存每次网络请求/响应的调试日志。
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
    async def log_cookie_summary(self, context) -> None:
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

    # 访问入口页，等待 JS 挑战自动完成并写入 Cookie。
    async def warmup(self, page: Page, context) -> None:
        # 输出预热开始日志。
        logger.info("开始预热入口页：%s", WARMUP_URL)
        # 访问入口页，等待 DOMContentLoaded 即可，不强求 networkidle，避免某些统计请求长时间挂起。
        response = await page.goto(WARMUP_URL, wait_until="domcontentloaded", timeout=60_000)
        # 如果拿到响应对象，则输出状态码。
        if response:
            logger.info("入口页响应：status=%s url=%s", response.status, response.url)
        # 尝试等待网络空闲，让挑战脚本、统计脚本、Cookie 写入有时间完成。
        try:
            await page.wait_for_load_state("networkidle", timeout=30_000)
            logger.info("入口页 networkidle 完成")
        except PlaywrightTimeoutError:
            logger.warning("入口页等待 networkidle 超时，继续后续流程")
        # 额外等待 2 秒，给阿里云 JS 挑战写 Cookie 的时间。
        await page.wait_for_timeout(2_000)
        # 打印 Cookie 概况，用于判断挑战是否完成。
        await self.log_cookie_summary(context)
        # 保存当前 storage_state，后续启动可复用 Cookie。
        await context.storage_state(path=str(STATE_FILE))
        # 输出保存状态日志。
        logger.info("已保存 storage_state：%s", STATE_FILE)

    # 使用页面内 fetch 请求 JSON 接口。
    async def fetch_json(self, page: Page, start: int) -> dict[str, Any]:
        # 拼接当前页接口地址。
        url = SEARCH_URL.format(start=start)
        # 输出请求开始日志。
        logger.info("开始请求院校接口：start=%s url=%s", start, url)
        # 记录开始时间，用于计算耗时。
        started = time.time()
        # 在浏览器页面上下文中执行 fetch。
        # credentials: 'include' 表示携带当前页面同域 Cookie。
        result = await page.evaluate(
            """
            async ({url}) => {
                const startedAt = new Date().toISOString();
                const resp = await fetch(url, {
                    method: 'GET',
                    credentials: 'include',
                    headers: {
                        'Accept': 'application/json, text/plain, */*'
                    }
                });
                const text = await resp.text();
                const headers = {};
                for (const [key, value] of resp.headers.entries()) {
                    headers[key] = value;
                }
                return {
                    url: resp.url,
                    status: resp.status,
                    statusText: resp.statusText,
                    ok: resp.ok,
                    headers,
                    bodyText: text,
                    startedAt,
                    receivedAt: new Date().toISOString()
                };
            }
            """,
            {"url": url},
        )
        # 计算耗时。
        elapsed = round(time.time() - started, 3)
        # 组织网络日志记录。
        network_record = {
            "request": {
                "url": url,
                "method": "GET",
                "mode": "page_fetch",
                "start": start,
                "initiator_url": page.url,
            },
            "response": result,
            "elapsed_seconds": elapsed,
            "recorded_at": datetime.now().isoformat(timespec="seconds"),
        }
        # 将网络记录加入内存列表。
        self.network_logs.append(network_record)
        # 输出接口状态日志。
        logger.info("接口响应：start=%s status=%s ok=%s elapsed=%ss", start, result.get("status"), result.get("ok"), elapsed)
        # 取出响应文本。
        body_text = result.get("bodyText") or ""
        # 如果疑似挑战页，则抛出异常，让上层 warmup 后重试。
        if self.is_challenge_text(body_text):
            logger.warning("接口响应疑似挑战页：start=%s preview=%s", start, body_text[:300])
            raise RuntimeError(f"接口响应疑似阿里云/风控挑战页，start={start}")
        # 尝试解析 JSON。
        try:
            data = json.loads(body_text)
        except json.JSONDecodeError as exc:
            # JSON 解析失败时，打印正文前 500 字便于定位。
            logger.error("JSON 解析失败：start=%s error=%s body_preview=%s", start, exc, body_text[:500])
            # 继续抛出异常，由上层决定是否重试。
            raise
        # 如果接口业务 flag 不为 true，也视为异常并记录。
        if data.get("flag") is not True:
            logger.warning("接口 flag 非 true：start=%s data_preview=%s", start, str(data)[:500])
        # 返回解析后的 JSON。
        return data

    # 保存当前已抓取的数据，方便中途失败也能保留进度。
    def save_outputs(self) -> None:
        # 将原始分页 JSON 写入文件。
        RAW_PAGES_FILE.write_text(json.dumps(self.raw_pages, ensure_ascii=False, indent=2), encoding="utf-8")
        # 将合并后的院校列表写入文件。
        COLLEGES_FILE.write_text(json.dumps(self.colleges, ensure_ascii=False, indent=2), encoding="utf-8")
        # 将网络调试日志写入文件。
        NETWORK_LOG_FILE.write_text(json.dumps(self.network_logs, ensure_ascii=False, indent=2), encoding="utf-8")
        # 输出保存日志。
        logger.info("已保存输出：raw_pages=%s colleges=%s network_log=%s", RAW_PAGES_FILE, COLLEGES_FILE, NETWORK_LOG_FILE)

    # 对院校记录做轻量标准化，保留 raw 便于追溯。
    def normalize_school(self, item: dict[str, Any]) -> dict[str, Any]:
        # 返回统一字段结构。
        return {
            "sch_id": item.get("schId"),
            "sch_info_id": item.get("schInfoId"),
            "school_code": item.get("yxdm"),
            "name": item.get("yxmc"),
            "province": item.get("ssmc"),
            "authority": item.get("zgbmmc"),
            "school_type": item.get("yxlxmc"),
            "education_level": item.get("xlcc"),
            "is_double_first_class_university": item.get("yldx"),
            "is_double_first_class_subject": item.get("ylxk"),
            "has_master_degree": item.get("syl"),
            "has_doctor_degree": item.get("yjsy"),
            "satisfaction_score": item.get("avgRank"),
            "source": "chsi_gaokao_schsearch",
            "source_url": SEARCH_URL.format(start="{start}"),
            "raw": item,
        }

    # 主抓取流程。
    async def run(self) -> None:
        # 输出启动配置日志。
        logger.info("启动配置：headless=%s start=%s max_pages=%s interval=%s", self.headless, self.start, self.max_pages, self.request_interval_seconds)
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
            # 创建一个页面，后续 warmup 和 fetch 都在同一页面/上下文中完成。
            page = await context.new_page()
            # 先预热入口页，确保 Cookie 可用。
            await self.warmup(page, context)
            # 当前分页 start。
            current_start = self.start
            # 已抓取页数。
            page_index = 0
            # 去重集合，避免重复写入同一 schId。
            seen_school_ids: set[str] = set()
            # 开始分页循环。
            while True:
                # 如果设置了最大页数，并且已达到，则停止。
                if self.max_pages > 0 and page_index >= self.max_pages:
                    logger.info("达到 MAX_PAGES=%s，停止抓取", self.max_pages)
                    break
                # 当前页最多重试 2 次：首次失败后 warmup 再试一次。
                last_error: Exception | None = None
                # 循环重试。
                for attempt in range(1, 3):
                    try:
                        logger.info("抓取分页：page_index=%s start=%s attempt=%s", page_index + 1, current_start, attempt)
                        # 请求当前页 JSON。
                        data = await self.fetch_json(page, current_start)
                        # 请求成功则跳出重试循环。
                        break
                    except Exception as exc:
                        # 记录最后一次异常。
                        last_error = exc
                        # 输出异常日志。
                        logger.exception("抓取失败：start=%s attempt=%s error=%s", current_start, attempt, exc)
                        # 失败后重新 warmup，刷新 Cookie / 挑战状态。
                        await self.warmup(page, context)
                else:
                    # 如果两次都失败，则抛出最后异常并终止。
                    raise RuntimeError(f"start={current_start} 多次重试失败") from last_error
                # 取业务 msg。
                msg = data.get("msg") or {}
                # 取当前页院校列表。
                items = msg.get("list") or []
                # 输出当前页条数。
                logger.info("分页解析成功：start=%s count=%s totalCount=%s totalPage=%s", current_start, len(items), msg.get("totalCount"), msg.get("totalPage"))
                # 保存原始页数据。
                self.raw_pages.append({"start": current_start, "data": data})
                # 遍历院校记录。
                for item in items:
                    # 取 schId 作为去重键。
                    sch_id = str(item.get("schId") or "")
                    # 如果 schId 已存在则跳过。
                    if sch_id and sch_id in seen_school_ids:
                        continue
                    # 标记 schId 已见。
                    if sch_id:
                        seen_school_ids.add(sch_id)
                    # 写入标准化后的院校记录。
                    self.colleges.append(self.normalize_school(item))
                # 每页保存一次，避免中途失败丢失已抓结果。
                self.save_outputs()
                # 如果没有下一页，则结束循环。
                if not msg.get("nextPageAvailable"):
                    logger.info("接口提示没有下一页，抓取完成")
                    break
                # 读取下一页 start。
                next_start = msg.get("startOfNextPage")
                # 如果下一页 start 不存在，则结束，避免死循环。
                if next_start is None:
                    logger.warning("nextPageAvailable=true 但 startOfNextPage 为空，停止")
                    break
                # 更新当前 start。
                current_start = int(next_start)
                # 页数 +1。
                page_index += 1
                # 请求间隔，降低触发风控概率。
                await asyncio.sleep(self.request_interval_seconds)
            # 抓取完成后再次保存 storage_state。
            await context.storage_state(path=str(STATE_FILE))
            # 输出最终统计。
            logger.info("抓取完成：raw_pages=%s colleges=%s", len(self.raw_pages), len(self.colleges))
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
