from __future__ import annotations

from abc import ABC, abstractmethod

from job_agent.models import JobPost, SearchIntent


class JobSource(ABC):
    name: str

    @abstractmethod
    def search(self, intent: SearchIntent) -> list[JobPost]:
        raise NotImplementedError

