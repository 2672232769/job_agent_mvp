from __future__ import annotations

import json
import os
import re
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

from openai import OpenAI

from .neo4j_graph import Neo4jGraphStore
from .storage import connect, get_job
from .syllabus import get_syllabi, get_syllabus


COURSE_EXTRACTOR_VERSION = "course_graph_v1"
JOB_EXTRACTOR_VERSION = "job_requirement_graph_v1"
MATCHER_VERSION = "graph_match_v1"

MAX_SYLLABUS_CHARS = 24000
MAX_JOB_DESC_CHARS = 5000
MAX_JOBS_PER_EXTRACT_CALL = 8
MAX_JOBS_FOR_JUDGE = 24

COURSE_NODE_TYPES = {"knowledge", "ability", "tool", "method", "job_direction"}
JOB_NODE_TYPES = {"responsibility", "skill", "tool", "experience", "education", "domain", "soft_skill"}
RELATION_TYPES = {"supports", "partially_supports", "related", "not_supported"}


def match_syllabi_to_jobs_graph(
    db_path: Path,
    syllabus_ids: list[int],
    limit: int = 8,
    candidate_limit: int = 60,
) -> dict[str, Any]:
    syllabi = get_syllabi(db_path, syllabus_ids)
    if not syllabi:
        raise ValueError(f"No syllabi found for ids: {syllabus_ids}")

    jobs = load_recent_detailed_jobs(db_path, candidate_limit)
    if not jobs:
        raise ValueError("No jobs found in job pool. Crawl jobs first.")

    try:
        with Neo4jGraphStore() as store:
            evidence_packages = store.retrieve_course_job_evidence(
                syllabus_ids,
                candidate_limit=min(max(candidate_limit, limit), MAX_JOBS_FOR_JUDGE),
            )
    except Exception as exc:
        raise ValueError(
            "Neo4j knowledge graph is not available. Start Neo4j and build/sync the knowledge graph before matching."
        ) from exc

    course_nodes = list_course_nodes(db_path, syllabus_ids)
    existing_syllabus_ids = {int(row["syllabus_id"]) for row in course_nodes}
    missing_syllabus_ids = [syllabus_id for syllabus_id in syllabus_ids if syllabus_id not in existing_syllabus_ids]
    if missing_syllabus_ids:
        raise ValueError(
            "Course knowledge graph is not ready. Build course graph first in the Knowledge Graph module. "
            f"Missing syllabus ids: {missing_syllabus_ids}"
        )

    job_ids = [int(row["id"]) for row in jobs]
    job_nodes = list_job_requirement_nodes(db_path, job_ids)
    covered_job_ids = sorted({int(row["job_id"]) for row in job_nodes}, reverse=True)
    if not covered_job_ids:
        raise ValueError(
            "Job knowledge graph is not ready. Build job requirement graph first in the Knowledge Graph module."
        )

    if not evidence_packages:
        raise ValueError(
            "GraphRAG did not retrieve candidate evidence from Neo4j. Build more job requirement nodes or adjust selected courses."
        )

    retrieved = retrieved_from_evidence_packages(evidence_packages)
    client = build_client()
    judged = judge_graphrag_matches(db_path, client, evidence_packages, limit=limit)
    candidate_job_ids = [int(package["job_id"]) for package in evidence_packages]
    result = build_match_result(db_path, syllabus_ids, judged, retrieved, candidate_job_ids)
    result["graphrag_evidence"] = trim_evidence_packages(evidence_packages)
    run_id = save_match_run(db_path, syllabus_ids, candidate_job_ids, result)
    result["match_run_id"] = run_id
    return result


def build_client() -> OpenAI:
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is not configured. Set it in .env or the terminal environment.")
    return OpenAI(api_key=api_key, base_url=os.getenv("OPENAI_BASE_URL") or None)


def load_recent_detailed_jobs(db_path: Path, candidate_limit: int) -> list[sqlite3.Row]:
    limit_sql = "" if candidate_limit <= 0 else "LIMIT ?"
    params: tuple[int, ...] = () if candidate_limit <= 0 else (candidate_limit,)
    with connect(db_path) as conn:
        return conn.execute(
            f"""
            SELECT *
            FROM jobs
            WHERE coalesce(description, '') != ''
            ORDER BY id DESC
            {limit_sql}
            """,
            params,
        ).fetchall()


def ensure_course_graph(db_path: Path, syllabus_id: int, client: OpenAI | None = None, force: bool = False) -> list[sqlite3.Row]:
    if not force:
        nodes = list_course_nodes(db_path, [syllabus_id])
        if nodes:
            return nodes
    return extract_course_graph(db_path, syllabus_id, client=client)


def ensure_job_requirement_graph(
    db_path: Path,
    job_ids: list[int],
    client: OpenAI | None = None,
    force: bool = False,
) -> dict[int, list[sqlite3.Row]]:
    existing = list_job_requirement_nodes(db_path, job_ids)
    existing_by_job: dict[int, list[sqlite3.Row]] = defaultdict(list)
    for row in existing:
        existing_by_job[int(row["job_id"])].append(row)

    missing = [job_id for job_id in job_ids if force or not existing_by_job.get(job_id)]
    if missing:
        extract_job_requirement_graph_batch(db_path, missing, client=client)

    rows = list_job_requirement_nodes(db_path, job_ids)
    result: dict[int, list[sqlite3.Row]] = defaultdict(list)
    for row in rows:
        result[int(row["job_id"])].append(row)
    return result


def extract_course_graph(db_path: Path, syllabus_id: int, client: OpenAI | None = None) -> list[sqlite3.Row]:
    syllabus = get_syllabus(db_path, syllabus_id)
    if not syllabus:
        raise ValueError(f"Syllabus not found: #{syllabus_id}")
    client = client or build_client()
    response = client.chat.completions.create(
        model=os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
        temperature=0.1,
        messages=[
            {
                "role": "system",
                "content": (
                    "You extract a compact course capability graph from university syllabi. "
                    "Use only evidence from the syllabus. Return valid JSON only."
                ),
            },
            {"role": "user", "content": build_course_graph_prompt(syllabus)},
        ],
    )
    payload = parse_json_response(response.choices[0].message.content or "")
    nodes = normalize_course_nodes(payload)
    save_course_nodes(db_path, syllabus_id, nodes, payload)
    return list_course_nodes(db_path, [syllabus_id])


def build_course_graph_prompt(syllabus: sqlite3.Row) -> str:
    raw_text = str(syllabus["raw_text"])[:MAX_SYLLABUS_CHARS]
    return f"""
Course title: {syllabus['title']}
File name: {syllabus['file_name']}

Extract 12 to 35 graph nodes. Prefer concrete capabilities, tools, methods, and knowledge that can be compared with job requirements.

Allowed node_type values:
- knowledge
- ability
- tool
- method
- job_direction

Allowed proficiency_level values:
- aware
- understand
- apply
- practice

Return JSON in this exact shape:
{{
  "nodes": [
    {{
      "node_type": "ability",
      "name": "short normalized name",
      "category": "domain category",
      "description": "what students learn or can do",
      "proficiency_level": "apply",
      "keywords": ["alias or search keyword"],
      "evidence_text": "short evidence copied or closely paraphrased from the syllabus",
      "evidence_locator": {{"page": "", "section": ""}}
    }}
  ]
}}

Rules:
- Do not invent tools, abilities, or job directions absent from the syllabus.
- Split broad items into useful atomic nodes.
- Keep evidence_text under 120 Chinese characters when possible.
- If the syllabus is Chinese, keep names and evidence in Chinese.

Syllabus text:
{raw_text}
""".strip()


def normalize_course_nodes(payload: dict[str, Any]) -> list[dict[str, Any]]:
    raw_nodes = payload.get("nodes", [])
    if not isinstance(raw_nodes, list):
        return []
    nodes: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for raw in raw_nodes:
        if not isinstance(raw, dict):
            continue
        node_type = str(raw.get("node_type") or "").strip()
        name = clean_text(raw.get("name"))
        if node_type not in COURSE_NODE_TYPES or not name:
            continue
        key = (node_type, name.lower())
        if key in seen:
            continue
        seen.add(key)
        nodes.append(
            {
                "node_type": node_type,
                "name": name[:120],
                "category": clean_text(raw.get("category"))[:80],
                "description": clean_text(raw.get("description"))[:500],
                "proficiency_level": normalize_choice(raw.get("proficiency_level"), {"aware", "understand", "apply", "practice"}, "understand"),
                "keywords": normalize_text_list(raw.get("keywords"), fallback=name),
                "evidence_text": clean_text(raw.get("evidence_text"))[:500],
                "evidence_locator": raw.get("evidence_locator") if isinstance(raw.get("evidence_locator"), dict) else {},
                "metadata": raw,
            }
        )
    return nodes


def save_course_nodes(db_path: Path, syllabus_id: int, nodes: list[dict[str, Any]], payload: dict[str, Any]) -> None:
    now = datetime.now().isoformat(timespec="seconds")
    with connect(db_path) as conn:
        old_ids = [
            int(row["id"])
            for row in conn.execute("SELECT id FROM course_graph_nodes WHERE syllabus_id = ?", (syllabus_id,)).fetchall()
        ]
        if old_ids:
            placeholders = ",".join("?" for _ in old_ids)
            conn.execute(f"DELETE FROM graph_match_edges WHERE course_node_id IN ({placeholders})", old_ids)
        conn.execute("DELETE FROM course_graph_nodes WHERE syllabus_id = ?", (syllabus_id,))
        conn.executemany(
            """
            INSERT INTO course_graph_nodes (
                syllabus_id, node_type, name, category, description, proficiency_level,
                keywords_json, evidence_text, evidence_locator_json, metadata_json,
                created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    syllabus_id,
                    node["node_type"],
                    node["name"],
                    node.get("category"),
                    node.get("description"),
                    node.get("proficiency_level"),
                    json.dumps(node.get("keywords", []), ensure_ascii=False),
                    node.get("evidence_text"),
                    json.dumps(node.get("evidence_locator", {}), ensure_ascii=False),
                    json.dumps(node.get("metadata", {}), ensure_ascii=False),
                    now,
                    now,
                )
                for node in nodes
            ],
        )
        save_extraction_run_conn(conn, "syllabus", syllabus_id, COURSE_EXTRACTOR_VERSION, payload, now)
        conn.commit()


def extract_job_requirement_graph_batch(
    db_path: Path,
    job_ids: list[int],
    client: OpenAI | None = None,
) -> dict[int, list[sqlite3.Row]]:
    client = client or build_client()
    jobs = [get_job(db_path, job_id) for job_id in job_ids]
    jobs = [job for job in jobs if job and str(job["description"] or "").strip()]
    if not jobs:
        return {}

    for batch in chunked(jobs, MAX_JOBS_PER_EXTRACT_CALL):
        response = client.chat.completions.create(
            model=os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
            temperature=0.1,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You extract structured job requirement graph nodes from job descriptions. "
                        "Use only the job description. Return valid JSON only."
                    ),
                },
                {"role": "user", "content": build_job_requirement_prompt(batch)},
            ],
        )
        payload = parse_json_response(response.choices[0].message.content or "")
        by_job = normalize_job_requirement_payload(payload)
        for row in batch:
            save_job_requirement_nodes(db_path, int(row["id"]), by_job.get(int(row["id"]), []), payload)

    rows = list_job_requirement_nodes(db_path, job_ids)
    result: dict[int, list[sqlite3.Row]] = defaultdict(list)
    for row in rows:
        result[int(row["job_id"])].append(row)
    return result


def build_job_requirement_prompt(jobs: list[sqlite3.Row]) -> str:
    job_blocks = []
    for row in jobs:
        desc = str(row["description"] or "")[:MAX_JOB_DESC_CHARS]
        job_blocks.append(
            f"""
[JOB {row['id']}]
Title: {row['title']}
Company: {row['company']}
City: {row['city']}
Salary: {row['salary']}
Education: {row['education']}
Experience: {row['experience']}
Description:
{desc}
""".strip()
        )
    return f"""
Extract 6 to 18 requirement nodes per job.

Allowed node_type values:
- responsibility
- skill
- tool
- experience
- education
- domain
- soft_skill

Allowed importance values:
- must
- preferred
- context

Return JSON in this exact shape:
{{
  "jobs": [
    {{
      "job_id": 123,
      "requirements": [
        {{
          "node_type": "skill",
          "requirement_text": "original requirement sentence or bullet",
          "normalized_name": "short normalized name",
          "category": "domain category",
          "importance": "must",
          "keywords": ["alias or search keyword"],
          "evidence_text": "same or shorter evidence from the JD",
          "evidence_locator": {{"section": ""}}
        }}
      ]
    }}
  ]
}}

Rules:
- Do not invent requirements absent from the JD.
- Split long bullets into atomic requirements.
- Keep requirement_text faithful to the original JD.
- If the JD is Chinese, keep text in Chinese.

Jobs:
{chr(10).join(job_blocks)}
""".strip()


def normalize_job_requirement_payload(payload: dict[str, Any]) -> dict[int, list[dict[str, Any]]]:
    result: dict[int, list[dict[str, Any]]] = defaultdict(list)
    raw_jobs = payload.get("jobs", [])
    if not isinstance(raw_jobs, list):
        return result
    for raw_job in raw_jobs:
        if not isinstance(raw_job, dict):
            continue
        try:
            job_id = int(raw_job.get("job_id"))
        except Exception:
            continue
        seen: set[tuple[str, str]] = set()
        for raw in raw_job.get("requirements", []):
            if not isinstance(raw, dict):
                continue
            node_type = str(raw.get("node_type") or "").strip()
            requirement = clean_text(raw.get("requirement_text"))
            if node_type not in JOB_NODE_TYPES or not requirement:
                continue
            key = (node_type, requirement.lower())
            if key in seen:
                continue
            seen.add(key)
            result[job_id].append(
                {
                    "node_type": node_type,
                    "requirement_text": requirement[:500],
                    "normalized_name": clean_text(raw.get("normalized_name"))[:120],
                    "category": clean_text(raw.get("category"))[:80],
                    "importance": normalize_choice(raw.get("importance"), {"must", "preferred", "context"}, "must"),
                    "keywords": normalize_text_list(raw.get("keywords"), fallback=requirement),
                    "evidence_text": clean_text(raw.get("evidence_text") or requirement)[:500],
                    "evidence_locator": raw.get("evidence_locator") if isinstance(raw.get("evidence_locator"), dict) else {},
                    "metadata": raw,
                }
            )
    return result


def save_job_requirement_nodes(
    db_path: Path,
    job_id: int,
    requirements: list[dict[str, Any]],
    payload: dict[str, Any],
) -> None:
    now = datetime.now().isoformat(timespec="seconds")
    with connect(db_path) as conn:
        old_ids = [
            int(row["id"])
            for row in conn.execute("SELECT id FROM job_requirement_nodes WHERE job_id = ?", (job_id,)).fetchall()
        ]
        if old_ids:
            placeholders = ",".join("?" for _ in old_ids)
            conn.execute(f"DELETE FROM graph_match_edges WHERE job_requirement_id IN ({placeholders})", old_ids)
        conn.execute("DELETE FROM job_requirement_nodes WHERE job_id = ?", (job_id,))
        conn.executemany(
            """
            INSERT INTO job_requirement_nodes (
                job_id, node_type, requirement_text, normalized_name, category, importance,
                keywords_json, evidence_text, evidence_locator_json, metadata_json,
                created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    job_id,
                    item["node_type"],
                    item["requirement_text"],
                    item.get("normalized_name"),
                    item.get("category"),
                    item.get("importance"),
                    json.dumps(item.get("keywords", []), ensure_ascii=False),
                    item.get("evidence_text"),
                    json.dumps(item.get("evidence_locator", {}), ensure_ascii=False),
                    json.dumps(item.get("metadata", {}), ensure_ascii=False),
                    now,
                    now,
                )
                for item in requirements
            ],
        )
        save_extraction_run_conn(conn, "job", job_id, JOB_EXTRACTOR_VERSION, payload, now)
        conn.commit()


def retrieve_candidate_jobs(
    db_path: Path,
    syllabus_ids: list[int],
    job_ids: list[int],
    max_jobs: int,
) -> list[dict[str, Any]]:
    course_nodes = list_course_nodes(db_path, syllabus_ids)
    job_nodes = list_job_requirement_nodes(db_path, job_ids)
    course_keywords = build_course_keyword_index(course_nodes)

    scores: Counter[int] = Counter()
    signals: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for job_node in job_nodes:
        job_terms = row_terms(job_node, name_fields=("requirement_text", "normalized_name", "category"))
        hits = sorted(course_keywords.keys() & job_terms, key=len, reverse=True)
        if not hits:
            continue
        job_id = int(job_node["job_id"])
        weight = 3 if job_node["node_type"] in {"skill", "tool", "responsibility"} else 1
        scores[job_id] += len(hits) * weight
        for hit in hits[:5]:
            for course_node in course_keywords[hit][:3]:
                signals[job_id].append(
                    {
                        "keyword": hit,
                        "course_node_id": int(course_node["id"]),
                        "course_node_name": course_node["name"],
                        "job_requirement_id": int(job_node["id"]),
                        "job_requirement": job_node["requirement_text"],
                    }
                )

    ranked = []
    for job_id, score in scores.most_common(max_jobs):
        ranked.append({"job_id": job_id, "keyword_hits": int(score), "signals": signals[job_id][:12]})
    return ranked


def build_course_keyword_index(course_nodes: list[sqlite3.Row]) -> dict[str, list[sqlite3.Row]]:
    index: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for node in course_nodes:
        terms = row_terms(node, name_fields=("name", "category", "description"))
        for term in terms:
            index[term].append(node)
    return index


def row_terms(row: sqlite3.Row, name_fields: tuple[str, ...]) -> set[str]:
    chunks: list[str] = []
    for field in name_fields:
        if field in row.keys() and row[field]:
            chunks.append(str(row[field]))
    if "keywords_json" in row.keys():
        chunks.extend(normalize_text_list(json_loads(row["keywords_json"], [])))
    return tokenize_terms(" ".join(chunks))


def tokenize_terms(text: str) -> set[str]:
    normalized = text.lower()
    tokens = set(re.findall(r"[a-zA-Z][a-zA-Z0-9+#.\-]{1,}|[\u4e00-\u9fff]{2,}", normalized))
    merged = set(tokens)
    chinese = [token for token in tokens if re.fullmatch(r"[\u4e00-\u9fff]{2,}", token)]
    for token in chinese:
        if len(token) > 4:
            for size in (2, 3, 4):
                for index in range(0, len(token) - size + 1):
                    merged.add(token[index : index + size])
    stopwords = {"能力", "相关", "岗位", "课程", "学生", "掌握", "熟悉", "了解", "进行", "使用", "负责", "要求"}
    return {token for token in merged if token not in stopwords and len(token) >= 2}


def judge_graph_matches(
    db_path: Path,
    client: OpenAI,
    syllabus_ids: list[int],
    retrieved: list[dict[str, Any]],
    limit: int,
) -> dict[str, Any]:
    course_nodes = list_course_nodes(db_path, syllabus_ids)
    candidate_job_ids = [int(item["job_id"]) for item in retrieved[:MAX_JOBS_FOR_JUDGE]]
    job_nodes = list_job_requirement_nodes(db_path, candidate_job_ids)
    jobs = load_jobs_by_id(db_path, candidate_job_ids)

    response = client.chat.completions.create(
        model=os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
        temperature=0.1,
        messages=[
            {
                "role": "system",
                "content": (
                    "You judge course-to-job matches using only structured graph nodes and evidence. "
                    "Return valid JSON only. Do not output a numeric match score."
                ),
            },
            {"role": "user", "content": build_graph_match_prompt(course_nodes, job_nodes, jobs, retrieved, limit)},
        ],
    )
    payload = parse_json_response(response.choices[0].message.content or "")
    normalized = normalize_match_payload(payload)
    save_graph_edges(db_path, normalized)
    return normalized


def judge_graphrag_matches(
    db_path: Path,
    client: OpenAI,
    evidence_packages: list[dict[str, Any]],
    limit: int,
) -> dict[str, Any]:
    response = client.chat.completions.create(
        model=os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
        temperature=0.1,
        messages=[
            {
                "role": "system",
                "content": (
                    "You judge course-to-job matches using only GraphRAG evidence packages. "
                    "Each package contains graph paths, course node evidence, job requirement evidence, "
                    "and limited job context. Return valid JSON only. Do not output a numeric match score."
                ),
            },
            {"role": "user", "content": build_graphrag_match_prompt(evidence_packages, limit)},
        ],
    )
    payload = parse_json_response(response.choices[0].message.content or "")
    normalized = normalize_match_payload(payload)
    save_graph_edges(db_path, normalized)
    sync_match_edges_to_neo4j(db_path, normalized)
    return normalized


def build_graphrag_match_prompt(evidence_packages: list[dict[str, Any]], limit: int) -> str:
    packages = []
    for package in evidence_packages[:MAX_JOBS_FOR_JUDGE]:
        paths = []
        for path in package.get("evidence_paths", [])[:10]:
            paths.append(
                {
                    "graph_path": path.get("path", []),
                    "course_node_id": path.get("course_node_id"),
                    "course_node": {
                        "syllabus_id": path.get("syllabus_id"),
                        "syllabus_title": path.get("syllabus_title"),
                        "type": path.get("course_node_type"),
                        "name": path.get("course_node_name"),
                        "category": path.get("course_category"),
                        "description": path.get("course_description"),
                        "evidence": path.get("course_evidence"),
                    },
                    "job_requirement_id": path.get("requirement_id"),
                    "job_requirement": {
                        "type": path.get("requirement_type"),
                        "name": path.get("requirement_name"),
                        "category": path.get("requirement_category"),
                        "importance": path.get("requirement_importance"),
                        "text": path.get("requirement_text"),
                        "evidence": path.get("requirement_evidence"),
                    },
                }
            )
        packages.append(
            {
                "job": package.get("job", {}),
                "retrieval_score": package.get("retrieval_score", 0),
                "evidence_paths": paths,
            }
        )
    return (
        "Select the best matching jobs from these GraphRAG evidence packages.\n"
        "Use only the provided graph paths, course node evidence, job requirement evidence, and job metadata.\n"
        "Reject jobs where the relationship is only a weak keyword coincidence.\n"
        "Do not output a numeric score.\n\n"
        "Return JSON in this exact shape:\n"
        "{\n"
        '  "matches": [\n'
        "    {\n"
        '      "job_id": 123,\n'
        '      "explanation": "overall explanation based on the evidence paths",\n'
        '      "edges": [\n'
        "        {\n"
        '          "course_node_id": 1,\n'
        '          "job_requirement_id": 9,\n'
        '          "relation_type": "supports",\n'
        '          "confidence_label": "high",\n'
        '          "rationale": "why this course node supports this requirement",\n'
        '          "course_evidence": "course evidence copied from the package",\n'
        '          "job_evidence": "job evidence copied from the package"\n'
        "        }\n"
        "      ]\n"
        "    }\n"
        "  ]\n"
        "}\n\n"
        f"Return at most {limit} jobs. Use relation_type supports, partially_supports, or related. "
        "Use high/medium/low confidence labels for audit only.\n\n"
        f"GraphRAG evidence packages:\n{json.dumps(packages, ensure_ascii=False)}"
    )


def build_graph_match_prompt(
    course_nodes: list[sqlite3.Row],
    job_nodes: list[sqlite3.Row],
    jobs: dict[int, sqlite3.Row],
    retrieved: list[dict[str, Any]],
    limit: int,
) -> str:
    course_block = [
        {
            "course_node_id": int(row["id"]),
            "syllabus_id": int(row["syllabus_id"]),
            "type": row["node_type"],
            "name": row["name"],
            "category": row["category"],
            "description": row["description"],
            "evidence": row["evidence_text"],
            "keywords": json_loads(row["keywords_json"], []),
        }
        for row in course_nodes
    ]
    job_block_by_id: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for row in job_nodes:
        job_block_by_id[int(row["job_id"])].append(
            {
                "job_requirement_id": int(row["id"]),
                "type": row["node_type"],
                "requirement": row["requirement_text"],
                "normalized_name": row["normalized_name"],
                "importance": row["importance"],
                "evidence": row["evidence_text"],
                "keywords": json_loads(row["keywords_json"], []),
            }
        )
    job_blocks = []
    retrieved_by_id = {int(item["job_id"]): item for item in retrieved}
    for job_id in [int(item["job_id"]) for item in retrieved]:
        row = jobs.get(job_id)
        if not row:
            continue
        job_blocks.append(
            {
                "job_id": job_id,
                "title": row["title"],
                "company": row["company"],
                "city": row["city"],
                "salary": row["salary"],
                "education": row["education"],
                "experience": row["experience"],
                "retrieval_signals": retrieved_by_id.get(job_id, {}).get("signals", [])[:8],
                "requirements": job_block_by_id.get(job_id, []),
            }
        )
    return (
        "Select the best matching jobs for the selected courses.\n"
        "Only use the provided course nodes and job requirement nodes.\n"
        "Reject jobs where the relationship is only a weak keyword coincidence.\n"
        "Do not output a numeric score.\n\n"
        "Return JSON in this exact shape:\n"
        "{\n"
        '  "matches": [\n'
        "    {\n"
        '      "job_id": 123,\n'
        '      "explanation": "overall explanation",\n'
        '      "edges": [\n'
        "        {\n"
        '          "course_node_id": 1,\n'
        '          "job_requirement_id": 9,\n'
        '          "relation_type": "supports",\n'
        '          "confidence_label": "high",\n'
        '          "rationale": "why this course evidence supports this requirement",\n'
        '          "course_evidence": "course evidence",\n'
        '          "job_evidence": "job evidence"\n'
        "        }\n"
        "      ]\n"
        "    }\n"
        "  ]\n"
        "}\n\n"
        f"Return at most {limit} jobs. Use relation_type supports, partially_supports, or related. "
        "Use high/medium/low confidence labels for audit only.\n\n"
        f"Course nodes:\n{json.dumps(course_block, ensure_ascii=False)}\n\n"
        f"Candidate jobs:\n{json.dumps(job_blocks, ensure_ascii=False)}"
    )


def retrieved_from_evidence_packages(evidence_packages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    retrieved = []
    for package in evidence_packages:
        signals = []
        for path in package.get("evidence_paths", [])[:12]:
            signals.append(
                {
                    "keyword": path.get("course_node_name") or "",
                    "course_node_id": path.get("course_node_id"),
                    "course_node_name": path.get("course_node_name"),
                    "job_requirement_id": path.get("requirement_id"),
                    "job_requirement": path.get("requirement_text"),
                    "graph_path": path.get("path", []),
                }
            )
        retrieved.append(
            {
                "job_id": int(package["job_id"]),
                "keyword_hits": len(signals),
                "retrieval_score": float(package.get("retrieval_score") or 0),
                "signals": signals,
            }
        )
    return retrieved


def trim_evidence_packages(evidence_packages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    trimmed = []
    for package in evidence_packages[:MAX_JOBS_FOR_JUDGE]:
        trimmed.append(
            {
                "job_id": package.get("job_id"),
                "job": package.get("job", {}),
                "retrieval_score": package.get("retrieval_score", 0),
                "evidence_paths": package.get("evidence_paths", [])[:8],
            }
        )
    return trimmed


def normalize_match_payload(payload: dict[str, Any]) -> dict[str, Any]:
    matches = []
    for raw_match in payload.get("matches", []):
        if not isinstance(raw_match, dict):
            continue
        try:
            job_id = int(raw_match.get("job_id"))
        except Exception:
            continue
        edges = []
        for raw_edge in raw_match.get("edges", []):
            if not isinstance(raw_edge, dict):
                continue
            try:
                course_node_id = int(raw_edge.get("course_node_id"))
                job_requirement_id = int(raw_edge.get("job_requirement_id"))
            except Exception:
                continue
            relation_type = normalize_choice(raw_edge.get("relation_type"), RELATION_TYPES, "related")
            if relation_type == "not_supported":
                continue
            edges.append(
                {
                    "course_node_id": course_node_id,
                    "job_requirement_id": job_requirement_id,
                    "relation_type": relation_type,
                    "confidence_label": normalize_choice(raw_edge.get("confidence_label"), {"high", "medium", "low"}, "medium"),
                    "rationale": clean_text(raw_edge.get("rationale"))[:600],
                    "course_evidence": clean_text(raw_edge.get("course_evidence"))[:500],
                    "job_evidence": clean_text(raw_edge.get("job_evidence"))[:500],
                    "metadata": raw_edge,
                }
            )
        if edges:
            matches.append({"job_id": job_id, "explanation": clean_text(raw_match.get("explanation"))[:1000], "edges": edges})
    return {"matches": matches}


def save_graph_edges(db_path: Path, payload: dict[str, Any]) -> None:
    now = datetime.now().isoformat(timespec="seconds")
    with connect(db_path) as conn:
        for match in payload.get("matches", []):
            job_id = int(match["job_id"])
            rows_to_insert = []
            for edge in match.get("edges", []):
                course = conn.execute("SELECT syllabus_id FROM course_graph_nodes WHERE id = ?", (edge["course_node_id"],)).fetchone()
                requirement = conn.execute("SELECT job_id FROM job_requirement_nodes WHERE id = ?", (edge["job_requirement_id"],)).fetchone()
                if not course or not requirement or int(requirement["job_id"]) != job_id:
                    continue
                rows_to_insert.append(
                    (
                        int(course["syllabus_id"]),
                        job_id,
                        edge["course_node_id"],
                        edge["job_requirement_id"],
                        edge["relation_type"],
                        edge["confidence_label"],
                        edge["rationale"],
                        edge["course_evidence"],
                        edge["job_evidence"],
                        json.dumps(edge.get("metadata", {}), ensure_ascii=False),
                        now,
                        now,
                    )
                )
            conn.executemany(
                """
                INSERT INTO graph_match_edges (
                    syllabus_id, job_id, course_node_id, job_requirement_id,
                    relation_type, confidence_label, rationale, course_evidence,
                    job_evidence, metadata_json, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(course_node_id, job_requirement_id, relation_type) DO UPDATE SET
                    confidence_label = excluded.confidence_label,
                    rationale = excluded.rationale,
                    course_evidence = excluded.course_evidence,
                    job_evidence = excluded.job_evidence,
                    metadata_json = excluded.metadata_json,
                    updated_at = excluded.updated_at
                """,
                rows_to_insert,
            )
        save_extraction_run_conn(conn, "match", 0, MATCHER_VERSION, payload, now)
        conn.commit()


def sync_match_edges_to_neo4j(db_path: Path, payload: dict[str, Any]) -> None:
    keys = []
    for match in payload.get("matches", []):
        for edge in match.get("edges", []):
            keys.append((edge["course_node_id"], edge["job_requirement_id"], edge["relation_type"]))
    if not keys:
        return
    rows = []
    with connect(db_path) as conn:
        for course_node_id, job_requirement_id, relation_type in keys:
            row = conn.execute(
                """
                SELECT *
                FROM graph_match_edges
                WHERE course_node_id = ?
                  AND job_requirement_id = ?
                  AND relation_type = ?
                """,
                (course_node_id, job_requirement_id, relation_type),
            ).fetchone()
            if row:
                rows.append(row)
    if rows:
        with Neo4jGraphStore() as store:
            store.sync_match_edges(rows)


def build_match_result(
    db_path: Path,
    syllabus_ids: list[int],
    judged: dict[str, Any],
    retrieved: list[dict[str, Any]],
    candidate_job_ids: list[int],
) -> dict[str, Any]:
    job_ids = [int(match["job_id"]) for match in judged.get("matches", [])]
    jobs = load_jobs_by_id(db_path, job_ids)
    course_nodes_by_id = {int(row["id"]): row for row in list_course_nodes(db_path, syllabus_ids)}
    requirement_nodes_by_id = {int(row["id"]): row for row in list_job_requirement_nodes(db_path, job_ids)}
    syllabi = {int(row["id"]): row for row in get_syllabi(db_path, syllabus_ids)}

    matches = []
    for match in judged.get("matches", []):
        job_id = int(match["job_id"])
        job = jobs.get(job_id)
        if not job:
            continue
        evidence_pairs = []
        matched_course_content = []
        matched_job_requirements = []
        for edge in match.get("edges", []):
            course_node = course_nodes_by_id.get(int(edge["course_node_id"]))
            requirement = requirement_nodes_by_id.get(int(edge["job_requirement_id"]))
            if not course_node or not requirement:
                continue
            syllabus = syllabi.get(int(course_node["syllabus_id"]))
            course_evidence = edge.get("course_evidence") or course_node["evidence_text"] or course_node["description"] or course_node["name"]
            job_evidence = edge.get("job_evidence") or requirement["evidence_text"] or requirement["requirement_text"]
            matched_course_content.append(format_course_evidence(course_node, course_evidence, syllabus))
            matched_job_requirements.append(job_evidence)
            evidence_pairs.append(
                {
                    "job_requirement_id": int(requirement["id"]),
                    "job_requirement": requirement["requirement_text"],
                    "job_evidence": job_evidence,
                    "job_requirement_type": requirement["node_type"],
                    "course_node_id": int(course_node["id"]),
                    "course_node": course_node["name"],
                    "course_node_type": course_node["node_type"],
                    "syllabus_id": int(course_node["syllabus_id"]),
                    "syllabus_title": syllabus["title"] if syllabus else "",
                    "course_evidence": course_evidence,
                    "relation_type": edge["relation_type"],
                    "confidence_label": edge["confidence_label"],
                    "rationale": edge.get("rationale", ""),
                }
            )
        matches.append(
            {
                "job_id": job_id,
                "job_title": job["title"],
                "company": job["company"],
                "matched_course_content": dedupe_texts(matched_course_content),
                "matched_job_requirements": dedupe_texts(matched_job_requirements),
                "evidence_pairs": evidence_pairs,
                "explanation": match.get("explanation", ""),
            }
        )

    return {
        "syllabus_ids": syllabus_ids,
        "candidate_job_ids": candidate_job_ids,
        "matches": matches,
        "match_mode": "graphrag",
        "note": "GraphRAG uses Neo4j evidence paths, course node evidence, job requirement evidence, and limited job metadata. It does not output numeric match scores.",
        "retrieval": retrieved,
    }


def format_course_evidence(course_node: sqlite3.Row, evidence: str, syllabus: sqlite3.Row | None) -> str:
    title = syllabus["title"] if syllabus else f"syllabus #{course_node['syllabus_id']}"
    return f"{title}: {course_node['name']} - {evidence}"


def list_course_nodes(db_path: Path, syllabus_ids: list[int]) -> list[sqlite3.Row]:
    if not syllabus_ids:
        return []
    placeholders = ",".join("?" for _ in syllabus_ids)
    with connect(db_path) as conn:
        return conn.execute(
            f"""
            SELECT *
            FROM course_graph_nodes
            WHERE syllabus_id IN ({placeholders})
            ORDER BY syllabus_id, node_type, id
            """,
            syllabus_ids,
        ).fetchall()


def list_job_requirement_nodes(db_path: Path, job_ids: list[int]) -> list[sqlite3.Row]:
    if not job_ids:
        return []
    placeholders = ",".join("?" for _ in job_ids)
    with connect(db_path) as conn:
        return conn.execute(
            f"""
            SELECT *
            FROM job_requirement_nodes
            WHERE job_id IN ({placeholders})
            ORDER BY job_id, node_type, id
            """,
            job_ids,
        ).fetchall()


def load_jobs_by_id(db_path: Path, job_ids: list[int]) -> dict[int, sqlite3.Row]:
    if not job_ids:
        return {}
    placeholders = ",".join("?" for _ in job_ids)
    with connect(db_path) as conn:
        rows = conn.execute(f"SELECT * FROM jobs WHERE id IN ({placeholders})", job_ids).fetchall()
    return {int(row["id"]): row for row in rows}


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


def save_extraction_run_conn(
    conn: sqlite3.Connection,
    source_type: str,
    source_id: int,
    extractor_version: str,
    result: dict[str, Any],
    now: str,
) -> None:
    conn.execute(
        """
        INSERT INTO graph_extraction_runs (source_type, source_id, extractor_version, result_json, created_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (source_type, source_id, extractor_version, json.dumps(result, ensure_ascii=False), now),
    )


def graph_stats(db_path: Path) -> dict[str, int]:
    with connect(db_path) as conn:
        row = conn.execute(
            """
            SELECT
                (SELECT count(*) FROM course_graph_nodes) AS course_node_count,
                (SELECT count(*) FROM job_requirement_nodes) AS job_requirement_count,
                (SELECT count(*) FROM graph_match_edges) AS graph_edge_count,
                (SELECT count(*) FROM graph_extraction_runs) AS graph_run_count
            """
        ).fetchone()
    return dict(row)


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


def clean_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def normalize_text_list(value: Any, fallback: str = "") -> list[str]:
    if isinstance(value, list):
        result = [clean_text(item) for item in value if clean_text(item)]
    elif isinstance(value, str) and value.strip():
        result = [clean_text(value)]
    else:
        result = []
    if fallback:
        result.append(fallback)
    return dedupe_texts(result)[:20]


def normalize_choice(value: Any, allowed: set[str], fallback: str) -> str:
    text = str(value or "").strip()
    return text if text in allowed else fallback


def dedupe_texts(items: list[str]) -> list[str]:
    seen: set[str] = set()
    result = []
    for item in items:
        text = clean_text(item)
        if not text:
            continue
        key = text.lower()
        if key in seen:
            continue
        seen.add(key)
        result.append(text)
    return result


def chunked(items: list[Any], size: int) -> list[list[Any]]:
    return [items[index : index + size] for index in range(0, len(items), size)]
