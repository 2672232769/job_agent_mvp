from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Iterable
from urllib.parse import urlsplit, urlunsplit

from .models import JobPost


SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    title TEXT NOT NULL,
    company TEXT NOT NULL,
    city TEXT,
    salary TEXT,
    description TEXT,
    url TEXT,
    address TEXT,
    education TEXT,
    experience TEXT,
    industry TEXT,
    company_size TEXT,
    tags_json TEXT,
    publish_date TEXT,
    crawled_at TEXT NOT NULL,
    raw_json TEXT,
    UNIQUE(source, title, company, city, url)
);

CREATE INDEX IF NOT EXISTS idx_jobs_source ON jobs(source);
CREATE INDEX IF NOT EXISTS idx_jobs_city ON jobs(city);
CREATE INDEX IF NOT EXISTS idx_jobs_crawled_at ON jobs(crawled_at);
CREATE UNIQUE INDEX IF NOT EXISTS idx_jobs_source_url_unique
ON jobs(source, url)
WHERE coalesce(url, '') != '';

CREATE TABLE IF NOT EXISTS syllabi (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    file_name TEXT NOT NULL,
    source_path TEXT NOT NULL,
    stored_path TEXT,
    file_type TEXT NOT NULL,
    content_hash TEXT NOT NULL UNIQUE,
    raw_text TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_syllabi_title ON syllabi(title);

CREATE TABLE IF NOT EXISTS match_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    syllabus_ids_json TEXT NOT NULL,
    candidate_job_ids_json TEXT NOT NULL,
    result_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS syllabus_profiles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    syllabus_id INTEGER NOT NULL UNIQUE,
    summary TEXT,
    knowledge_points_json TEXT,
    abilities_json TEXT,
    technologies_tools_methods_json TEXT,
    job_directions_json TEXT,
    profile_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (syllabus_id) REFERENCES syllabi(id)
);

CREATE TABLE IF NOT EXISTS course_graph_nodes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    syllabus_id INTEGER NOT NULL,
    node_type TEXT NOT NULL,
    name TEXT NOT NULL,
    category TEXT,
    description TEXT,
    proficiency_level TEXT,
    keywords_json TEXT,
    evidence_text TEXT,
    evidence_locator_json TEXT,
    metadata_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (syllabus_id) REFERENCES syllabi(id),
    UNIQUE(syllabus_id, node_type, name)
);

CREATE INDEX IF NOT EXISTS idx_course_graph_nodes_syllabus ON course_graph_nodes(syllabus_id);
CREATE INDEX IF NOT EXISTS idx_course_graph_nodes_type ON course_graph_nodes(node_type);
CREATE INDEX IF NOT EXISTS idx_course_graph_nodes_category ON course_graph_nodes(category);

CREATE TABLE IF NOT EXISTS job_requirement_nodes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id INTEGER NOT NULL,
    node_type TEXT NOT NULL,
    requirement_text TEXT NOT NULL,
    normalized_name TEXT,
    category TEXT,
    importance TEXT,
    keywords_json TEXT,
    evidence_text TEXT,
    evidence_locator_json TEXT,
    metadata_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (job_id) REFERENCES jobs(id),
    UNIQUE(job_id, node_type, requirement_text)
);

CREATE INDEX IF NOT EXISTS idx_job_requirement_nodes_job ON job_requirement_nodes(job_id);
CREATE INDEX IF NOT EXISTS idx_job_requirement_nodes_type ON job_requirement_nodes(node_type);
CREATE INDEX IF NOT EXISTS idx_job_requirement_nodes_category ON job_requirement_nodes(category);

CREATE TABLE IF NOT EXISTS graph_match_edges (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    syllabus_id INTEGER NOT NULL,
    job_id INTEGER NOT NULL,
    course_node_id INTEGER NOT NULL,
    job_requirement_id INTEGER NOT NULL,
    relation_type TEXT NOT NULL,
    confidence_label TEXT,
    rationale TEXT,
    course_evidence TEXT,
    job_evidence TEXT,
    metadata_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (syllabus_id) REFERENCES syllabi(id),
    FOREIGN KEY (job_id) REFERENCES jobs(id),
    FOREIGN KEY (course_node_id) REFERENCES course_graph_nodes(id),
    FOREIGN KEY (job_requirement_id) REFERENCES job_requirement_nodes(id),
    UNIQUE(course_node_id, job_requirement_id, relation_type)
);

CREATE INDEX IF NOT EXISTS idx_graph_match_edges_syllabus_job ON graph_match_edges(syllabus_id, job_id);
CREATE INDEX IF NOT EXISTS idx_graph_match_edges_course_node ON graph_match_edges(course_node_id);
CREATE INDEX IF NOT EXISTS idx_graph_match_edges_job_requirement ON graph_match_edges(job_requirement_id);

CREATE TABLE IF NOT EXISTS graph_extraction_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_type TEXT NOT NULL,
    source_id INTEGER NOT NULL,
    extractor_version TEXT NOT NULL,
    result_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_graph_extraction_runs_source ON graph_extraction_runs(source_type, source_id);
CREATE INDEX IF NOT EXISTS idx_graph_extraction_runs_version ON graph_extraction_runs(extractor_version);

CREATE TABLE IF NOT EXISTS graph_embeddings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_type TEXT NOT NULL,
    owner_id INTEGER NOT NULL,
    model TEXT NOT NULL,
    vector_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(owner_type, owner_id, model)
);

CREATE INDEX IF NOT EXISTS idx_graph_embeddings_owner ON graph_embeddings(owner_type, owner_id);
"""


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


def normalize_job_url(url: str | None) -> str:
    text = (url or "").strip()
    if not text:
        return ""
    try:
        parts = urlsplit(text)
    except ValueError:
        return text
    if not parts.scheme or not parts.netloc:
        return text
    netloc = parts.netloc.lower()
    path = parts.path or ""
    return urlunsplit((parts.scheme.lower(), netloc, path, "", ""))


def save_jobs(db_path: Path, jobs: Iterable[JobPost]) -> tuple[int, int]:
    inserted = 0
    skipped = 0
    with connect(db_path) as conn:
        for job in jobs:
            normalized_url = normalize_job_url(job.url)
            cur = conn.execute(
                """
                INSERT OR IGNORE INTO jobs (
                    source, title, company, city, salary, description, url, address,
                    education, experience, industry, company_size, tags_json,
                    publish_date, crawled_at, raw_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job.source,
                    job.title,
                    job.company,
                    job.city,
                    job.salary,
                    job.description,
                    normalized_url,
                    job.address,
                    job.education,
                    job.experience,
                    job.industry,
                    job.company_size,
                    json.dumps(job.tags, ensure_ascii=False),
                    job.publish_date,
                    job.crawled_at,
                    json.dumps(job.raw, ensure_ascii=False),
                ),
            )
            if cur.rowcount:
                inserted += 1
            else:
                update_sql = """
                UPDATE jobs
                SET
                    title = CASE WHEN coalesce(title, '') = '' THEN ? ELSE title END,
                    company = CASE WHEN coalesce(company, '') = '' THEN ? ELSE company END,
                    city = CASE WHEN coalesce(city, '') = '' THEN ? ELSE city END,
                    salary = CASE WHEN coalesce(salary, '') = '' THEN ? ELSE salary END,
                    description = CASE
                        WHEN length(coalesce(description, '')) < length(coalesce(?, '')) THEN ?
                        ELSE description
                    END,
                    raw_json = ?,
                    crawled_at = ?
                WHERE source = ? AND url = ?
                """
                update_params = (
                    job.title,
                    job.company,
                    job.city,
                    job.salary,
                    job.description,
                    job.description,
                    json.dumps(job.raw, ensure_ascii=False),
                    job.crawled_at,
                    job.source,
                    normalized_url,
                )
                if not normalized_url:
                    update_sql = """
                    UPDATE jobs
                    SET
                        description = CASE
                            WHEN length(coalesce(description, '')) < length(coalesce(?, '')) THEN ?
                            ELSE description
                        END,
                        raw_json = ?,
                        crawled_at = ?
                    WHERE source = ? AND title = ? AND company = ? AND city = ? AND url = ?
                    """
                    update_params = (
                        job.description,
                        job.description,
                        json.dumps(job.raw, ensure_ascii=False),
                        job.crawled_at,
                        job.source,
                        job.title,
                        job.company,
                        job.city,
                        normalized_url,
                    )
                conn.execute(update_sql, update_params)
                skipped += 1
        conn.commit()
    return inserted, skipped


def list_jobs(db_path: Path, limit: int = 20) -> list[sqlite3.Row]:
    with connect(db_path) as conn:
        return conn.execute(
            """
            SELECT id, source, title, company, city, salary, publish_date, url, crawled_at
            FROM jobs
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()


def get_job(db_path: Path, job_id: int) -> sqlite3.Row | None:
    with connect(db_path) as conn:
        return conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()


def export_jobs(db_path: Path) -> list[dict]:
    with connect(db_path) as conn:
        rows = conn.execute("SELECT * FROM jobs ORDER BY id DESC").fetchall()
    result = []
    for row in rows:
        item = dict(row)
        item["tags"] = json.loads(item.pop("tags_json") or "[]")
        item["raw"] = json.loads(item.pop("raw_json") or "{}")
        result.append(item)
    return result
