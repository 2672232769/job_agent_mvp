from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import Any

from openai import OpenAI

from .graph import ensure_course_graph, list_course_nodes
from .syllabus import get_syllabi, get_syllabus_profile


MAX_RAW_TEXT_CHARS = 5000


def build_course_job_search_plan(
    db_path: Path,
    syllabus_ids: list[int],
    city: str = "",
    max_keywords: int = 8,
) -> dict[str, Any]:
    syllabi = get_syllabi(db_path, syllabus_ids)
    if not syllabi:
        raise ValueError(f"No syllabi found for ids: {syllabus_ids}")

    client = build_client()
    for syllabus_id in syllabus_ids:
        ensure_course_graph(db_path, syllabus_id, client=client)

    course_nodes = list_course_nodes(db_path, syllabus_ids)
    context = build_course_context(db_path, syllabi, course_nodes)
    response = client.chat.completions.create(
        model=os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
        temperature=0.2,
        messages=[
            {
                "role": "system",
                "content": (
                    "You plan job search keywords from selected university course syllabi. "
                    "Prefer realistic Chinese recruitment search terms. Return valid JSON only."
                ),
            },
            {"role": "user", "content": build_plan_prompt(context, city, max_keywords)},
        ],
    )
    payload = parse_json_response(response.choices[0].message.content or "")
    return normalize_plan(payload, syllabus_ids, city, max_keywords)


def build_client() -> OpenAI:
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is not configured.")
    return OpenAI(api_key=api_key, base_url=os.getenv("OPENAI_BASE_URL") or None)


def build_course_context(db_path: Path, syllabi: list[sqlite3.Row], course_nodes: list[sqlite3.Row]) -> dict[str, Any]:
    nodes_by_syllabus: dict[int, list[dict[str, Any]]] = {}
    for node in course_nodes:
        nodes_by_syllabus.setdefault(int(node["syllabus_id"]), []).append(
            {
                "type": node["node_type"],
                "name": node["name"],
                "category": node["category"],
                "description": node["description"],
                "evidence": node["evidence_text"],
                "keywords": json_loads(node["keywords_json"], []),
            }
        )

    courses = []
    for row in syllabi:
        profile = get_syllabus_profile(db_path, int(row["id"]))
        profile_json = json_loads(profile["profile_json"], {}) if profile else {}
        courses.append(
            {
                "syllabus_id": int(row["id"]),
                "title": row["title"],
                "file_name": row["file_name"],
                "profile": {
                    "summary": profile_json.get("summary", ""),
                    "knowledge_points": profile_json.get("knowledge_points", []),
                    "abilities": profile_json.get("abilities", []),
                    "technologies_tools_methods": profile_json.get("technologies_tools_methods", []),
                    "job_directions": profile_json.get("job_directions", []),
                },
                "graph_nodes": nodes_by_syllabus.get(int(row["id"]), [])[:40],
                "raw_text_excerpt": str(row["raw_text"] or "")[:MAX_RAW_TEXT_CHARS],
            }
        )
    return {"courses": courses}


def build_plan_prompt(context: dict[str, Any], city: str, max_keywords: int) -> str:
    return f"""
Selected course context:
{json.dumps(context, ensure_ascii=False)}

Task:
Analyze the main technologies, methods, abilities, and job directions taught by these courses.
Then produce practical job-search keywords for Chinese recruitment platforms.

Rules:
- Return {max_keywords} or fewer keywords.
- Keywords should be search terms that actually find jobs, such as "软件测试工程师", "Python后端开发", "数据分析助理".
- Prefer entry-level, internship, assistant, junior, or campus-suitable roles when the courses look undergraduate.
- Avoid over-broad keywords like "工程师" or "开发".
- Avoid unrelated roles caused by weak keyword coincidence.
- If city is provided, consider that market but do not include city inside the keyword.
- Keep the answer in Chinese.

City preference: {city or "不限"}

Return JSON:
{{
  "keywords": ["岗位搜索关键词"],
  "course_signals": ["从课程中识别出的关键技术/能力"],
  "job_directions": ["可能对口的岗位方向"],
  "rationale": "为什么建议这些关键词"
}}
""".strip()


def normalize_plan(payload: dict[str, Any], syllabus_ids: list[int], city: str, max_keywords: int) -> dict[str, Any]:
    keywords = dedupe_texts(payload.get("keywords", []))[:max_keywords]
    return {
        "syllabus_ids": syllabus_ids,
        "city": city,
        "keywords": keywords,
        "course_signals": dedupe_texts(payload.get("course_signals", []))[:20],
        "job_directions": dedupe_texts(payload.get("job_directions", []))[:12],
        "rationale": str(payload.get("rationale") or "").strip(),
    }


def parse_json_response(raw: str) -> dict[str, Any]:
    cleaned = raw.strip()
    if cleaned.startswith("```json"):
        cleaned = cleaned.removeprefix("```json").strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.removeprefix("```").strip()
    if cleaned.endswith("```"):
        cleaned = cleaned[: -3].strip()
    return json.loads(cleaned)


def json_loads(value: str | None, fallback: Any) -> Any:
    if not value:
        return fallback
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return fallback


def dedupe_texts(value: Any) -> list[str]:
    if not isinstance(value, list):
        value = [value] if value else []
    seen: set[str] = set()
    result: list[str] = []
    for item in value:
        text = str(item or "").strip()
        if not text:
            continue
        key = text.lower()
        if key in seen:
            continue
        seen.add(key)
        result.append(text)
    return result
