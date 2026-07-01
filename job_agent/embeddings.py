from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

from openai import OpenAI

from .storage import connect


COURSE_NODE_OWNER = "course_node"
JOB_REQUIREMENT_OWNER = "job_requirement"
DEFAULT_EMBEDDING_MODEL = "text-embedding-3-small"


def embedding_model() -> str:
    return os.getenv("OPENAI_EMBEDDING_MODEL", DEFAULT_EMBEDDING_MODEL).strip() or DEFAULT_EMBEDDING_MODEL


def build_embedding_client() -> OpenAI:
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is not configured. Set it before building graph embeddings.")
    return OpenAI(api_key=api_key, base_url=os.getenv("OPENAI_BASE_URL") or None)


def embedding_status(db_path: Path) -> dict[str, Any]:
    model = embedding_model()
    with connect(db_path) as conn:
        row = conn.execute(
            """
            SELECT
              sum(CASE WHEN owner_type = ? AND model = ? THEN 1 ELSE 0 END) AS course_node_embeddings,
              sum(CASE WHEN owner_type = ? AND model = ? THEN 1 ELSE 0 END) AS job_requirement_embeddings
            FROM graph_embeddings
            """,
            (COURSE_NODE_OWNER, model, JOB_REQUIREMENT_OWNER, model),
        ).fetchone()
    return {
        "model": model,
        "course_node_embeddings": int(row["course_node_embeddings"] or 0),
        "job_requirement_embeddings": int(row["job_requirement_embeddings"] or 0),
    }


def ensure_course_node_embeddings(
    db_path: Path,
    nodes: list[sqlite3.Row],
    client: OpenAI | None = None,
    force: bool = False,
) -> dict[int, list[float]]:
    return ensure_graph_embeddings(
        db_path,
        COURSE_NODE_OWNER,
        nodes,
        course_node_embedding_text,
        client=client,
        force=force,
    )


def ensure_job_requirement_embeddings(
    db_path: Path,
    nodes: list[sqlite3.Row],
    client: OpenAI | None = None,
    force: bool = False,
) -> dict[int, list[float]]:
    return ensure_graph_embeddings(
        db_path,
        JOB_REQUIREMENT_OWNER,
        nodes,
        job_requirement_embedding_text,
        client=client,
        force=force,
    )


def ensure_graph_embeddings(
    db_path: Path,
    owner_type: str,
    rows: list[sqlite3.Row],
    text_builder,
    client: OpenAI | None = None,
    force: bool = False,
) -> dict[int, list[float]]:
    if not rows:
        return {}

    model = embedding_model()
    row_ids = [int(row["id"]) for row in rows]
    existing = {} if force else load_embeddings(db_path, owner_type, row_ids, model)
    missing_rows = [row for row in rows if int(row["id"]) not in existing]

    if missing_rows:
        client = client or build_embedding_client()
        batch_size = max(1, min(int(os.getenv("OPENAI_EMBEDDING_BATCH_SIZE", "64")), 256))
        for batch in chunked(missing_rows, batch_size):
            texts = [text_builder(row) for row in batch]
            response = client.embeddings.create(model=model, input=texts)
            vectors = [list(item.embedding) for item in response.data]
            if len(vectors) != len(batch):
                raise RuntimeError(
                    f"Embedding API returned {len(vectors)} vectors for {len(batch)} {owner_type} nodes."
                )
            save_embeddings(db_path, owner_type, model, [int(row["id"]) for row in batch], vectors)
            existing.update({int(row["id"]): vector for row, vector in zip(batch, vectors)})

    expected_dimension = len(next(iter(existing.values()))) if existing else 0
    if expected_dimension <= 0:
        raise RuntimeError(f"No embeddings were generated for {owner_type}.")
    for owner_id, vector in existing.items():
        if len(vector) != expected_dimension:
            raise RuntimeError(
                f"Embedding dimension mismatch for {owner_type} #{owner_id}: "
                f"expected {expected_dimension}, got {len(vector)}."
            )
    return existing


def load_embeddings(db_path: Path, owner_type: str, owner_ids: list[int], model: str) -> dict[int, list[float]]:
    if not owner_ids:
        return {}
    placeholders = ",".join("?" for _ in owner_ids)
    with connect(db_path) as conn:
        rows = conn.execute(
            f"""
            SELECT owner_id, vector_json
            FROM graph_embeddings
            WHERE owner_type = ?
              AND model = ?
              AND owner_id IN ({placeholders})
            """,
            [owner_type, model, *owner_ids],
        ).fetchall()
    result: dict[int, list[float]] = {}
    for row in rows:
        try:
            vector = json.loads(row["vector_json"])
        except json.JSONDecodeError:
            continue
        if isinstance(vector, list) and vector:
            result[int(row["owner_id"])] = [float(value) for value in vector]
    return result


def save_embeddings(
    db_path: Path,
    owner_type: str,
    model: str,
    owner_ids: list[int],
    vectors: list[list[float]],
) -> None:
    now = datetime.now().isoformat(timespec="seconds")
    with connect(db_path) as conn:
        conn.executemany(
            """
            INSERT INTO graph_embeddings (owner_type, owner_id, model, vector_json, created_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(owner_type, owner_id, model) DO UPDATE SET
              vector_json = excluded.vector_json,
              created_at = excluded.created_at
            """,
            [
                (owner_type, owner_id, model, json.dumps(vector), now)
                for owner_id, vector in zip(owner_ids, vectors)
            ],
        )
        conn.commit()


def course_node_embedding_text(row: sqlite3.Row) -> str:
    return "\n".join(
        part
        for part in [
            f"课程能力节点: {row['name']}",
            f"类型: {row['node_type']}",
            f"类别: {row['category'] or ''}",
            f"说明: {row['description'] or ''}",
            f"关键词: {', '.join(json_list(row['keywords_json']))}",
            f"课程证据: {row['evidence_text'] or ''}",
        ]
        if part.strip()
    )


def job_requirement_embedding_text(row: sqlite3.Row) -> str:
    return "\n".join(
        part
        for part in [
            f"岗位要求节点: {row['normalized_name'] or row['requirement_text']}",
            f"类型: {row['node_type']}",
            f"类别: {row['category'] or ''}",
            f"重要性: {row['importance'] or ''}",
            f"要求: {row['requirement_text']}",
            f"关键词: {', '.join(json_list(row['keywords_json']))}",
            f"岗位证据: {row['evidence_text'] or ''}",
        ]
        if part.strip()
    )


def json_list(value: str | None) -> list[str]:
    if not value:
        return []
    try:
        payload = json.loads(value)
    except json.JSONDecodeError:
        return []
    return [str(item) for item in payload] if isinstance(payload, list) else []


def chunked(items: list[Any], size: int):
    for index in range(0, len(items), size):
        yield items[index : index + size]
