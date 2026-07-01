from __future__ import annotations

import time
from pathlib import Path
from urllib.parse import quote

from job_agent.auth import load_storage_state
from job_agent.models import JobPost, SearchIntent
from job_agent.sources.base import JobSource


CITY_CODES = {
    "全国": "410",
    "北京": "010",
    "上海": "020",
    "广州": "050020",
    "深圳": "050090",
    "杭州": "070020",
    "成都": "280020",
    "武汉": "170020",
    "南京": "060020",
    "苏州": "060080",
}

SALARY_CODES = {
    "8-10K": "1",
    "10-15K": "2",
    "15-20K": "3",
    "20-30K": "4",
    "30-50K": "5",
    "50K以上": "6",
}


class LiepinSource(JobSource):
    name = "liepin"

    def __init__(self, root: Path, headless: bool = True, delay_seconds: float = 3.0):
        self.root = root
        self.headless = headless
        self.delay_seconds = delay_seconds

    def search(self, intent: SearchIntent) -> list[JobPost]:
        from playwright.sync_api import sync_playwright

        jobs: list[JobPost] = []
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=self.headless)
            context = browser.new_context(
                viewport={"width": 1365, "height": 900},
                storage_state=load_storage_state(self.root, "liepin"),
            )
            page = context.new_page()
            try:
                for keyword in intent.keywords:
                    for page_no in range(1, intent.pages + 1):
                        jobs.extend(self._search_one_page(page, keyword, intent, page_no))
                        time.sleep(self.delay_seconds)
            finally:
                browser.close()
        return jobs

    def _search_one_page(self, page, keyword: str, intent: SearchIntent, page_no: int) -> list[JobPost]:
        captured: list[dict] = []

        def on_response(response):
            if "pc-search-job" not in response.url or response.status != 200:
                return
            try:
                data = response.json()
            except Exception:
                return
            if data.get("data", {}).get("data", {}).get("jobCardList"):
                captured.append(data)

        page.on("response", on_response)
        try:
            city_code = CITY_CODES.get(intent.city or "全国", "410")
            salary_code = SALARY_CODES.get(intent.salary.upper(), "")
            page_index = max(0, page_no - 1)
            url = f"https://www.liepin.com/zhaopin/?key={quote(keyword)}&dq={city_code}&currentPage={page_index}"
            if salary_code:
                url += f"&salaryCode={salary_code}"
            page.goto(url, wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(4000)
            if not captured:
                return []
            items = captured[-1].get("data", {}).get("data", {}).get("jobCardList", [])
            return [self._parse_item(item) for item in items]
        finally:
            page.remove_listener("response", on_response)

    def _parse_item(self, item: dict) -> JobPost:
        job = item.get("job", {})
        company = item.get("comp", {})
        recruiter = item.get("recruiter", {})
        labels = job.get("labels") or []
        if not isinstance(labels, list):
            labels = [str(labels)]
        raw_time = str(job.get("refreshTime") or "")
        publish_date = f"{raw_time[:4]}-{raw_time[4:6]}-{raw_time[6:8]}" if len(raw_time) >= 8 else ""
        return JobPost(
            source=self.name,
            title=job.get("title") or "",
            company=company.get("compName") or "",
            city=job.get("dq") or "",
            salary=job.get("salary") or "",
            description=job.get("description") or "",
            url=job.get("link") or "",
            address=job.get("dq") or "",
            education=job.get("requireEduLevel") or "",
            experience=job.get("requireWorkYears") or "",
            industry=company.get("compIndustry") or "",
            company_size=company.get("compScale") or "",
            tags=[str(label) for label in labels],
            publish_date=publish_date,
            raw={"job": job, "comp": company, "recruiter": recruiter},
        )

