from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from job_agent.embeddings import JOB_REQUIREMENT_OWNER, embedding_model, load_embeddings
from job_agent.neo4j_graph import Neo4jGraphStore
from job_agent.storage import normalize_job_url


def main() -> None:
    args = parse_args()
    db_path = args.db.resolve()
    if not db_path.exists():
        raise SystemExit(f"Database not found: {db_path}")

    summary = cleanup_sqlite(db_path, dry_run=args.dry_run)
    if args.sync_neo4j and not args.dry_run:
        load_env_file(PROJECT_ROOT / ".env")
        summary["neo4j"] = rebuild_neo4j_job_subgraph(db_path)

    print(json.dumps(summary, ensure_ascii=False, indent=2))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Normalize job URLs, merge duplicate job rows, and optionally rebuild the Neo4j job subgraph."
    )
    parser.add_argument("--db", type=Path, default=PROJECT_ROOT / "data" / "jobs.db")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--sync-neo4j",
        action="store_true",
        help="After SQLite cleanup, clear and rebuild Job/Company/JobRequirement nodes in Neo4j from SQLite.",
    )
    return parser.parse_args()


def connect_raw(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def cleanup_sqlite(db_path: Path, dry_run: bool = False) -> dict[str, Any]:
    with connect_raw(db_path) as conn:
        conn.execute("BEGIN")
        rows = conn.execute("SELECT * FROM jobs ORDER BY id").fetchall()
        groups: dict[tuple[Any, ...], list[sqlite3.Row]] = defaultdict(list)
        for row in rows:
            groups[job_identity_key(row)].append(row)

        duplicate_groups = [group for group in groups.values() if len(group) > 1]
        id_map: dict[int, int] = {}
        removed = 0
        merged_groups = []

        for group in duplicate_groups:
            canonical = choose_canonical_job(conn, group)
            canonical_id = int(canonical["id"])
            duplicate_ids = [int(row["id"]) for row in group if int(row["id"]) != canonical_id]
            for duplicate_id in duplicate_ids:
                id_map[duplicate_id] = canonical_id
            if not dry_run:
                merge_job_group(conn, canonical, group)
            removed += len(duplicate_ids)
            merged_groups.append(
                {
                    "canonical_id": canonical_id,
                    "duplicate_ids": duplicate_ids,
                    "url": normalize_job_url(canonical["url"]),
                    "title": canonical["title"],
                    "company": canonical["company"],
                }
            )

        normalized_updates = 0
        if not dry_run:
            for row in rows:
                if int(row["id"]) in id_map:
                    continue
                normalized_url = normalize_job_url(row["url"])
                if normalized_url != (row["url"] or ""):
                    conn.execute("UPDATE jobs SET url = ? WHERE id = ?", (normalized_url, int(row["id"])))
                    normalized_updates += 1
            update_match_runs(conn, id_map)
            conn.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_jobs_source_url_unique
                ON jobs(source, url)
                WHERE coalesce(url, '') != ''
                """
            )
            conn.commit()
        else:
            conn.rollback()

        remaining = conn.execute("SELECT count(*) AS value FROM jobs").fetchone()["value"] if not dry_run else len(rows)

    return {
        "dry_run": dry_run,
        "duplicate_groups": len(duplicate_groups),
        "removed_duplicate_rows": removed,
        "normalized_url_updates": normalized_updates,
        "id_map_size": len(id_map),
        "remaining_jobs": int(remaining),
        "examples": merged_groups[:10],
    }


def job_identity_key(row: sqlite3.Row) -> tuple[Any, ...]:
    normalized_url = normalize_job_url(row["url"])
    if normalized_url:
        return ("url", row["source"], normalized_url)
    return (
        "text",
        row["source"],
        normalize_text(row["title"]),
        normalize_text(row["company"]),
        normalize_text(row["city"]),
    )


def normalize_text(value: str | None) -> str:
    return " ".join((value or "").strip().lower().split())


def choose_canonical_job(conn: sqlite3.Connection, rows: list[sqlite3.Row]) -> sqlite3.Row:
    return max(rows, key=lambda row: canonical_score(conn, row))


def canonical_score(conn: sqlite3.Connection, row: sqlite3.Row) -> tuple[int, int, str, int]:
    job_id = int(row["id"])
    requirement_count = conn.execute(
        "SELECT count(*) AS value FROM job_requirement_nodes WHERE job_id = ?",
        (job_id,),
    ).fetchone()["value"]
    edge_count = conn.execute(
        "SELECT count(*) AS value FROM graph_match_edges WHERE job_id = ?",
        (job_id,),
    ).fetchone()["value"]
    run_count = conn.execute(
        "SELECT count(*) AS value FROM graph_extraction_runs WHERE source_type = 'job' AND source_id = ?",
        (job_id,),
    ).fetchone()["value"]
    description_len = len(row["description"] or "")
    crawled_at = row["crawled_at"] or ""
    return (int(requirement_count or 0) + int(edge_count or 0) + int(run_count or 0), description_len, crawled_at, -job_id)


def merge_job_group(conn: sqlite3.Connection, canonical: sqlite3.Row, rows: list[sqlite3.Row]) -> None:
    canonical_id = int(canonical["id"])
    duplicate_ids = [int(row["id"]) for row in rows if int(row["id"]) != canonical_id]
    best = merged_job_values(canonical, rows)
    conn.execute(
        """
        UPDATE jobs
        SET title = ?, company = ?, city = ?, salary = ?, description = ?, url = ?,
            address = ?, education = ?, experience = ?, industry = ?, company_size = ?,
            tags_json = ?, publish_date = ?, crawled_at = ?, raw_json = ?
        WHERE id = ?
        """,
        (
            best["title"],
            best["company"],
            best["city"],
            best["salary"],
            best["description"],
            best["url"],
            best["address"],
            best["education"],
            best["experience"],
            best["industry"],
            best["company_size"],
            best["tags_json"],
            best["publish_date"],
            best["crawled_at"],
            best["raw_json"],
            canonical_id,
        ),
    )

    for duplicate_id in duplicate_ids:
        migrate_requirements(conn, duplicate_id, canonical_id)
        conn.execute(
            "UPDATE graph_extraction_runs SET source_id = ? WHERE source_type = 'job' AND source_id = ?",
            (canonical_id, duplicate_id),
        )
        conn.execute("UPDATE graph_match_edges SET job_id = ? WHERE job_id = ?", (canonical_id, duplicate_id))
        conn.execute("DELETE FROM jobs WHERE id = ?", (duplicate_id,))


def merged_job_values(canonical: sqlite3.Row, rows: list[sqlite3.Row]) -> dict[str, Any]:
    result = dict(canonical)
    result["url"] = normalize_job_url(canonical["url"])
    for row in rows:
        normalized_url = normalize_job_url(row["url"])
        if normalized_url:
            result["url"] = normalized_url
            break

    longest_columns = [
        "title",
        "company",
        "city",
        "salary",
        "description",
        "address",
        "education",
        "experience",
        "industry",
        "company_size",
        "tags_json",
        "publish_date",
        "raw_json",
    ]
    for column in longest_columns:
        result[column] = max_text(result.get(column), *(row[column] for row in rows))
    result["crawled_at"] = max(row["crawled_at"] or "" for row in rows)
    return result


def max_text(*values: Any) -> str:
    candidates = [str(value) for value in values if value not in (None, "")]
    return max(candidates, key=len) if candidates else ""


def migrate_requirements(conn: sqlite3.Connection, duplicate_job_id: int, canonical_job_id: int) -> None:
    rows = conn.execute(
        "SELECT * FROM job_requirement_nodes WHERE job_id = ? ORDER BY id",
        (duplicate_job_id,),
    ).fetchall()
    for row in rows:
        existing = conn.execute(
            """
            SELECT *
            FROM job_requirement_nodes
            WHERE job_id = ?
              AND node_type = ?
              AND requirement_text = ?
            """,
            (canonical_job_id, row["node_type"], row["requirement_text"]),
        ).fetchone()
        if existing:
            merge_requirement_node(conn, int(existing["id"]), row)
            migrate_edges_for_requirement(conn, int(row["id"]), int(existing["id"]), canonical_job_id)
            migrate_embedding(conn, "job_requirement", int(row["id"]), int(existing["id"]))
            conn.execute("DELETE FROM job_requirement_nodes WHERE id = ?", (int(row["id"]),))
        else:
            conn.execute(
                "UPDATE job_requirement_nodes SET job_id = ? WHERE id = ?",
                (canonical_job_id, int(row["id"])),
            )
            conn.execute(
                "UPDATE graph_match_edges SET job_id = ? WHERE job_requirement_id = ?",
                (canonical_job_id, int(row["id"])),
            )


def merge_requirement_node(conn: sqlite3.Connection, target_id: int, duplicate: sqlite3.Row) -> None:
    target = conn.execute("SELECT * FROM job_requirement_nodes WHERE id = ?", (target_id,)).fetchone()
    values = {
        "normalized_name": max_text(target["normalized_name"], duplicate["normalized_name"]),
        "category": max_text(target["category"], duplicate["category"]),
        "importance": max_text(target["importance"], duplicate["importance"]),
        "keywords_json": merge_json_lists(target["keywords_json"], duplicate["keywords_json"]),
        "evidence_text": max_text(target["evidence_text"], duplicate["evidence_text"]),
        "evidence_locator_json": max_text(target["evidence_locator_json"], duplicate["evidence_locator_json"]),
        "metadata_json": max_text(target["metadata_json"], duplicate["metadata_json"]),
        "updated_at": max(target["updated_at"] or "", duplicate["updated_at"] or ""),
    }
    conn.execute(
        """
        UPDATE job_requirement_nodes
        SET normalized_name = ?, category = ?, importance = ?, keywords_json = ?,
            evidence_text = ?, evidence_locator_json = ?, metadata_json = ?, updated_at = ?
        WHERE id = ?
        """,
        (
            values["normalized_name"],
            values["category"],
            values["importance"],
            values["keywords_json"],
            values["evidence_text"],
            values["evidence_locator_json"],
            values["metadata_json"],
            values["updated_at"],
            target_id,
        ),
    )


def merge_json_lists(left: str | None, right: str | None) -> str:
    result = []
    for value in (left, right):
        try:
            payload = json.loads(value or "[]")
        except json.JSONDecodeError:
            payload = []
        if isinstance(payload, list):
            for item in payload:
                text = str(item)
                if text not in result:
                    result.append(text)
    return json.dumps(result, ensure_ascii=False)


def migrate_edges_for_requirement(
    conn: sqlite3.Connection,
    old_requirement_id: int,
    new_requirement_id: int,
    canonical_job_id: int,
) -> None:
    edges = conn.execute(
        "SELECT * FROM graph_match_edges WHERE job_requirement_id = ?",
        (old_requirement_id,),
    ).fetchall()
    for edge in edges:
        existing = conn.execute(
            """
            SELECT *
            FROM graph_match_edges
            WHERE course_node_id = ?
              AND job_requirement_id = ?
              AND relation_type = ?
            """,
            (edge["course_node_id"], new_requirement_id, edge["relation_type"]),
        ).fetchone()
        if existing:
            conn.execute(
                """
                UPDATE graph_match_edges
                SET rationale = ?, course_evidence = ?, job_evidence = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    max_text(existing["rationale"], edge["rationale"]),
                    max_text(existing["course_evidence"], edge["course_evidence"]),
                    max_text(existing["job_evidence"], edge["job_evidence"]),
                    max(existing["updated_at"] or "", edge["updated_at"] or ""),
                    int(existing["id"]),
                ),
            )
            conn.execute("DELETE FROM graph_match_edges WHERE id = ?", (int(edge["id"]),))
        else:
            conn.execute(
                """
                UPDATE graph_match_edges
                SET job_id = ?, job_requirement_id = ?
                WHERE id = ?
                """,
                (canonical_job_id, new_requirement_id, int(edge["id"])),
            )


def migrate_embedding(conn: sqlite3.Connection, owner_type: str, old_owner_id: int, new_owner_id: int) -> None:
    rows = conn.execute(
        "SELECT * FROM graph_embeddings WHERE owner_type = ? AND owner_id = ?",
        (owner_type, old_owner_id),
    ).fetchall()
    for row in rows:
        existing = conn.execute(
            """
            SELECT id
            FROM graph_embeddings
            WHERE owner_type = ?
              AND owner_id = ?
              AND model = ?
            """,
            (owner_type, new_owner_id, row["model"]),
        ).fetchone()
        if existing:
            conn.execute("DELETE FROM graph_embeddings WHERE id = ?", (int(row["id"]),))
        else:
            conn.execute("UPDATE graph_embeddings SET owner_id = ? WHERE id = ?", (new_owner_id, int(row["id"])))


def update_match_runs(conn: sqlite3.Connection, id_map: dict[int, int]) -> None:
    if not id_map:
        return
    rows = conn.execute("SELECT * FROM match_runs ORDER BY id").fetchall()
    for row in rows:
        candidate_ids = replace_json_job_ids(row["candidate_job_ids_json"], id_map)
        result_json = replace_json_job_ids(row["result_json"], id_map)
        conn.execute(
            """
            UPDATE match_runs
            SET candidate_job_ids_json = ?, result_json = ?
            WHERE id = ?
            """,
            (candidate_ids, result_json, int(row["id"])),
        )


def replace_json_job_ids(value: str, id_map: dict[int, int]) -> str:
    try:
        payload = json.loads(value or "null")
    except json.JSONDecodeError:
        return value
    replaced = replace_job_ids(payload, id_map)
    return json.dumps(replaced, ensure_ascii=False)


def replace_job_ids(value: Any, id_map: dict[int, int]) -> Any:
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            if key == "job_id" and isinstance(item, int):
                result[key] = id_map.get(item, item)
            else:
                result[key] = replace_job_ids(item, id_map)
        return result
    if isinstance(value, list):
        result = [replace_job_ids(item, id_map) for item in value]
        if all(isinstance(item, int) for item in result):
            deduped = []
            for item in result:
                mapped = id_map.get(item, item)
                if mapped not in deduped:
                    deduped.append(mapped)
            return deduped
        return result
    if isinstance(value, int):
        return id_map.get(value, value)
    return value


def rebuild_neo4j_job_subgraph(db_path: Path) -> dict[str, Any]:
    with connect_raw(db_path) as conn:
        jobs = conn.execute("SELECT * FROM jobs ORDER BY id").fetchall()
        requirement_nodes = conn.execute("SELECT * FROM job_requirement_nodes ORDER BY job_id, id").fetchall()
        match_edges = conn.execute("SELECT * FROM graph_match_edges ORDER BY id").fetchall()
    requirement_ids = [int(row["id"]) for row in requirement_nodes]
    requirement_embeddings = load_embeddings(db_path, JOB_REQUIREMENT_OWNER, requirement_ids, embedding_model())
    with Neo4jGraphStore() as store:
        if requirement_embeddings:
            dimension = len(next(iter(requirement_embeddings.values())))
            store.ensure_vector_indexes(dimension)
        store.clear_job_subgraph()
        store.sync_jobs(jobs, requirement_nodes, requirement_embeddings)
        store.sync_match_edges(match_edges)
        counts = store.counts()
    return {
        "jobs_synced": len(jobs),
        "job_requirements_synced": len(requirement_nodes),
        "match_edges_synced": len(match_edges),
        "neo4j_counts": counts,
    }


def load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


if __name__ == "__main__":
    main()
