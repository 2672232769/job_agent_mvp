from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from dotenv import load_dotenv

from .auth import login_with_browser, storage_state_path
from .graph import match_syllabi_to_jobs_graph
from .intent import parse_intent
from .matcher import get_match_run, list_match_runs, match_syllabi_to_jobs
from .sources.boss import BossSource
from .sources.job51 import Job51Source
from .sources.liepin import LiepinSource
from .sources.sample import SampleSource
from .storage import connect, export_jobs, get_job, list_jobs, save_jobs
from .syllabus import add_syllabus, analyze_syllabus, get_syllabus, get_syllabus_profile, list_syllabi


ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = Path(os.getenv("JOB_AGENT_DATA_DIR") or ROOT / "data").resolve()
DEFAULT_DB = DATA_DIR / "jobs.db"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Course syllabus and job-pool matching agent")
    parser.add_argument("--db", default=str(DEFAULT_DB), help="SQLite database path")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init-db", help="Create or migrate database")

    auth = sub.add_parser("auth", help="Manage platform login state")
    auth_sub = auth.add_subparsers(dest="auth_command", required=True)
    login = auth_sub.add_parser("login", help="Open browser, log in manually, and save storage state")
    login.add_argument("platform", choices=["boss", "51job", "liepin"])
    status = auth_sub.add_parser("status", help="Show saved login state files")
    status.add_argument("--platform", choices=["boss", "51job", "liepin"], default=None)

    run = sub.add_parser("run", help="Parse a natural-language request, crawl jobs, and save them")
    run.add_argument("request", help="Example: 查询计算机专业在广州的对口岗位，抓取2页")
    run.add_argument("--source", choices=["boss", "51job", "liepin", "sample"], default="sample")
    run.add_argument("--pages", type=int, default=None, help="Override parsed page count")
    run.add_argument("--headless", action=argparse.BooleanOptionalAction, default=True)
    run.add_argument("--cookies-file", default=None)

    ls = sub.add_parser("list", help="Show stored jobs")
    ls.add_argument("--limit", type=int, default=20)

    show = sub.add_parser("show", help="Show one stored job with full description")
    show.add_argument("id", type=int)

    syllabus = sub.add_parser("syllabus", help="Manage course syllabi")
    syllabus_sub = syllabus.add_subparsers(dest="syllabus_command", required=True)
    syllabus_add = syllabus_sub.add_parser("add", help="Extract text from a syllabus file and save it")
    syllabus_add.add_argument("file", help="Path to .pdf, .docx, .txt, or .md syllabus")
    syllabus_add.add_argument("--title", default=None, help="Optional course title override")
    syllabus_add.add_argument("--analyze", action="store_true", help="Analyze and save course profile after import")
    syllabus_sub.add_parser("list", help="List saved syllabi")
    syllabus_show = syllabus_sub.add_parser("show", help="Show one saved syllabus raw text")
    syllabus_show.add_argument("id", type=int)
    syllabus_analyze = syllabus_sub.add_parser("analyze", help="Analyze one saved syllabus with the configured model")
    syllabus_analyze.add_argument("id", type=int)
    syllabus_profile = syllabus_sub.add_parser("profile", help="Show one saved syllabus analysis profile")
    syllabus_profile.add_argument("id", type=int)

    match = sub.add_parser("match", help="Match selected syllabi against the job pool with an LLM")
    match.add_argument("--syllabus", required=True, help="Comma-separated syllabus ids, e.g. 1 or 1,2,3")
    match.add_argument("--limit", type=int, default=10, help="Maximum matched jobs to print")
    match.add_argument("--candidate-limit", type=int, default=60, help="How many recent jobs to send as candidates; 0 means all")
    match.add_argument("--batch-size", type=int, default=12, help="Jobs per model call")
    match.add_argument("--legacy", action="store_true", help="Use the old whole-text LLM matching flow")

    match_run = sub.add_parser("match-run", help="View or export saved matching runs")
    match_run_sub = match_run.add_subparsers(dest="match_run_command", required=True)
    match_run_list = match_run_sub.add_parser("list", help="List saved matching runs")
    match_run_list.add_argument("--limit", type=int, default=20)
    match_run_show = match_run_sub.add_parser("show", help="Show a saved matching run")
    match_run_show.add_argument("id", type=int)
    match_run_export = match_run_sub.add_parser("export", help="Export a saved matching run JSON")
    match_run_export.add_argument("id", type=int)
    match_run_export.add_argument("--output", required=True)

    export = sub.add_parser("export-json", help="Export all stored jobs to JSON")
    export.add_argument("--output", default=str(ROOT / "data" / "jobs.json"))
    return parser


def main() -> None:
    load_dotenv(ROOT / ".env")
    args = build_parser().parse_args()
    db_path = Path(args.db)

    if args.command == "init-db":
        connect(db_path).close()
        print(f"Database ready: {db_path}")
    elif args.command == "auth":
        handle_auth(args)
    elif args.command == "run":
        handle_run(args, db_path)
    elif args.command == "list":
        handle_list(args, db_path)
    elif args.command == "show":
        handle_show(args, db_path)
    elif args.command == "syllabus":
        handle_syllabus(args, db_path)
    elif args.command == "match":
        handle_match(args, db_path)
    elif args.command == "match-run":
        handle_match_run(args, db_path)
    elif args.command == "export-json":
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(export_jobs(db_path), ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"Exported: {output}")


def handle_auth(args: argparse.Namespace) -> None:
    if args.auth_command == "login":
        path = login_with_browser(ROOT, args.platform)
        print(f"Saved {args.platform} login state: {path}")
        return
    platforms = [args.platform] if args.platform else ["boss", "51job", "liepin"]
    for platform in platforms:
        path = storage_state_path(ROOT, platform)
        print(f"{platform}: {'ready' if path.exists() else 'missing'} ({path})")


def handle_run(args: argparse.Namespace, db_path: Path) -> None:
    intent = parse_intent(args.request)
    if args.pages:
        intent.pages = max(1, min(args.pages, 10))
    print(f"Intent: keywords={intent.keywords}, city={intent.city or '全国'}, salary={intent.salary or '不限'}, pages={intent.pages}")

    if args.source == "51job":
        source = Job51Source(root=ROOT, headless=args.headless, cookies_file=args.cookies_file)
    elif args.source == "liepin":
        source = LiepinSource(root=ROOT, headless=args.headless)
    elif args.source == "boss":
        source = BossSource(root=ROOT)
    else:
        source = SampleSource()

    jobs = source.search(intent)
    inserted, skipped = save_jobs(db_path, jobs)
    print(f"Fetched={len(jobs)}, inserted={inserted}, skipped_duplicates={skipped}, db={db_path}")


def handle_list(args: argparse.Namespace, db_path: Path) -> None:
    rows = list_jobs(db_path, limit=args.limit)
    for row in rows:
        print(
            f"#{row['id']} [{row['source']}] {row['title']} | {row['company']} | "
            f"{row['city']} | {row['salary']} | {row['publish_date']}"
        )
        if row["url"]:
            print(f"    {row['url']}")


def handle_show(args: argparse.Namespace, db_path: Path) -> None:
    row = get_job(db_path, args.id)
    if not row:
        print(f"Job not found: #{args.id}")
        return
    print(f"#{row['id']} [{row['source']}] {row['title']}")
    print(f"公司: {row['company']}")
    print(f"城市: {row['city']}")
    print(f"薪资: {row['salary']}")
    print(f"经验: {row['experience']}")
    print(f"学历: {row['education']}")
    print(f"行业: {row['industry']}")
    print(f"规模: {row['company_size']}")
    print(f"发布日期: {row['publish_date']}")
    print(f"链接: {row['url']}")
    print()
    print("岗位描述/职责/要求:")
    print(row["description"] or "(暂无详情)")


def handle_syllabus(args: argparse.Namespace, db_path: Path) -> None:
    if args.syllabus_command == "add":
        row = add_syllabus(db_path, ROOT, Path(args.file), title=args.title)
        print(f"Saved syllabus #{row['id']}: {row['title']}")
        print(f"File: {row['file_name']} ({row['file_type']})")
        print(f"Text length: {len(row['raw_text'])}")
        if args.analyze:
            profile = analyze_syllabus(db_path, int(row["id"]))
            print()
            print_profile(profile)
    elif args.syllabus_command == "list":
        for row in list_syllabi(db_path):
            print(
                f"#{row['id']} {row['title']} | {row['file_name']} | "
                f"{row['file_type']} | text={row['text_length']} | {row['updated_at']}"
            )
    elif args.syllabus_command == "show":
        row = get_syllabus(db_path, args.id)
        if not row:
            print(f"Syllabus not found: #{args.id}")
            return
        print(f"#{row['id']} {row['title']}")
        print(f"File: {row['file_name']} ({row['file_type']})")
        print(f"Source: {row['source_path']}")
        print(f"Stored: {row['stored_path']}")
        print(f"Updated: {row['updated_at']}")
        print()
        print(row["raw_text"])
    elif args.syllabus_command == "analyze":
        print_profile(analyze_syllabus(db_path, args.id))
    elif args.syllabus_command == "profile":
        profile = get_syllabus_profile(db_path, args.id)
        if not profile:
            print(f"No profile found for syllabus #{args.id}. Run: syllabus analyze {args.id}")
            return
        print_profile(profile)


def print_profile(profile_row) -> None:
    profile = json.loads(profile_row["profile_json"])
    print(f"Profile for syllabus #{profile_row['syllabus_id']}")
    print(json.dumps(profile, ensure_ascii=False, indent=2))


def handle_match(args: argparse.Namespace, db_path: Path) -> None:
    syllabus_ids = [int(part.strip()) for part in args.syllabus.split(",") if part.strip()]
    if args.legacy:
        result = match_syllabi_to_jobs(
            db_path,
            syllabus_ids=syllabus_ids,
            limit=args.limit,
            candidate_limit=args.candidate_limit,
            batch_size=args.batch_size,
        )
    else:
        result = match_syllabi_to_jobs_graph(
            db_path,
            syllabus_ids=syllabus_ids,
            limit=args.limit,
            candidate_limit=args.candidate_limit,
        )
    print(f"Match run #{result.get('match_run_id')} saved.")
    print_matches(result)


def handle_match_run(args: argparse.Namespace, db_path: Path) -> None:
    if args.match_run_command == "list":
        for row in list_match_runs(db_path, limit=args.limit):
            result = json.loads(row["result_json"])
            match_count = len(result.get("matches", []))
            print(f"#{row['id']} syllabi={row['syllabus_ids_json']} matches={match_count} created_at={row['created_at']}")
        return

    row = get_match_run(db_path, args.id)
    if not row:
        print(f"Match run not found: #{args.id}")
        return
    result = json.loads(row["result_json"])
    result.setdefault("match_run_id", row["id"])

    if args.match_run_command == "show":
        print_matches(result)
    elif args.match_run_command == "export":
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "id": row["id"],
            "syllabus_ids": json.loads(row["syllabus_ids_json"]),
            "candidate_job_ids": json.loads(row["candidate_job_ids_json"]),
            "created_at": row["created_at"],
            "result": result,
        }
        output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"Exported: {output}")


def print_matches(result: dict) -> None:
    matches = result.get("matches", [])
    if not matches:
        print("没有找到明确匹配的岗位。")
        return
    for index, item in enumerate(matches, start=1):
        print("=" * 80)
        print(f"{index}. 匹配岗位: {item.get('job_title')} (岗位ID: {item.get('job_id')})")
        print(f"公司: {item.get('company')}")
        print()
        print("匹配到的课程内容:")
        for evidence in item.get("matched_course_content", []):
            print(f"- {evidence}")
        print()
        print("匹配到的岗位要求:")
        for evidence in item.get("matched_job_requirements", []):
            print(f"- {evidence}")
        print()
        print("解释:")
        print(item.get("explanation", ""))


if __name__ == "__main__":
    main()
