from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass(slots=True)
class SearchIntent:
    raw_text: str
    keywords: list[str]
    city: str = ""
    salary: str = ""
    pages: int = 1

    @property
    def primary_keyword(self) -> str:
        return " ".join(self.keywords).strip()


@dataclass(slots=True)
class JobPost:
    source: str
    title: str
    company: str
    city: str
    salary: str = ""
    description: str = ""
    url: str = ""
    address: str = ""
    education: str = ""
    experience: str = ""
    industry: str = ""
    company_size: str = ""
    tags: list[str] = field(default_factory=list)
    publish_date: str = ""
    crawled_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))
    raw: dict[str, Any] = field(default_factory=dict)

