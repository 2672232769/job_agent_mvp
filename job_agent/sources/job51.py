from __future__ import annotations

import json
import os
import time
from pathlib import Path
from urllib.parse import quote

from job_agent.auth import load_storage_state
from job_agent.models import JobPost, SearchIntent
from job_agent.sources.base import JobSource


CITY_CODES = {
    "全国": "000000",
    "北京": "010000",
    "上海": "020000",
    "广州": "030200",
    "深圳": "040000",
    "杭州": "080200",
    "成都": "090200",
    "南京": "070200",
    "武汉": "180200",
    "苏州": "070300",
    "西安": "200200",
    "长沙": "190200",
    "重庆": "060000",
    "天津": "050000",
}

SALARY_CODES = {
    "8-10K": "06",
    "10-15K": "07",
    "15-20K": "08",
    "20-30K": "09",
    "30-40K": "10",
}


class Job51Source(JobSource):
    name = "51job"

    def __init__(
        self,
        root: Path,
        headless: bool = True,
        cookies_file: str | None = None,
        delay_seconds: float = 2.0,
    ):
        self.root = root
        self.headless = headless
        self.cookies_file = cookies_file or ""
        self.delay_seconds = delay_seconds
        self.warnings: list[str] = []
        self.detail_verification_detected = False
        self.manual_verification_timeout_seconds = int(os.getenv("JOB51_MANUAL_VERIFY_TIMEOUT_SECONDS", "30"))

    def search(self, intent: SearchIntent) -> list[JobPost]:
        from playwright.sync_api import Error as PlaywrightError
        from playwright.sync_api import sync_playwright

        self.warnings = []
        self.detail_verification_detected = False
        all_jobs: list[JobPost] = []
        with sync_playwright() as p:
            try:
                browser = p.chromium.launch(headless=self.headless)
            except PlaywrightError as exc:
                if "Executable doesn't exist" in str(exc) or "playwright install" in str(exc):
                    raise RuntimeError(
                        "Playwright Chromium 浏览器未安装，无法启动招聘平台抓取。"
                        "本地请运行：.\\.venv\\Scripts\\python.exe -m playwright install chromium；"
                        "云端部署请确认构建阶段执行 python -m playwright install chromium。"
                    ) from exc
                raise
            state_path = load_storage_state(self.root, "51job")
            context = browser.new_context(
                viewport={"width": 1365, "height": 900},
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36"
                ),
                storage_state=state_path,
            )
            self._load_cookies(context)
            page = context.new_page()
            try:
                for keyword in intent.keywords:
                    for page_no in range(1, intent.pages + 1):
                        jobs = self._search_one_page(page, keyword, intent, page_no)
                        all_jobs.extend(jobs)
                        time.sleep(self.delay_seconds)
            finally:
                browser.close()
        return all_jobs

    def _load_cookies(self, context) -> None:
        if not self.cookies_file:
            return
        path = os.path.abspath(self.cookies_file)
        if not os.path.exists(path):
            return
        with open(path, "r", encoding="utf-8") as f:
            cookies = json.load(f)
        if cookies:
            context.add_cookies(cookies)

    def _search_one_page(self, page, keyword: str, intent: SearchIntent, page_no: int) -> list[JobPost]:
        captured: list[dict] = []
        search_urls: list[str] = []

        def on_response(response):
            if "api/job/search" not in response.url or response.status != 200:
                return
            search_urls.append(response.url)
            try:
                text = response.text()
                if not text.lstrip().startswith("{"):
                    return
                captured.append(json.loads(text))
            except Exception:
                return

        page.on("response", on_response)
        try:
            city_code = CITY_CODES.get(intent.city or "全国", "000000")
            salary_code = SALARY_CODES.get(intent.salary.upper(), "")
            url = (
                "https://we.51job.com/pc/search"
                f"?keyword={quote(keyword)}&jobArea={city_code}&salary={salary_code}"
            )
            page.goto(url, wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(12000)
            self._handle_search_verification_if_needed(page)
            for _ in range(1, page_no):
                page.evaluate(
                    """
                    () => {
                        const btn = document.querySelector('button.btn-next, li.next, .el-pagination button:last-child');
                        if (btn && !btn.disabled && !String(btn.className).includes('disabled')) btn.click();
                    }
                    """
                )
                page.wait_for_timeout(12000)
                self._handle_search_verification_if_needed(page)

            if not captured:
                self._handle_search_verification_if_needed(page)
                if not captured and not search_urls:
                    page.reload(wait_until="domcontentloaded", timeout=45000)
                    page.wait_for_timeout(12000)
                    self._handle_search_verification_if_needed(page)
                self._fetch_search_api_from_page(page, search_urls, captured)
            if not captured:
                return []
            items = captured[-1].get("resultbody", {}).get("job", {}).get("items", [])
            return [self._parse_item(item) for item in items]
        finally:
            page.remove_listener("response", on_response)

    def _fetch_search_api_from_page(self, page, search_urls: list[str], captured: list[dict]) -> None:
        for url in reversed(search_urls[-3:]):
            try:
                text = page.evaluate(
                    """async (url) => {
                        const resp = await fetch(url, { credentials: 'include' });
                        return await resp.text();
                    }""",
                    url,
                )
                if isinstance(text, str) and text.lstrip().startswith("{"):
                    captured.append(json.loads(text))
                    return
            except Exception:
                continue

    def _parse_item(self, item: dict) -> JobPost:
        tags = item.get("jobTags") or []
        if not isinstance(tags, list):
            tags = [str(tags)]
        return JobPost(
            source=self.name,
            title=item.get("jobName") or "",
            company=item.get("companyName") or "",
            city=item.get("jobAreaString") or "",
            salary=item.get("provideSalaryString") or "",
            description=item.get("jobDescribe") or "",
            url=item.get("jobHref") or "",
            address=item.get("jobAreaString") or "",
            education=item.get("degreeString") or "",
            experience=item.get("workYearString") or "",
            industry=item.get("companyIndustryType1Str") or "",
            company_size=item.get("companySizeString") or "",
            tags=[str(tag) for tag in tags],
            publish_date=(item.get("issueDateString") or "").split(" ")[0],
            raw=item,
        )

    def _handle_search_verification_if_needed(self, page) -> None:
        body = self._safe_body_text(page)
        if not self._is_verification_page(body, page.url):
            return

        if self.headless:
            raise RuntimeError(
                "51job 搜索页出现访问验证/滑动验证。后台抓取无法人工处理，请取消勾选“后台抓取”后重试。"
            )

        timeout = max(5, self.manual_verification_timeout_seconds)
        self._add_warning(
            f"51job 搜索页出现访问验证，已暂停 {timeout} 秒。请在打开的浏览器窗口中完成验证，验证通过后系统会继续抓取。"
        )
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            page.wait_for_timeout(2000)
            body = self._safe_body_text(page)
            if not self._is_verification_page(body, page.url):
                self._add_warning("51job 访问验证已通过，继续抓取。")
                page.wait_for_timeout(5000)
                return
        raise RuntimeError(
            f"51job 访问验证在 {timeout} 秒内未完成，已停止本次抓取。请完成验证后重新点击抓取。"
        )

    def _safe_body_text(self, page) -> str:
        try:
            return page.locator("body").inner_text(timeout=5000).strip()
        except Exception:
            return ""

    def _is_verification_page(self, body: str, url: str = "") -> bool:
        verification_markers = [
            "访问验证",
            "滑动验证",
            "请进行验证",
            "通过后即可继续访问网页",
            "为了更好的访问体验",
        ]
        text = body or ""
        return (
            any(marker in text for marker in verification_markers)
            or "TraceID" in text
            or "verify" in (url or "").lower()
        )

    def _add_warning(self, message: str) -> None:
        if message not in self.warnings:
            self.warnings.append(message)
