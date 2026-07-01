from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from .embeddings import (
    embedding_status,
    embedding_model,
    ensure_course_node_embeddings,
    ensure_job_requirement_embeddings,
)
from .graph import (
    ensure_course_graph,
    ensure_job_requirement_graph,
    graph_stats,
    list_course_nodes,
    list_job_requirement_nodes,
)
from .neo4j_graph import Neo4jGraphStore, get_neo4j_config
from .storage import connect
from .syllabus import get_syllabi


def knowledge_graph_status(db_path: Path) -> dict[str, Any]:
    sqlite_stats = graph_stats(db_path)
    with connect(db_path) as conn:
        raw_row = conn.execute(
            """
            SELECT
                (SELECT count(*) FROM syllabi) AS syllabi,
                (SELECT count(*) FROM jobs) AS jobs,
                (SELECT count(*) FROM jobs WHERE coalesce(description, '') != '') AS detailed_jobs,
                (
                    SELECT count(*)
                    FROM (
                        SELECT jobs.id,
                               count(job_requirement_nodes.id) AS requirement_count,
                               sum(CASE WHEN graph_embeddings.id IS NOT NULL THEN 1 ELSE 0 END) AS embedding_count
                        FROM jobs
                        LEFT JOIN job_requirement_nodes
                          ON job_requirement_nodes.job_id = jobs.id
                        LEFT JOIN graph_embeddings
                          ON graph_embeddings.owner_type = 'job_requirement'
                         AND graph_embeddings.owner_id = job_requirement_nodes.id
                         AND graph_embeddings.model = ?
                        WHERE coalesce(jobs.description, '') != ''
                        GROUP BY jobs.id
                        HAVING requirement_count = 0 OR embedding_count < requirement_count
                    )
                ) AS pending_job_graph_jobs
            """
            ,
            (embedding_model(),),
        ).fetchone()

    config = get_neo4j_config()
    neo4j_status: dict[str, Any] = {
        "configured": bool(config),
        "connected": False,
        "database": config.database if config else "",
        "counts": {},
        "error": "",
    }
    if config:
        try:
            with Neo4jGraphStore(config) as store:
                neo4j_status["connected"] = True
                neo4j_status["counts"] = store.counts()
        except Exception as exc:
            neo4j_status["error"] = str(exc)

    return {
        "sqlite": dict(raw_row),
        "extracted": sqlite_stats,
        "embeddings": embedding_status(db_path),
        "neo4j": neo4j_status,
    }


def build_course_knowledge_graph(
    db_path: Path,
    syllabus_ids: list[int] | None = None,
    force: bool = False,
) -> dict[str, Any]:
    verify_neo4j_ready()
    ids = syllabus_ids or [int(row["id"]) for row in list_all_syllabi(db_path)]
    if not ids:
        return {"syllabus_ids": [], "course_node_count": 0, "neo4j": {}}

    for syllabus_id in ids:
        ensure_course_graph(db_path, syllabus_id, force=force)

    syllabi = get_syllabi(db_path, ids)
    course_nodes = list_course_nodes(db_path, ids)
    course_embeddings = ensure_course_node_embeddings(db_path, course_nodes, force=force)
    embedding_dimension = embedding_dimension_from_maps(course_embeddings)
    with Neo4jGraphStore() as store:
        store.ensure_vector_indexes(embedding_dimension)
        store.sync_syllabi(syllabi, course_nodes, course_embeddings=course_embeddings)
        counts = store.counts()
    return {
        "syllabus_ids": ids,
        "course_node_count": len(course_nodes),
        "embedding_count": len(course_embeddings),
        "embedding_dimension": embedding_dimension,
        "neo4j": counts,
    }


def build_job_knowledge_graph(
    db_path: Path,
    job_ids: list[int] | None = None,
    limit: int = 30,
    force: bool = False,
) -> dict[str, Any]:
    verify_neo4j_ready()
    ids = job_ids or [
        int(row["id"])
        for row in (list_recent_detailed_jobs(db_path, limit) if force else list_pending_job_graph_jobs(db_path, limit))
    ]
    if not ids:
        return {"job_ids": [], "job_count": 0, "requirement_count": 0, "neo4j": {}}

    nodes_by_job = ensure_job_requirement_graph(db_path, ids, force=force)
    jobs = load_jobs(db_path, ids)
    requirement_nodes = list_job_requirement_nodes(db_path, ids)
    requirement_embeddings = ensure_job_requirement_embeddings(db_path, requirement_nodes, force=force)
    embedding_dimension = embedding_dimension_from_maps(requirement_embeddings)
    with Neo4jGraphStore() as store:
        store.ensure_vector_indexes(embedding_dimension)
        store.sync_jobs(jobs, requirement_nodes, requirement_embeddings=requirement_embeddings)
        counts = store.counts()
    return {
        "job_ids": ids,
        "job_count": len(ids),
        "requirement_count": sum(len(nodes) for nodes in nodes_by_job.values()),
        "embedding_count": len(requirement_embeddings),
        "embedding_dimension": embedding_dimension,
        "neo4j": counts,
    }


def preview_knowledge_graph(limit: int = 120) -> dict[str, Any]:
    with Neo4jGraphStore() as store:
        return store.preview(limit=limit)


def verify_neo4j_ready() -> None:
    with Neo4jGraphStore():
        return


def embedding_dimension_from_maps(*embedding_maps: dict[int, list[float]]) -> int:
    dimensions = {len(vector) for mapping in embedding_maps for vector in mapping.values() if vector}
    if not dimensions:
        raise RuntimeError("No graph embeddings are available. Build course and job embeddings first.")
    if len(dimensions) != 1:
        raise RuntimeError(f"Graph embedding dimensions are inconsistent: {sorted(dimensions)}")
    return dimensions.pop()


def list_all_syllabi(db_path: Path) -> list[sqlite3.Row]:
    with connect(db_path) as conn:
        return conn.execute("SELECT * FROM syllabi ORDER BY id DESC").fetchall()


def list_recent_detailed_jobs(db_path: Path, limit: int) -> list[sqlite3.Row]:
    safe_limit = max(1, min(limit, 500))
    with connect(db_path) as conn:
        return conn.execute(
            """
            SELECT *
            FROM jobs
            WHERE coalesce(description, '') != ''
            ORDER BY id DESC
            LIMIT ?
            """,
            (safe_limit,),
        ).fetchall()


def list_pending_job_graph_jobs(db_path: Path, limit: int) -> list[sqlite3.Row]:
    safe_limit = max(1, min(limit, 500))
    model = embedding_model()
    with connect(db_path) as conn:
        return conn.execute(
            """
            SELECT jobs.*,
                   count(job_requirement_nodes.id) AS requirement_count,
                   sum(CASE WHEN graph_embeddings.id IS NOT NULL THEN 1 ELSE 0 END) AS embedding_count
            FROM jobs
            LEFT JOIN job_requirement_nodes
              ON job_requirement_nodes.job_id = jobs.id
            LEFT JOIN graph_embeddings
              ON graph_embeddings.owner_type = 'job_requirement'
             AND graph_embeddings.owner_id = job_requirement_nodes.id
             AND graph_embeddings.model = ?
            WHERE coalesce(jobs.description, '') != ''
            GROUP BY jobs.id
            HAVING requirement_count = 0 OR embedding_count < requirement_count
            ORDER BY jobs.id DESC
            LIMIT ?
            """,
            (model, safe_limit),
        ).fetchall()


def load_jobs(db_path: Path, job_ids: list[int]) -> list[sqlite3.Row]:
    if not job_ids:
        return []
    placeholders = ",".join("?" for _ in job_ids)
    with connect(db_path) as conn:
        return conn.execute(f"SELECT * FROM jobs WHERE id IN ({placeholders})", job_ids).fetchall()

