from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

from openai import OpenAI

from .storage import connect
from .syllabus import get_syllabi, get_syllabus_profile


MAX_SYLLABUS_PROFILE_CHARS = 8000
MAX_SYLLABUS_RAW_CHARS = 12000
MAX_JOB_DESC_CHARS = 2200


def match_syllabi_to_jobs(
    db_path: Path,
    syllabus_ids: list[int],
    limit: int = 10,
    candidate_limit: int = 60,
    batch_size: int = 12,
) -> dict[str, Any]:
    syllabi = get_syllabi(db_path, syllabus_ids)
    if not syllabi:
        raise ValueError(f"No syllabi found for ids: {syllabus_ids}")

    jobs = load_candidate_jobs(db_path, candidate_limit=candidate_limit)
    if not jobs:
        raise ValueError("No jobs found in job pool. Crawl jobs first.")

    client = build_client()
    all_matches: list[dict[str, Any]] = []
    for batch in chunked(jobs, batch_size):
        result = call_match_model(client, db_path, syllabi, batch, limit=limit)
        all_matches.extend(result.get("matches", []))

    merged = dedupe_matches(all_matches)[:limit]
    final_result = {
        "syllabus_ids": syllabus_ids,
        "matches": merged,
        "note": "结果不包含匹配分数；按模型判断的相关性和解释完整度排序。",
    }
    run_id = save_match_run(db_path, syllabus_ids, [row["id"] for row in jobs], final_result)
    final_result["match_run_id"] = run_id
    return final_result


def build_client() -> OpenAI:
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is not configured. Set it in .env or the terminal environment.")
    base_url = os.getenv("OPENAI_BASE_URL") or None
    return OpenAI(api_key=api_key, base_url=base_url)


def load_candidate_jobs(db_path: Path, candidate_limit: int) -> list[sqlite3.Row]:
    limit_sql = "" if candidate_limit <= 0 else "LIMIT ?"
    params: tuple[int, ...] = () if candidate_limit <= 0 else (candidate_limit,)
    with connect(db_path) as conn:
        return conn.execute(
            f"""
            SELECT id, source, title, company, city, salary, education, experience,
                   industry, company_size, description, url
            FROM jobs
            WHERE coalesce(description, '') != ''
            ORDER BY id DESC
            {limit_sql}
            """,
            params,
        ).fetchall()


def call_match_model(
    client: OpenAI,
    db_path: Path,
    syllabi: list[sqlite3.Row],
    jobs: list[sqlite3.Row],
    limit: int,
) -> dict[str, Any]:
    model = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
    prompt = build_prompt(db_path, syllabi, jobs, limit=limit)
    response = client.chat.completions.create(
        model=model,
        temperature=0.2,
        messages=[
            {
                "role": "system",
                "content": (
                    "你是课程教学大纲与招聘岗位匹配智能体。"
                    "你必须依据课程大纲原文、课程画像和岗位描述原文进行判断。"
                    "不要输出匹配分数，不要编造课程内容或岗位要求。"
                    "只返回合法 JSON。"
                ),
            },
            {"role": "user", "content": prompt},
        ],
    )
    raw = response.choices[0].message.content or ""
    return parse_json_response(raw)


def build_prompt(db_path: Path, syllabi: list[sqlite3.Row], jobs: list[sqlite3.Row], limit: int) -> str:
    syllabus_blocks = []
    for row in syllabi:
        profile_block = ""
        profile = get_syllabus_profile(db_path, int(row["id"]))
        if profile:
            profile_json = str(profile["profile_json"])[:MAX_SYLLABUS_PROFILE_CHARS]
            profile_block = f"已分析课程画像 JSON：\n{profile_json}\n\n"
        text = str(row["raw_text"])[:MAX_SYLLABUS_RAW_CHARS]
        syllabus_blocks.append(
            f"【大纲ID {row['id']}】{row['title']}\n"
            f"文件：{row['file_name']}\n"
            f"{profile_block}"
            f"大纲原文：\n{text}"
        )

    job_blocks = []
    for row in jobs:
        desc = str(row["description"] or "")[:MAX_JOB_DESC_CHARS]
        job_blocks.append(
            f"【岗位ID {row['id']}】{row['title']}\n"
            f"公司：{row['company']}\n"
            f"城市：{row['city']}\n"
            f"薪资：{row['salary']}\n"
            f"经验：{row['experience']}\n"
            f"学历：{row['education']}\n"
            f"行业：{row['industry']}\n"
            f"岗位描述：\n{desc}"
        )

    return (
        "请从候选岗位中找出与所选课程大纲最相关的岗位。不要给匹配分数。\n"
        "必须明确标出：匹配到的课程能力或内容、匹配到的岗位要求、匹配原因。\n"
        "如果岗位不相关，不要输出。\n\n"
        f"最多输出 {limit} 个匹配岗位。\n\n"
        "返回 JSON 格式：\n"
        "{\n"
        '  "matches": [\n'
        "    {\n"
        '      "job_id": 123,\n'
        '      "job_title": "岗位名称",\n'
        '      "company": "公司名称",\n'
        '      "matched_course_content": ["引用或概括大纲中的相关课程能力/内容"],\n'
        '      "matched_job_requirements": ["引用或概括岗位描述中的相关要求"],\n'
        '      "explanation": "为什么这些课程能力能支撑该岗位"\n'
        "    }\n"
        "  ]\n"
        "}\n\n"
        "课程大纲：\n"
        + "\n\n".join(syllabus_blocks)
        + "\n\n候选岗位：\n"
        + "\n\n".join(job_blocks)
    )


def parse_json_response(raw: str) -> dict[str, Any]:
    cleaned = raw.strip()
    if cleaned.startswith("```json"):
        cleaned = cleaned.removeprefix("```json").strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.removeprefix("```").strip()
    if cleaned.endswith("```"):
        cleaned = cleaned[: -3].strip()
    return json.loads(cleaned)


def dedupe_matches(matches: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[int] = set()
    result: list[dict[str, Any]] = []
    for item in matches:
        try:
            job_id = int(item.get("job_id"))
        except Exception:
            continue
        if job_id in seen:
            continue
        seen.add(job_id)
        result.append(item)
    return result


def save_match_run(
    db_path: Path,
    syllabus_ids: list[int],
    candidate_job_ids: list[int],
    result: dict[str, Any],
) -> int:
    now = datetime.now().isoformat(timespec="seconds")
    with connect(db_path) as conn:
        cur = conn.execute(
            """
            INSERT INTO match_runs (syllabus_ids_json, candidate_job_ids_json, result_json, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (
                json.dumps(syllabus_ids, ensure_ascii=False),
                json.dumps(candidate_job_ids, ensure_ascii=False),
                json.dumps(result, ensure_ascii=False),
                now,
            ),
        )
        conn.commit()
        return int(cur.lastrowid)


def list_match_runs(db_path: Path, limit: int = 20) -> list[sqlite3.Row]:
    with connect(db_path) as conn:
        return conn.execute(
            """
            SELECT id, syllabus_ids_json, result_json, created_at
            FROM match_runs
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()


def get_match_run(db_path: Path, run_id: int) -> sqlite3.Row | None:
    with connect(db_path) as conn:
        return conn.execute("SELECT * FROM match_runs WHERE id = ?", (run_id,)).fetchone()


def chunked(items: list[sqlite3.Row], size: int) -> list[list[sqlite3.Row]]:
    return [items[index : index + size] for index in range(0, len(items), size)]
