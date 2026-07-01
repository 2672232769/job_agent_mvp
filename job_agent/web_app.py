from __future__ import annotations

import json
import logging
import mimetypes
import os
import shutil
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .graph import ensure_course_graph, ensure_job_requirement_graph, graph_stats, match_syllabi_to_jobs_graph
from .job_discovery import build_course_job_search_plan
from .knowledge_graph import (
    build_course_knowledge_graph,
    build_job_knowledge_graph,
    knowledge_graph_status,
    list_pending_job_graph_jobs,
    verify_neo4j_ready,
    preview_knowledge_graph,
)
from .matcher import get_match_run, list_match_runs, match_syllabi_to_jobs
from .models import SearchIntent
from .neo4j_graph import Neo4jGraphStore
from .sources.job51 import Job51Source
from .sources.sample import SampleSource
from .storage import connect, export_jobs, get_job, save_jobs
from .syllabus import add_syllabus, analyze_syllabus, get_syllabus, get_syllabus_profile, list_syllabi


logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = Path(os.getenv("JOB_AGENT_DATA_DIR") or ROOT / "data").resolve()
DEFAULT_DB = DATA_DIR / "jobs.db"
WEB_DIR = ROOT / "web"
UPLOAD_DIR = DATA_DIR / "uploads"

load_dotenv(ROOT / ".env")

app = FastAPI(title="Course Job Matching Agent", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class MatchRequest(BaseModel):
    syllabus_ids: list[int] = Field(min_length=1)
    limit: int = 8
    candidate_limit: int = 60
    batch_size: int = 12


class CrawlRequest(BaseModel):
    request: str
    source: str = "51job"
    pages: int | None = None
    headless: bool = True


class CourseJobSearchPlanRequest(BaseModel):
    syllabus_ids: list[int] = Field(min_length=1)
    city: str = ""
    max_keywords: int = 8


class CourseJobCrawlRequest(CourseJobSearchPlanRequest):
    source: str = "51job"
    pages: int = 1
    headless: bool = True


class GraphJobRebuildRequest(BaseModel):
    job_ids: list[int] = Field(default_factory=list)
    limit: int = 60
    force: bool = False


class KnowledgeGraphCourseBuildRequest(BaseModel):
    syllabus_ids: list[int] = Field(default_factory=list)
    force: bool = False


class KnowledgeGraphJobBuildRequest(BaseModel):
    job_ids: list[int] = Field(default_factory=list)
    limit: int = 30
    force: bool = False


class GraphRagEvidenceRequest(BaseModel):
    syllabus_ids: list[int] = Field(min_length=1)
    candidate_limit: int = 24


KG_TASKS: dict[str, dict[str, Any]] = {}
KG_TASK_LOCK = threading.Lock()


def db_path() -> Path:
    return DEFAULT_DB


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def create_kg_task(task_type: str, total: int, params: dict[str, Any]) -> dict[str, Any]:
    task_id = uuid.uuid4().hex[:12]
    task = {
        "id": task_id,
        "type": task_type,
        "status": "queued",
        "total": total,
        "processed": 0,
        "succeeded": 0,
        "failed": 0,
        "current_job_id": None,
        "current_job_title": "",
        "message": "Task queued.",
        "errors": [],
        "result": {},
        "params": params,
        "created_at": now_iso(),
        "started_at": "",
        "updated_at": now_iso(),
        "finished_at": "",
    }
    with KG_TASK_LOCK:
        KG_TASKS[task_id] = task
    return public_kg_task(task)


def public_kg_task(task: dict[str, Any]) -> dict[str, Any]:
    return json.loads(json.dumps(task, ensure_ascii=False))


def update_kg_task(task_id: str, **updates: Any) -> dict[str, Any]:
    with KG_TASK_LOCK:
        task = KG_TASKS[task_id]
        task.update(updates)
        task["updated_at"] = now_iso()
        return public_kg_task(task)


def append_kg_task_error(task_id: str, error: dict[str, Any]) -> None:
    with KG_TASK_LOCK:
        task = KG_TASKS[task_id]
        errors = list(task.get("errors") or [])
        errors.append(error)
        task["errors"] = errors[-30:]
        task["updated_at"] = now_iso()


def run_job_knowledge_graph_task(task_id: str, job_ids: list[int], force: bool) -> None:
    result = {
        "job_ids": job_ids,
        "job_count": len(job_ids),
        "requirement_count": 0,
        "embedding_count": 0,
        "neo4j": {},
    }
    try:
        update_kg_task(
            task_id,
            status="running",
            started_at=now_iso(),
            message="Checking Neo4j before processing job graph batch.",
        )
        verify_neo4j_ready()
        if not job_ids:
            update_kg_task(
                task_id,
                status="succeeded",
                message="No detailed jobs were selected for graph construction.",
                result=result,
                finished_at=now_iso(),
            )
            return

        for index, job_id in enumerate(job_ids, start=1):
            job = get_job(db_path(), job_id)
            title = str(job["title"] if job else f"Job #{job_id}")
            company = str(job["company"] if job else "")
            label = f"{title} / {company}".strip(" /")
            update_kg_task(
                task_id,
                current_job_id=job_id,
                current_job_title=label,
                message=f"Processing job {index}/{len(job_ids)}: {label}",
            )
            try:
                payload = build_job_knowledge_graph(db_path(), job_ids=[job_id], force=force)
                result["requirement_count"] += int(payload.get("requirement_count") or 0)
                result["embedding_count"] += int(payload.get("embedding_count") or 0)
                result["neo4j"] = payload.get("neo4j") or result["neo4j"]
                with KG_TASK_LOCK:
                    succeeded_count = int(KG_TASKS[task_id].get("succeeded") or 0) + 1
                update_kg_task(
                    task_id,
                    processed=index,
                    succeeded=succeeded_count,
                    result=result,
                )
            except Exception as exc:
                logger.exception("Job knowledge graph task failed for job #%s", job_id)
                append_kg_task_error(task_id, {"job_id": job_id, "job": label, "error": str(exc)})
                with KG_TASK_LOCK:
                    failed_count = int(KG_TASKS[task_id].get("failed") or 0) + 1
                update_kg_task(
                    task_id,
                    processed=index,
                    failed=failed_count,
                    result=result,
                )

        with KG_TASK_LOCK:
            task = KG_TASKS[task_id]
            failed = int(task.get("failed") or 0)
            succeeded = int(task.get("succeeded") or 0)
        status = "completed_with_errors" if failed else "succeeded"
        if succeeded == 0 and failed:
            status = "failed"
        update_kg_task(
            task_id,
            status=status,
            current_job_id=None,
            current_job_title="",
            message=(
                f"Job graph batch finished: {succeeded} succeeded, {failed} failed, "
                f"{result['requirement_count']} requirement nodes, {result['embedding_count']} embeddings."
            ),
            result=result,
            finished_at=now_iso(),
        )
    except Exception as exc:
        logger.exception("Job knowledge graph task failed before processing")
        update_kg_task(
            task_id,
            status="failed",
            message=str(exc),
            result=result,
            finished_at=now_iso(),
        )


def json_loads(value: str | None, fallback: Any) -> Any:
    if not value:
        return fallback
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return fallback


def syllabus_row_to_dict(row, include_raw: bool = False) -> dict[str, Any]:
    item = {
        "id": row["id"],
        "title": row["title"],
        "file_name": row["file_name"],
        "file_type": row["file_type"],
        "text_length": row["text_length"] if "text_length" in row.keys() else len(row["raw_text"]),
        "updated_at": row["updated_at"],
        "file_url": f"/api/syllabi/{row['id']}/file",
        "text_url": f"/api/syllabi/{row['id']}/text",
    }
    if include_raw:
        item.update(
            {
                "source_path": row["source_path"],
                "stored_path": row["stored_path"],
                "raw_text": row["raw_text"],
                "created_at": row["created_at"],
            }
        )
    return item


def syllabus_link_row_to_dict(row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "title": row["title"],
        "file_name": row["file_name"],
        "file_type": row["file_type"],
        "text_length": row["text_length"] if "text_length" in row.keys() else len(row["raw_text"]),
        "file_url": f"/api/syllabi/{row['id']}/file",
        "text_url": f"/api/syllabi/{row['id']}/text",
    }


def stored_syllabus_file(row) -> Path | None:
    stored_path = Path(str(row["stored_path"] or "")).resolve()
    allowed_root = (ROOT / "data" / "syllabi_files").resolve()
    if stored_path.exists() and stored_path.is_file() and allowed_root in stored_path.parents:
        return stored_path
    return None


def profile_row_to_dict(row) -> dict[str, Any] | None:
    if not row:
        return None
    return {
        "id": row["id"],
        "syllabus_id": row["syllabus_id"],
        "summary": row["summary"],
        "knowledge_points": json_loads(row["knowledge_points_json"], []),
        "abilities": json_loads(row["abilities_json"], []),
        "technologies_tools_methods": json_loads(row["technologies_tools_methods_json"], []),
        "job_directions": json_loads(row["job_directions_json"], []),
        "profile": json_loads(row["profile_json"], {}),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def job_row_to_dict(row, include_detail: bool = False) -> dict[str, Any]:
    item = {
        "id": row["id"],
        "source": row["source"],
        "title": row["title"],
        "company": row["company"],
        "city": row["city"],
        "salary": row["salary"],
        "education": row["education"] if "education" in row.keys() else None,
        "experience": row["experience"] if "experience" in row.keys() else None,
        "publish_date": row["publish_date"] if "publish_date" in row.keys() else None,
        "url": row["url"] if "url" in row.keys() else None,
    }
    if include_detail:
        item.update(
            {
                "description": row["description"],
                "address": row["address"],
                "industry": row["industry"],
                "company_size": row["company_size"],
                "tags": json_loads(row["tags_json"], []),
                "raw": json_loads(row["raw_json"], {}),
                "crawled_at": row["crawled_at"],
            }
        )
    return item


def enrich_match_result(result: dict[str, Any]) -> dict[str, Any]:
    syllabus_ids = result.get("syllabus_ids")
    if isinstance(syllabus_ids, list) and syllabus_ids:
        placeholders = ",".join("?" for _ in syllabus_ids)
        with connect(db_path()) as conn:
            rows = conn.execute(f"SELECT * FROM syllabi WHERE id IN ({placeholders})", syllabus_ids).fetchall()
        rows_by_id = {int(row["id"]): row for row in rows}
        result["syllabi"] = [
            syllabus_link_row_to_dict(rows_by_id[int(syllabus_id)])
            for syllabus_id in syllabus_ids
            if str(syllabus_id).isdigit() and int(syllabus_id) in rows_by_id
        ]

    matches = result.get("matches")
    if not isinstance(matches, list) or not matches:
        return result

    job_ids = []
    for item in matches:
        try:
            job_ids.append(int(item.get("job_id")))
        except Exception:
            continue
    if not job_ids:
        return result

    placeholders = ",".join("?" for _ in job_ids)
    with connect(db_path()) as conn:
        rows = conn.execute(f"SELECT * FROM jobs WHERE id IN ({placeholders})", job_ids).fetchall()
    jobs_by_id = {int(row["id"]): job_row_to_dict(row, include_detail=True) for row in rows}

    for item in matches:
        try:
            job_id = int(item.get("job_id"))
        except Exception:
            continue
        job = jobs_by_id.get(job_id)
        if not job:
            continue
        item["job_url"] = job.get("url")
        item["city"] = job.get("city")
        item["salary"] = job.get("salary")
        item["education"] = job.get("education")
        item["experience"] = job.get("experience")
        item["job_detail"] = job
    return result


@app.get("/api/health")
def health() -> dict[str, Any]:
    return {"ok": True}


@app.get("/api/stats")
def stats() -> dict[str, Any]:
    with connect(db_path()) as conn:
        row = conn.execute(
            """
            SELECT
                (SELECT count(*) FROM syllabi) AS syllabus_count,
                (SELECT count(*) FROM syllabus_profiles) AS profile_count,
                (SELECT count(*) FROM jobs) AS job_count,
                (SELECT count(*) FROM jobs WHERE coalesce(description, '') != '') AS detailed_job_count,
                (SELECT count(*) FROM match_runs) AS match_run_count
            """
        ).fetchone()
    payload = dict(row)
    payload.update(graph_stats(db_path()))
    return payload


@app.get("/api/syllabi")
def api_list_syllabi() -> dict[str, Any]:
    rows = list_syllabi(db_path())
    items = []
    for row in rows:
        item = syllabus_row_to_dict(row)
        item["profile"] = profile_row_to_dict(get_syllabus_profile(db_path(), int(row["id"])))
        items.append(item)
    return {"items": items}


@app.get("/api/syllabi/{syllabus_id}")
def api_get_syllabus(syllabus_id: int) -> dict[str, Any]:
    row = get_syllabus(db_path(), syllabus_id)
    if not row:
        raise HTTPException(status_code=404, detail="Syllabus not found")
    item = syllabus_row_to_dict(row, include_raw=True)
    item["profile"] = profile_row_to_dict(get_syllabus_profile(db_path(), syllabus_id))
    return item


@app.get("/api/syllabi/{syllabus_id}/file")
def api_get_syllabus_file(syllabus_id: int):
    row = get_syllabus(db_path(), syllabus_id)
    if not row:
        raise HTTPException(status_code=404, detail="Syllabus not found")
    stored_path = stored_syllabus_file(row)
    if stored_path:
        media_type = mimetypes.guess_type(str(stored_path))[0] or "application/octet-stream"
        encoded_name = quote(str(row["file_name"]))
        return FileResponse(
            stored_path,
            media_type=media_type,
            headers={"Content-Disposition": f"inline; filename*=UTF-8''{encoded_name}"},
        )
    return PlainTextResponse(str(row["raw_text"] or ""), media_type="text/plain; charset=utf-8")


@app.get("/api/syllabi/{syllabus_id}/text")
def api_get_syllabus_text(syllabus_id: int) -> PlainTextResponse:
    row = get_syllabus(db_path(), syllabus_id)
    if not row:
        raise HTTPException(status_code=404, detail="Syllabus not found")
    return PlainTextResponse(str(row["raw_text"] or ""), media_type="text/plain; charset=utf-8")


@app.post("/api/syllabi")
def api_upload_syllabus(
    file: UploadFile = File(...),
    title: str | None = Form(default=None),
    analyze: bool = Form(default=True),
) -> dict[str, Any]:
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    suffix = Path(file.filename or "syllabus").suffix
    safe_name = f"{Path(file.filename or 'syllabus').stem[:80]}{suffix}"
    target = UPLOAD_DIR / safe_name
    counter = 1
    while target.exists():
        target = UPLOAD_DIR / f"{Path(safe_name).stem}_{counter}{suffix}"
        counter += 1

    with target.open("wb") as f:
        shutil.copyfileobj(file.file, f)

    try:
        row = add_syllabus(db_path(), ROOT, target, title=title)
        profile = analyze_syllabus(db_path(), int(row["id"])) if analyze else None
        if analyze:
            ensure_course_graph(db_path(), int(row["id"]), force=True)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    item = syllabus_row_to_dict(row, include_raw=True)
    item["profile"] = profile_row_to_dict(profile or get_syllabus_profile(db_path(), int(row["id"])))
    return item


@app.post("/api/syllabi/{syllabus_id}/analyze")
def api_analyze_syllabus(syllabus_id: int) -> dict[str, Any]:
    try:
        profile = analyze_syllabus(db_path(), syllabus_id)
        ensure_course_graph(db_path(), syllabus_id, force=True)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"profile": profile_row_to_dict(profile)}


@app.get("/api/jobs")
def api_list_jobs(
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    q: str = "",
) -> dict[str, Any]:
    where = ""
    params: list[Any] = []
    if q.strip():
        where = """
        WHERE title LIKE ? OR company LIKE ? OR city LIKE ? OR description LIKE ?
        """
        like = f"%{q.strip()}%"
        params.extend([like, like, like, like])
    params.extend([limit, offset])
    with connect(db_path()) as conn:
        rows = conn.execute(
            f"""
            SELECT id, source, title, company, city, salary, education, experience, publish_date, url
            FROM jobs
            {where}
            ORDER BY id DESC
            LIMIT ? OFFSET ?
            """,
            params,
        ).fetchall()
        total_params = params[:-2]
        total = conn.execute(f"SELECT count(*) AS c FROM jobs {where}", total_params).fetchone()["c"]
    return {"items": [job_row_to_dict(row) for row in rows], "total": total}


@app.get("/api/jobs/export")
def api_export_jobs() -> JSONResponse:
    return JSONResponse(export_jobs(db_path()))


@app.get("/api/jobs/{job_id}")
def api_get_job(job_id: int) -> dict[str, Any]:
    row = get_job(db_path(), job_id)
    if not row:
        raise HTTPException(status_code=404, detail="Job not found")
    return job_row_to_dict(row, include_detail=True)


@app.post("/api/jobs/crawl")
def api_crawl_jobs(request: CrawlRequest) -> dict[str, Any]:
    intent = build_exact_keyword_intent(request)
    if request.pages:
        intent.pages = max(1, min(request.pages, 10))

    source = build_job_source(request.source, request.headless)

    try:
        jobs = source.search(intent)
        inserted, skipped = save_jobs(db_path(), jobs)
    except Exception as exc:
        logger.exception("Job crawl failed for source=%s request=%r", request.source, request.request)
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "fetched": len(jobs),
        "inserted": inserted,
        "skipped_duplicates": skipped,
        "warnings": getattr(source, "warnings", []),
    }


def build_exact_keyword_intent(request: CrawlRequest) -> SearchIntent:
    raw = request.request.strip()
    if not raw:
        raise HTTPException(status_code=400, detail="请输入岗位关键词。")
    keywords = [part.strip() for part in raw.replace("，", ",").replace("、", ",").split(",") if part.strip()]
    return SearchIntent(
        raw_text=raw,
        keywords=keywords or [raw],
        pages=max(1, min(request.pages or 1, 10)),
    )


@app.post("/api/jobs/search-plan")
def api_build_job_search_plan(request: CourseJobSearchPlanRequest) -> dict[str, Any]:
    try:
        return build_course_job_search_plan(
            db_path(),
            syllabus_ids=request.syllabus_ids,
            city=request.city.strip(),
            max_keywords=max(1, min(request.max_keywords, 12)),
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/jobs/crawl-from-courses")
def api_crawl_jobs_from_courses(request: CourseJobCrawlRequest) -> dict[str, Any]:
    try:
        plan = build_course_job_search_plan(
            db_path(),
            syllabus_ids=request.syllabus_ids,
            city=request.city.strip(),
            max_keywords=max(1, min(request.max_keywords, 12)),
        )
        if not plan["keywords"]:
            raise ValueError("No job search keywords generated from selected courses.")
        source = build_job_source(request.source, request.headless)
        intent = SearchIntent(
            raw_text=f"按课程智能抓取岗位：{', '.join(plan['keywords'])}",
            keywords=plan["keywords"],
            city=request.city.strip(),
            pages=max(1, min(request.pages, 10)),
        )
        jobs = source.search(intent)
        inserted, skipped = save_jobs(db_path(), jobs)
    except Exception as exc:
        logger.exception(
            "Course-based job crawl failed for source=%s syllabus_ids=%s",
            request.source,
            request.syllabus_ids,
        )
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "plan": plan,
        "fetched": len(jobs),
        "inserted": inserted,
        "skipped_duplicates": skipped,
        "warnings": getattr(source, "warnings", []),
    }


def build_job_source(source_name: str, headless: bool):
    if source_name == "51job":
        return Job51Source(root=ROOT, headless=headless)
    if source_name == "sample":
        return SampleSource()
    raise HTTPException(status_code=400, detail="Web API currently supports 51job and sample crawling.")


@app.post("/api/match")
def api_match_jobs(request: MatchRequest) -> dict[str, Any]:
    try:
        result = match_syllabi_to_jobs_graph(
            db_path(),
            syllabus_ids=request.syllabus_ids,
            limit=request.limit,
            candidate_limit=request.candidate_limit,
        )
        return enrich_match_result(result)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/match/legacy")
def api_match_jobs_legacy(request: MatchRequest) -> dict[str, Any]:
    try:
        result = match_syllabi_to_jobs(
            db_path(),
            syllabus_ids=request.syllabus_ids,
            limit=request.limit,
            candidate_limit=request.candidate_limit,
            batch_size=request.batch_size,
        )
        return enrich_match_result(result)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/graphrag/evidence")
def api_graphrag_evidence(request: GraphRagEvidenceRequest) -> dict[str, Any]:
    try:
        with Neo4jGraphStore() as store:
            packages = store.retrieve_course_job_evidence(
                request.syllabus_ids,
                candidate_limit=max(1, min(request.candidate_limit, 100)),
            )
        return {"items": packages, "count": len(packages)}
    except Exception as exc:
        logger.exception("GraphRAG evidence preview failed")
        raise HTTPException(
            status_code=400,
            detail="Neo4j knowledge graph is not available. Start Neo4j and build/sync the knowledge graph before previewing GraphRAG evidence.",
        ) from exc


@app.post("/api/graph/syllabi/{syllabus_id}/rebuild")
def api_rebuild_course_graph(syllabus_id: int) -> dict[str, Any]:
    try:
        nodes = ensure_course_graph(db_path(), syllabus_id, force=True)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"syllabus_id": syllabus_id, "node_count": len(nodes)}


@app.post("/api/graph/jobs/rebuild")
def api_rebuild_job_graph(request: GraphJobRebuildRequest) -> dict[str, Any]:
    job_ids = request.job_ids
    if not job_ids:
        rows = load_recent_detailed_jobs_for_api(request.limit)
        job_ids = [int(row["id"]) for row in rows]
    try:
        nodes_by_job = ensure_job_requirement_graph(db_path(), job_ids, force=request.force)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "job_ids": job_ids,
        "job_count": len(job_ids),
        "requirement_count": sum(len(nodes) for nodes in nodes_by_job.values()),
    }


@app.get("/api/knowledge-graph/status")
def api_knowledge_graph_status() -> dict[str, Any]:
    return knowledge_graph_status(db_path())


@app.post("/api/knowledge-graph/build-courses")
def api_build_course_knowledge_graph(request: KnowledgeGraphCourseBuildRequest) -> dict[str, Any]:
    try:
        return build_course_knowledge_graph(
            db_path(),
            syllabus_ids=request.syllabus_ids or None,
            force=request.force,
        )
    except Exception as exc:
        logger.exception("Course knowledge graph build failed")
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/knowledge-graph/build-jobs")
def api_build_job_knowledge_graph(request: KnowledgeGraphJobBuildRequest) -> dict[str, Any]:
    try:
        return build_job_knowledge_graph(
            db_path(),
            job_ids=request.job_ids or None,
            limit=request.limit,
            force=request.force,
        )
    except Exception as exc:
        logger.exception("Job knowledge graph build failed")
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/knowledge-graph/build-jobs/task")
def api_start_job_knowledge_graph_task(request: KnowledgeGraphJobBuildRequest) -> dict[str, Any]:
    job_ids = request.job_ids
    if not job_ids:
        rows = load_recent_detailed_jobs_for_api(request.limit) if request.force else list_pending_job_graph_jobs(db_path(), request.limit)
        job_ids = [int(row["id"]) for row in rows]

    task = create_kg_task(
        "job_knowledge_graph_build",
        total=len(job_ids),
        params={
            "job_ids": job_ids,
            "limit": request.limit,
            "force": request.force,
            "selection": "recent_detailed_jobs" if request.force else "pending_job_graph_jobs",
        },
    )
    thread = threading.Thread(
        target=run_job_knowledge_graph_task,
        args=(task["id"], job_ids, request.force),
        name=f"kg-job-build-{task['id']}",
        daemon=True,
    )
    thread.start()
    return task


@app.get("/api/knowledge-graph/tasks/{task_id}")
def api_get_knowledge_graph_task(task_id: str) -> dict[str, Any]:
    with KG_TASK_LOCK:
        task = KG_TASKS.get(task_id)
        if not task:
            raise HTTPException(status_code=404, detail="Knowledge graph task not found.")
        return public_kg_task(task)


@app.get("/api/knowledge-graph/preview")
def api_preview_knowledge_graph(limit: int = Query(default=120, ge=1, le=500)) -> dict[str, Any]:
    try:
        return preview_knowledge_graph(limit=limit)
    except Exception as exc:
        logger.exception("Knowledge graph preview failed")
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def load_recent_detailed_jobs_for_api(limit: int) -> list[Any]:
    safe_limit = max(1, min(limit, 300))
    with connect(db_path()) as conn:
        return conn.execute(
            """
            SELECT id
            FROM jobs
            WHERE coalesce(description, '') != ''
            ORDER BY id DESC
            LIMIT ?
            """,
            (safe_limit,),
        ).fetchall()


@app.get("/api/match-runs")
def api_list_match_runs(limit: int = Query(default=20, ge=1, le=100)) -> dict[str, Any]:
    rows = list_match_runs(db_path(), limit=limit)
    items = []
    for row in rows:
        result = json_loads(row["result_json"], {})
        result = enrich_match_result(result)
        items.append(
            {
                "id": row["id"],
                "syllabus_ids": json_loads(row["syllabus_ids_json"], []),
                "match_count": len(result.get("matches", [])),
                "created_at": row["created_at"],
                "result": result,
            }
        )
    return {"items": items}


@app.get("/api/match-runs/{run_id}")
def api_get_match_run(run_id: int) -> dict[str, Any]:
    row = get_match_run(db_path(), run_id)
    if not row:
        raise HTTPException(status_code=404, detail="Match run not found")
    return {
        "id": row["id"],
        "syllabus_ids": json_loads(row["syllabus_ids_json"], []),
        "candidate_job_ids": json_loads(row["candidate_job_ids_json"], []),
        "result": enrich_match_result(json_loads(row["result_json"], {})),
        "created_at": row["created_at"],
    }


if WEB_DIR.exists():
    app.mount("/assets", StaticFiles(directory=WEB_DIR), name="assets")


@app.get("/")
def index() -> FileResponse:
    return FileResponse(WEB_DIR / "index.html")
