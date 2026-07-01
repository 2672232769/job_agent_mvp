from __future__ import annotations

import json
import os
import sqlite3
from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import Any


def _load_neo4j():
    try:
        from neo4j import GraphDatabase
    except ImportError as exc:
        raise RuntimeError("neo4j Python driver is not installed. Run: pip install -r requirements.txt") from exc
    return GraphDatabase


@dataclass(frozen=True)
class Neo4jConfig:
    uri: str
    user: str
    password: str
    database: str = "neo4j"


def get_neo4j_config() -> Neo4jConfig | None:
    uri = os.getenv("NEO4J_URI", "").strip()
    user = os.getenv("NEO4J_USER", "").strip()
    password = os.getenv("NEO4J_PASSWORD", "").strip()
    database = os.getenv("NEO4J_DATABASE", "neo4j").strip() or "neo4j"
    if not uri or not user or not password:
        return None
    return Neo4jConfig(uri=uri, user=user, password=password, database=database)


def require_neo4j_config() -> Neo4jConfig:
    config = get_neo4j_config()
    if not config:
        raise RuntimeError(
            "Neo4j is not configured. Set NEO4J_URI, NEO4J_USER and NEO4J_PASSWORD in .env, "
            "then restart the backend."
        )
    return config


class Neo4jGraphStore(AbstractContextManager["Neo4jGraphStore"]):
    def __init__(self, config: Neo4jConfig | None = None):
        self.config = config or require_neo4j_config()
        graph_database = _load_neo4j()
        self.driver = graph_database.driver(
            self.config.uri,
            auth=(self.config.user, self.config.password),
            connection_timeout=float(os.getenv("NEO4J_CONNECTION_TIMEOUT_SECONDS", "3")),
        )

    def __enter__(self) -> "Neo4jGraphStore":
        self.driver.verify_connectivity()
        self.ensure_schema()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def close(self) -> None:
        self.driver.close()

    def ensure_schema(self) -> None:
        statements = [
            "CREATE CONSTRAINT syllabus_id IF NOT EXISTS FOR (n:Syllabus) REQUIRE n.id IS UNIQUE",
            "CREATE CONSTRAINT course_node_key IF NOT EXISTS FOR (n:CourseNode) REQUIRE n.key IS UNIQUE",
            "CREATE CONSTRAINT job_id IF NOT EXISTS FOR (n:Job) REQUIRE n.id IS UNIQUE",
            "CREATE CONSTRAINT company_name IF NOT EXISTS FOR (n:Company) REQUIRE n.name IS UNIQUE",
            "CREATE CONSTRAINT job_requirement_key IF NOT EXISTS FOR (n:JobRequirement) REQUIRE n.key IS UNIQUE",
            (
                "CREATE FULLTEXT INDEX course_node_text IF NOT EXISTS "
                "FOR (n:CourseNode) ON EACH [n.name, n.category, n.description, n.evidenceText]"
            ),
            (
                "CREATE FULLTEXT INDEX job_requirement_text IF NOT EXISTS "
                "FOR (n:JobRequirement) ON EACH [n.normalizedName, n.requirementText, n.category, n.evidenceText]"
            ),
        ]
        with self.driver.session(database=self.config.database) as session:
            for statement in statements:
                session.run(statement)

    def ensure_vector_indexes(self, dimension: int) -> None:
        if dimension <= 0:
            raise RuntimeError("Neo4j vector index requires a positive embedding dimension.")
        statements = [
            (
                "CREATE VECTOR INDEX course_node_embedding IF NOT EXISTS "
                "FOR (n:CourseNode) ON (n.embedding) "
                f"OPTIONS {{indexConfig: {{`vector.dimensions`: {dimension}, `vector.similarity_function`: 'cosine'}}}}"
            ),
            (
                "CREATE VECTOR INDEX job_requirement_embedding IF NOT EXISTS "
                "FOR (n:JobRequirement) ON (n.embedding) "
                f"OPTIONS {{indexConfig: {{`vector.dimensions`: {dimension}, `vector.similarity_function`: 'cosine'}}}}"
            ),
        ]
        with self.driver.session(database=self.config.database) as session:
            for statement in statements:
                session.run(statement)

    def clear_project_graph(self) -> None:
        with self.driver.session(database=self.config.database) as session:
            session.run(
                """
                MATCH (n)
                WHERE n.project = 'course_job_agent'
                DETACH DELETE n
                """
            )

    def clear_job_subgraph(self) -> None:
        with self.driver.session(database=self.config.database) as session:
            session.run(
                """
                MATCH (n)
                WHERE n.project = 'course_job_agent'
                  AND (n:Job OR n:JobRequirement OR n:Company)
                DETACH DELETE n
                """
            )

    def sync_syllabi(
        self,
        syllabi: list[sqlite3.Row],
        course_nodes: list[sqlite3.Row],
        course_embeddings: dict[int, list[float]] | None = None,
    ) -> None:
        course_embeddings = course_embeddings or {}
        with self.driver.session(database=self.config.database) as session:
            for row in syllabi:
                session.run(
                    """
                    MERGE (s:Syllabus {id: $id})
                    SET s.project = 'course_job_agent',
                        s.title = $title,
                        s.fileName = $file_name,
                        s.fileType = $file_type,
                        s.updatedAt = $updated_at
                    """,
                    dict(row),
                )
            for row in course_nodes:
                payload = _course_node_payload(row)
                payload["embedding"] = course_embeddings.get(int(row["id"]))
                session.run(
                    """
                    MATCH (s:Syllabus {id: $syllabus_id})
                    MERGE (n:CourseNode {key: $key})
                    SET n.project = 'course_job_agent',
                        n.sqliteId = $sqlite_id,
                        n.syllabusId = $syllabus_id,
                        n.nodeType = $node_type,
                        n.name = $name,
                        n.category = $category,
                        n.description = $description,
                        n.proficiencyLevel = $proficiency_level,
                        n.keywords = $keywords,
                        n.evidenceText = $evidence_text,
                        n.embedding = $embedding,
                        n.updatedAt = $updated_at
                    MERGE (s)-[:HAS_COURSE_NODE]->(n)
                    """,
                    payload,
                )

    def sync_jobs(
        self,
        jobs: list[sqlite3.Row],
        requirement_nodes: list[sqlite3.Row],
        requirement_embeddings: dict[int, list[float]] | None = None,
    ) -> None:
        requirement_embeddings = requirement_embeddings or {}
        job_payloads = [_job_payload(row) for row in jobs]
        requirement_payloads = []
        for row in requirement_nodes:
            payload = _requirement_payload(row)
            payload["embedding"] = requirement_embeddings.get(int(row["id"]))
            requirement_payloads.append(payload)
        with self.driver.session(database=self.config.database) as session:
            for batch in _chunked(job_payloads, 200):
                session.run(
                    """
                    UNWIND $rows AS row
                    MERGE (j:Job {id: row.id})
                    SET j.project = 'course_job_agent',
                        j.source = row.source,
                        j.title = row.title,
                        j.company = row.company,
                        j.city = row.city,
                        j.salary = row.salary,
                        j.url = row.url,
                        j.education = row.education,
                        j.experience = row.experience,
                        j.industry = row.industry,
                        j.crawledAt = row.crawled_at
                    MERGE (c:Company {name: row.company})
                    SET c.project = 'course_job_agent'
                    MERGE (c)-[:POSTED]->(j)
                    """,
                    {"rows": batch},
                )
            for batch in _chunked(requirement_payloads, 200):
                session.run(
                    """
                    UNWIND $rows AS row
                    MATCH (j:Job {id: row.job_id})
                    MERGE (r:JobRequirement {key: row.key})
                    SET r.project = 'course_job_agent',
                        r.sqliteId = row.sqlite_id,
                        r.jobId = row.job_id,
                        r.nodeType = row.node_type,
                        r.requirementText = row.requirement_text,
                        r.normalizedName = row.normalized_name,
                        r.category = row.category,
                        r.importance = row.importance,
                        r.keywords = row.keywords,
                        r.evidenceText = row.evidence_text,
                        r.embedding = row.embedding,
                        r.updatedAt = row.updated_at
                    MERGE (j)-[:HAS_REQUIREMENT]->(r)
                    """,
                    {"rows": batch},
                )

    def sync_match_edges(self, edges: list[sqlite3.Row]) -> None:
        relation_map = {
            "supports": "SUPPORTS",
            "partially_supports": "PARTIALLY_SUPPORTS",
            "related": "RELATED_TO",
        }
        with self.driver.session(database=self.config.database) as session:
            for row in edges:
                rel_type = relation_map.get(str(row["relation_type"]), "RELATED_TO")
                statement = f"""
                    MATCH (c:CourseNode {{key: $course_key}})
                    MATCH (r:JobRequirement {{key: $requirement_key}})
                    MERGE (c)-[e:{rel_type}]->(r)
                    SET e.project = 'course_job_agent',
                        e.sqliteId = $sqlite_id,
                        e.rationale = $rationale,
                        e.courseEvidence = $course_evidence,
                        e.jobEvidence = $job_evidence,
                        e.confidenceLabel = $confidence_label,
                        e.updatedAt = $updated_at
                """
                session.run(
                    statement,
                    {
                        "course_key": f"course_node:{row['course_node_id']}",
                        "requirement_key": f"job_requirement:{row['job_requirement_id']}",
                        "sqlite_id": int(row["id"]),
                        "rationale": row["rationale"],
                        "course_evidence": row["course_evidence"],
                        "job_evidence": row["job_evidence"],
                        "confidence_label": row["confidence_label"],
                        "updated_at": row["updated_at"],
                    },
                )

    def counts(self) -> dict[str, int]:
        query = """
        MATCH (n)
        WHERE n.project = 'course_job_agent'
        RETURN
          sum(CASE WHEN 'Syllabus' IN labels(n) THEN 1 ELSE 0 END) AS syllabi,
          sum(CASE WHEN 'CourseNode' IN labels(n) THEN 1 ELSE 0 END) AS course_nodes,
          sum(CASE WHEN 'Job' IN labels(n) THEN 1 ELSE 0 END) AS jobs,
          sum(CASE WHEN 'Company' IN labels(n) THEN 1 ELSE 0 END) AS companies,
          sum(CASE WHEN 'JobRequirement' IN labels(n) THEN 1 ELSE 0 END) AS job_requirements
        """
        rel_query = """
        MATCH (a)-[r]->(b)
        WHERE a.project = 'course_job_agent' AND b.project = 'course_job_agent'
        RETURN count(r) AS relationships
        """
        with self.driver.session(database=self.config.database) as session:
            node_row = session.run(query).single()
            rel_row = session.run(rel_query).single()
        data = dict(node_row or {})
        data["relationships"] = int(rel_row["relationships"] if rel_row else 0)
        return {key: int(value or 0) for key, value in data.items()}

    def preview(self, limit: int = 120) -> dict[str, Any]:
        sample_limit = max(1, min(limit, 500))
        node_queries = [
            (
                "Syllabus",
                """
                MATCH (n:Syllabus)
                WHERE n.project = 'course_job_agent'
                RETURN n
                ORDER BY n.id DESC
                LIMIT $limit
                """,
                min(30, sample_limit),
            ),
            (
                "CourseNode",
                """
                MATCH (n:CourseNode)
                WHERE n.project = 'course_job_agent'
                RETURN n
                ORDER BY n.syllabusId DESC, n.sqliteId DESC
                LIMIT $limit
                """,
                min(40, sample_limit),
            ),
            (
                "Job",
                """
                MATCH (n:Job)
                WHERE n.project = 'course_job_agent'
                RETURN n
                ORDER BY n.id DESC
                LIMIT $limit
                """,
                min(40, sample_limit),
            ),
            (
                "Company",
                """
                MATCH (n:Company)
                WHERE n.project = 'course_job_agent'
                RETURN n
                ORDER BY n.name
                LIMIT $limit
                """,
                min(40, sample_limit),
            ),
            (
                "JobRequirement",
                """
                MATCH (n:JobRequirement)
                WHERE n.project = 'course_job_agent'
                RETURN n
                ORDER BY n.jobId DESC, n.sqliteId DESC
                LIMIT $limit
                """,
                min(80, sample_limit),
            ),
        ]
        edge_queries = [
            """
            MATCH (s:Syllabus)-[r:HAS_COURSE_NODE]->(c:CourseNode)
            WHERE s.project = 'course_job_agent' AND c.project = 'course_job_agent'
            RETURN s AS a, type(r) AS rel_type, c AS b
            ORDER BY s.id DESC, c.sqliteId DESC
            LIMIT $limit
            """,
            """
            MATCH (c:Company)-[r:POSTED]->(j:Job)
            WHERE c.project = 'course_job_agent' AND j.project = 'course_job_agent'
            RETURN c AS a, type(r) AS rel_type, j AS b
            ORDER BY j.id DESC
            LIMIT $limit
            """,
            """
            MATCH (j:Job)-[r:HAS_REQUIREMENT]->(req:JobRequirement)
            WHERE j.project = 'course_job_agent' AND req.project = 'course_job_agent'
            RETURN j AS a, type(r) AS rel_type, req AS b
            ORDER BY j.id DESC, req.sqliteId DESC
            LIMIT $limit
            """,
            """
            MATCH (c:CourseNode)-[r:SUPPORTS|PARTIALLY_SUPPORTS|RELATED_TO]->(req:JobRequirement)
            WHERE c.project = 'course_job_agent' AND req.project = 'course_job_agent'
            RETURN c AS a, type(r) AS rel_type, req AS b
            ORDER BY r.sqliteId DESC
            LIMIT $limit
            """,
        ]
        nodes: dict[str, dict[str, Any]] = {}
        edges: list[dict[str, Any]] = []
        with self.driver.session(database=self.config.database) as session:
            counts = self.counts()
            for _, query, per_label_limit in node_queries:
                for record in session.run(query, {"limit": per_label_limit}):
                    node = _neo4j_node_to_preview(record["n"])
                    nodes[node["id"]] = node
            edge_limit = max(20, sample_limit // 2)
            for query in edge_queries:
                for record in session.run(query, {"limit": edge_limit}):
                    source = _neo4j_node_to_preview(record["a"])
                    target = _neo4j_node_to_preview(record["b"])
                    nodes[source["id"]] = source
                    nodes[target["id"]] = target
                    edges.append({"source": source["id"], "target": target["id"], "type": record["rel_type"]})
        return {
            "counts": counts,
            "nodes": list(nodes.values()),
            "edges": edges[:sample_limit],
            "displayed": {"nodes": len(nodes), "edges": min(len(edges), sample_limit)},
        }

    def retrieve_course_job_evidence(
        self,
        syllabus_ids: list[int],
        candidate_limit: int = 24,
        paths_per_course_node: int = 8,
    ) -> list[dict[str, Any]]:
        if not syllabus_ids:
            return []
        query = """
        MATCH (s:Syllabus)-[:HAS_COURSE_NODE]->(c:CourseNode)
        WHERE s.id IN $syllabus_ids AND c.project = 'course_job_agent'
        RETURN s.id AS syllabus_id,
               s.title AS syllabus_title,
               c.key AS course_key,
               c.sqliteId AS course_node_id,
               c.nodeType AS course_node_type,
               c.name AS course_node_name,
               c.category AS course_category,
               c.description AS course_description,
               c.evidenceText AS course_evidence,
               c.keywords AS course_keywords,
               c.embedding AS course_embedding
        ORDER BY s.id, c.nodeType, c.name
        """
        packages: dict[int, dict[str, Any]] = {}
        with self.driver.session(database=self.config.database) as session:
            course_rows = list(session.run(query, {"syllabus_ids": syllabus_ids}))
            if not course_rows:
                return []
            for course_row in course_rows:
                course_embedding = course_row["course_embedding"]
                if not course_embedding:
                    raise RuntimeError(
                        "Course embeddings are missing in Neo4j. Rebuild or sync the course knowledge graph first."
                    )
                search_query = build_fulltext_query(
                    [
                        course_row["course_node_name"],
                        course_row["course_category"],
                        *(course_row["course_keywords"] or []),
                    ]
                )
                if search_query:
                    requirement_query = """
                    CALL db.index.fulltext.queryNodes('job_requirement_text', $query, {limit: $limit})
                    YIELD node, score
                    WHERE node.project = 'course_job_agent' AND node.embedding IS NOT NULL
                    MATCH (job:Job)-[:HAS_REQUIREMENT]->(node)
                    OPTIONAL MATCH (company:Company)-[:POSTED]->(job)
                    RETURN score,
                           job.id AS job_id,
                           job.title AS job_title,
                           job.company AS company,
                           job.city AS city,
                           job.salary AS salary,
                           job.url AS url,
                           job.education AS education,
                           job.experience AS experience,
                           company.name AS company_name,
                           node.key AS requirement_key,
                           node.sqliteId AS requirement_id,
                           node.nodeType AS requirement_type,
                           node.requirementText AS requirement_text,
                           node.normalizedName AS requirement_name,
                           node.category AS requirement_category,
                           node.importance AS requirement_importance,
                           node.evidenceText AS requirement_evidence,
                           node.keywords AS requirement_keywords
                    ORDER BY score DESC
                    """
                    for row in session.run(requirement_query, {"query": search_query, "limit": paths_per_course_node}):
                        add_retrieval_record(packages, course_row, row, source="fulltext")

                vector_query = """
                CALL db.index.vector.queryNodes('job_requirement_embedding', $limit, $embedding)
                YIELD node, score
                WHERE node.project = 'course_job_agent'
                MATCH (job:Job)-[:HAS_REQUIREMENT]->(node)
                OPTIONAL MATCH (company:Company)-[:POSTED]->(job)
                RETURN score,
                       job.id AS job_id,
                       job.title AS job_title,
                       job.company AS company,
                       job.city AS city,
                       job.salary AS salary,
                       job.url AS url,
                       job.education AS education,
                       job.experience AS experience,
                       company.name AS company_name,
                       node.key AS requirement_key,
                       node.sqliteId AS requirement_id,
                       node.nodeType AS requirement_type,
                       node.requirementText AS requirement_text,
                       node.normalizedName AS requirement_name,
                       node.category AS requirement_category,
                       node.importance AS requirement_importance,
                       node.evidenceText AS requirement_evidence,
                       node.keywords AS requirement_keywords
                ORDER BY score DESC
                """
                for row in session.run(
                    vector_query,
                    {"embedding": list(course_embedding), "limit": paths_per_course_node},
                ):
                    add_retrieval_record(packages, course_row, row, source="vector")

        ranked = sorted(packages.values(), key=lambda item: item["retrieval_score"], reverse=True)
        for package in ranked:
            package["evidence_paths"] = sorted(
                package["evidence_paths"],
                key=lambda item: item["hybrid_score"],
                reverse=True,
            )[:12]
            package.pop("_path_index", None)
        return ranked[: max(1, min(candidate_limit, 200))]


def add_retrieval_record(
    packages: dict[int, dict[str, Any]],
    course_row: Any,
    row: Any,
    source: str,
) -> None:
    job_id = int(row["job_id"])
    package = packages.setdefault(
        job_id,
        {
            "job_id": job_id,
            "job": {
                "job_id": job_id,
                "title": row["job_title"],
                "company": row["company"] or row["company_name"],
                "city": row["city"],
                "salary": row["salary"],
                "url": row["url"],
                "education": row["education"],
                "experience": row["experience"],
            },
            "evidence_paths": [],
            "retrieval_score": 0.0,
            "_path_index": {},
        },
    )
    course_node_id = int(course_row["course_node_id"])
    requirement_id = int(row["requirement_id"])
    path_key = f"{course_node_id}:{requirement_id}"
    path_index = package.setdefault("_path_index", {})
    path = path_index.get(path_key)
    if not path:
        path = {
            "course_node_id": course_node_id,
            "course_node_key": course_row["course_key"],
            "syllabus_id": int(course_row["syllabus_id"]),
            "syllabus_title": course_row["syllabus_title"],
            "course_node_type": course_row["course_node_type"],
            "course_node_name": course_row["course_node_name"],
            "course_category": course_row["course_category"],
            "course_description": course_row["course_description"],
            "course_evidence": course_row["course_evidence"],
            "requirement_id": requirement_id,
            "requirement_key": row["requirement_key"],
            "requirement_type": row["requirement_type"],
            "requirement_text": row["requirement_text"],
            "requirement_name": row["requirement_name"],
            "requirement_category": row["requirement_category"],
            "requirement_importance": row["requirement_importance"],
            "requirement_evidence": row["requirement_evidence"],
            "retrieval_sources": [],
            "fulltext_score": 0.0,
            "vector_score": 0.0,
            "hybrid_score": 0.0,
            "path": [
                f"Syllabus:{course_row['syllabus_title']}",
                f"CourseNode:{course_row['course_node_name']}",
                "HYBRID_RETRIEVES",
                f"JobRequirement:{row['requirement_name'] or row['requirement_text']}",
                f"Job:{row['job_title']}",
            ],
        }
        path_index[path_key] = path
        package["evidence_paths"].append(path)

    previous_score = float(path.get("hybrid_score") or 0)
    source_score = float(row["score"] or 0)
    if source == "fulltext":
        path["fulltext_score"] = max(float(path.get("fulltext_score") or 0), source_score)
    elif source == "vector":
        path["vector_score"] = max(float(path.get("vector_score") or 0), source_score)
    if source not in path["retrieval_sources"]:
        path["retrieval_sources"].append(source)

    path["hybrid_score"] = calculate_hybrid_score(path["fulltext_score"], path["vector_score"])
    package["retrieval_score"] += path["hybrid_score"] - previous_score


def calculate_hybrid_score(fulltext_score: float, vector_score: float) -> float:
    normalized_fulltext = min(max(fulltext_score, 0.0), 8.0) / 8.0
    normalized_vector = min(max(vector_score, 0.0), 1.0)
    if normalized_fulltext and normalized_vector:
        return normalized_fulltext + normalized_vector + 0.25
    return normalized_fulltext + normalized_vector


def _course_node_payload(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "key": f"course_node:{row['id']}",
        "sqlite_id": int(row["id"]),
        "syllabus_id": int(row["syllabus_id"]),
        "node_type": row["node_type"],
        "name": row["name"],
        "category": row["category"],
        "description": row["description"],
        "proficiency_level": row["proficiency_level"],
        "keywords": _json_list(row["keywords_json"]),
        "evidence_text": row["evidence_text"],
        "updated_at": row["updated_at"],
    }


def _job_payload(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": int(row["id"]),
        "source": row["source"],
        "title": row["title"],
        "company": row["company"] or "",
        "city": row["city"],
        "salary": row["salary"],
        "url": row["url"],
        "education": row["education"],
        "experience": row["experience"],
        "industry": row["industry"],
        "crawled_at": row["crawled_at"],
    }


def _requirement_payload(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "key": f"job_requirement:{row['id']}",
        "sqlite_id": int(row["id"]),
        "job_id": int(row["job_id"]),
        "node_type": row["node_type"],
        "requirement_text": row["requirement_text"],
        "normalized_name": row["normalized_name"],
        "category": row["category"],
        "importance": row["importance"],
        "keywords": _json_list(row["keywords_json"]),
        "evidence_text": row["evidence_text"],
        "updated_at": row["updated_at"],
    }


def _json_list(value: str | None) -> list[str]:
    if not value:
        return []
    try:
        payload = json.loads(value)
    except json.JSONDecodeError:
        return []
    return payload if isinstance(payload, list) else []


def _chunked(items: list[Any], size: int):
    for index in range(0, len(items), size):
        yield items[index : index + size]


def build_fulltext_query(values: list[Any]) -> str:
    terms: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if not text:
            continue
        for part in re_split_terms(text):
            cleaned = escape_fulltext_term(part)
            if cleaned and cleaned not in terms:
                terms.append(cleaned)
    return " OR ".join(f'"{term}"' for term in terms[:12])


def re_split_terms(text: str) -> list[str]:
    import re

    raw_terms = re.findall(r"[a-zA-Z][a-zA-Z0-9+#.\-]{1,}|[\u4e00-\u9fff]{2,}", text.lower())
    result: list[str] = []
    for term in raw_terms:
        result.append(term)
        if re.fullmatch(r"[\u4e00-\u9fff]{5,}", term):
            for size in (2, 3, 4):
                for index in range(0, len(term) - size + 1):
                    result.append(term[index : index + size])
    stopwords = {"能力", "相关", "岗位", "课程", "学生", "掌握", "熟悉", "了解", "进行", "使用", "负责", "要求"}
    return [term for term in result if len(term) >= 2 and term not in stopwords]


def escape_fulltext_term(term: str) -> str:
    return (
        term.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("~", " ")
        .replace("^", " ")
        .replace(":", " ")
        .strip()
    )


def _neo4j_node_to_preview(node: Any) -> dict[str, Any]:
    labels = list(node.labels)
    label = labels[0] if labels else "Node"
    props = dict(node)
    if label == "Syllabus":
        node_id = f"Syllabus:{props.get('id')}"
        title = props.get("title") or node_id
    elif label == "Job":
        node_id = f"Job:{props.get('id')}"
        title = props.get("title") or node_id
    elif label == "CourseNode":
        node_id = str(props.get("key"))
        title = props.get("name") or node_id
    elif label == "JobRequirement":
        node_id = str(props.get("key"))
        title = props.get("normalizedName") or props.get("requirementText") or node_id
    elif label == "Company":
        node_id = f"Company:{props.get('name')}"
        title = props.get("name") or node_id
    else:
        node_id = str(node.element_id)
        title = node_id
    return {"id": node_id, "label": label, "title": title, "properties": props}
