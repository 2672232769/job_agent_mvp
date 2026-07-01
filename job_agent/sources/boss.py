from __future__ import annotations

import os
import random
import time
from pathlib import Path
from typing import Any

import httpx

from job_agent.auth import load_cookie_header_from_state
from job_agent.models import JobPost, SearchIntent
from job_agent.sources.base import JobSource


CITY_CODES = {
    "全国": "100010000",
    "北京": "101010100",
    "上海": "101020100",
    "广州": "101280100",
    "深圳": "101280600",
    "杭州": "101210100",
    "成都": "101270100",
    "南京": "101190100",
    "武汉": "101200100",
    "西安": "101110100",
    "苏州": "101190400",
    "长沙": "101250100",
    "重庆": "101040100",
}

SALARY_CODES = {
    "3-5K": "402",
    "5-10K": "403",
    "10-15K": "404",
    "15-20K": "405",
    "20-30K": "406",
    "30-50K": "407",
    "50K以上": "408",
}


class BossSource(JobSource):
    name = "boss"

    def __init__(self, root: Path, delay_seconds: float = 3.0):
        self.root = root
        self.delay_seconds = delay_seconds

    def search(self, intent: SearchIntent) -> list[JobPost]:
        cookie_header = os.getenv("BOSS_COOKIES") or load_cookie_header_from_state(self.root, "boss")
        if not cookie_header:
            raise RuntimeError("BOSS login state not found. Run: python -m job_agent.app auth login boss")

        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0 Safari/537.36"
            ),
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Referer": "https://www.zhipin.com/web/geek/job",
            "Origin": "https://www.zhipin.com",
            "Cookie": cookie_header,
        }

        jobs: list[JobPost] = []
        with httpx.Client(base_url="https://www.zhipin.com", headers=headers, timeout=30.0) as client:
            for keyword in intent.keywords:
                for page in range(1, intent.pages + 1):
                    jobs.extend(self._search_page(client, keyword, intent, page))
                    time.sleep(self.delay_seconds + random.uniform(0.5, 2.0))
        return jobs

    def _search_page(self, client: httpx.Client, keyword: str, intent: SearchIntent, page: int) -> list[JobPost]:
        params: dict[str, Any] = {
            "query": keyword,
            "city": CITY_CODES.get(intent.city or "全国", "100010000"),
            "page": page,
            "pageSize": 15,
        }
        salary_code = SALARY_CODES.get(intent.salary.upper())
        if salary_code:
            params["salary"] = salary_code
        resp = client.get("/wapi/zpgeek/search/joblist.json", params=params)
        resp.raise_for_status()
        data = resp.json()
        if data.get("code") != 0:
            raise RuntimeError(f"BOSS search failed: {data.get('message') or data}")
        items = data.get("zpData", {}).get("jobList", [])
        result = []
        for item in items:
            detail = self._fetch_detail(client, item.get("securityId", ""))
            result.append(self._parse_item(item, detail))
            time.sleep(1.0 + random.uniform(0.2, 1.0))
        return result

    def _fetch_detail(self, client: httpx.Client, security_id: str) -> dict:
        if not security_id:
            return {}
        resp = client.get("/wapi/zpgeek/job/detail.json", params={"securityId": security_id})
        resp.raise_for_status()
        data = resp.json()
        if data.get("code") != 0:
            return {}
        return data.get("zpData", {})

    def _parse_item(self, item: dict, detail: dict) -> JobPost:
        job_info = detail.get("jobInfo", {})
        boss_info = detail.get("bossInfo", {})
        brand_info = detail.get("brandComInfo", {})
        skills = item.get("skills") or job_info.get("showSkills") or []
        if not isinstance(skills, list):
            skills = [str(skills)]
        encrypt_id = job_info.get("encryptId") or item.get("encryptId") or item.get("securityId", "")
        return JobPost(
            source=self.name,
            title=item.get("jobName") or job_info.get("jobName") or "",
            company=item.get("brandName") or brand_info.get("brandName") or "",
            city=item.get("cityName") or job_info.get("locationName") or "",
            salary=item.get("salaryDesc") or job_info.get("salaryDesc") or "",
            description=job_info.get("postDescription") or "",
            url=f"https://www.zhipin.com/job_detail/{encrypt_id}.html" if encrypt_id else "",
            address=job_info.get("address") or "",
            education=item.get("jobDegree") or job_info.get("degreeName") or "",
            experience=item.get("jobExperience") or job_info.get("experienceName") or "",
            industry=brand_info.get("industryName") or "",
            company_size=brand_info.get("scaleName") or "",
            tags=[str(skill) for skill in skills],
            publish_date="",
            raw={"list": item, "detail": detail, "boss": boss_info},
        )

