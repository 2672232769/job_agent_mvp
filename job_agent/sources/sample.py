from __future__ import annotations

from job_agent.models import JobPost, SearchIntent
from job_agent.sources.base import JobSource


class SampleSource(JobSource):
    name = "sample"

    def search(self, intent: SearchIntent) -> list[JobPost]:
        jobs: list[JobPost] = []
        for keyword in intent.keywords:
            jobs.append(
                JobPost(
                    source=self.name,
                    title=f"{keyword} 实习生",
                    company="示例科技有限公司",
                    city=intent.city or "广州",
                    salary=intent.salary or "面议",
                    description=f"面向 {intent.raw_text} 的示例岗位，用于验证本地入库流程。",
                    url=f"https://example.com/jobs/{keyword}",
                    tags=["校招", "可转正"],
                    raw={"keyword": keyword, "intent": intent.raw_text},
                )
            )
        return jobs

