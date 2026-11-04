"""Dissertation outline and revision-pinned membership services."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import sqlite3
import unicodedata
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fukamiru.defects import DefectRepository
from fukamiru.errors import FukamiruError
from fukamiru.revisions.service import RevisionService
from fukamiru.storage.runs import utc_now

WHOLE_DISSERTATION_EVALUATOR_VERSION = "deterministic-v1"
OUTLINE_IMPORT_VERSION = "csv-outline-v1"
DEFAULT_TARGET_CHARACTERS = {
    "part": 0,
    "chapter": 12000,
    "section": 5000,
    "appendix": 4000,
    "supplement": 3000,
}
_HEADER_ALIASES = {
    "node_type": {"type", "node_type", "kind", "種別", "種類", "構成種別"},
    "parent": {"parent", "parent_title", "parent_chapter", "親", "親タイトル", "親章"},
    "title": {"title", "chapter_title", "section_title", "タイトル", "章タイトル", "節タイトル"},
    "summary": {"summary", "abstract", "overview", "概要", "要約"},
    "instructions": {"instructions", "instruction", "prompt", "指示", "執筆指示", "生成指示"},
    "target_characters": {
        "target_characters",
        "target_chars",
        "characters",
        "length",
        "文字数",
        "目標文字数",
        "分量",
    },
    "order_index": {"order", "order_index", "position", "順序", "並び順"},
}
_NODE_TYPE_ALIASES = {
    "part": "part",
    "部": "part",
    "chapter": "chapter",
    "章": "chapter",
    "section": "section",
    "節": "section",
    "appendix": "appendix",
    "付録": "appendix",
    "supplement": "supplement",
    "補遺": "supplement",
}


@dataclass(frozen=True)
class DissertationFinding:
    defect_type: str
    severity: str
    excerpt: str
    context: str
    rationale: str
    page_start: int | None = None
    page_end: int | None = None
    repair_candidates: tuple[str, ...] = ()
    confidence: float = 1.0
    automatic_repair_allowed: bool = False


class DissertationService:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def create(self, *, project_id: str, title: str) -> dict[str, Any]:
        project = self.connection.execute(
            "SELECT id FROM projects WHERE id = ?", (project_id,)
        ).fetchone()
        if project is None:
            raise FukamiruError(f"Project not found: {project_id}")
        document_id = RevisionService(self.connection).create_document(
            project_id=project_id, kind="dissertation", title=title
        )
        root_id = str(uuid.uuid4())
        now = utc_now()
        self.connection.execute(
            """
            INSERT INTO dissertation_nodes(
                id, dissertation_document_id, parent_id, node_type,
                title, order_index, created_at
            ) VALUES (?, ?, NULL, 'root', ?, 0, ?)
            """,
            (root_id, document_id, title, now),
        )
        self.connection.commit()
        return self.snapshot(document_id)

    def add_outline_node(
        self,
        *,
        dissertation_document_id: str,
        parent_id: str,
        node_type: str,
        title: str,
        order_index: int,
    ) -> dict[str, Any]:
        if node_type not in {"part", "chapter", "section", "appendix", "supplement"}:
            raise FukamiruError(f"Invalid outline node type: {node_type}")
        parent = self.connection.execute(
            """
            SELECT id FROM dissertation_nodes
            WHERE id = ? AND dissertation_document_id = ?
            """,
            (parent_id, dissertation_document_id),
        ).fetchone()
        if parent is None:
            raise FukamiruError("Outline parent does not belong to this dissertation")
        node_id = str(uuid.uuid4())
        try:
            self.connection.execute(
                """
                INSERT INTO dissertation_nodes(
                    id, dissertation_document_id, parent_id, node_type,
                    title, order_index, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    node_id,
                    dissertation_document_id,
                    parent_id,
                    node_type,
                    title,
                    order_index,
                    utc_now(),
                ),
            )
            self.connection.commit()
        except sqlite3.IntegrityError as exc:
            self.connection.rollback()
            raise FukamiruError("Outline order is already occupied") from exc
        return dict(
            self.connection.execute(
                "SELECT * FROM dissertation_nodes WHERE id = ?", (node_id,)
            ).fetchone()
        )

    def preview_outline_csv(
        self,
        *,
        dissertation_document_id: str,
        source_path: str,
        total_target_characters: int | None = None,
    ) -> dict[str, Any]:
        if total_target_characters is not None and total_target_characters < 1:
            raise FukamiruError("Total target characters must be greater than zero")
        dissertation = self.connection.execute(
            """
            SELECT id FROM content_documents
            WHERE id = ? AND kind = 'dissertation'
            """,
            (dissertation_document_id,),
        ).fetchone()
        if dissertation is None:
            raise FukamiruError(f"Dissertation not found: {dissertation_document_id}")
        path = Path(source_path.strip().strip('"\'')).expanduser().resolve()
        if not path.is_file():
            raise FukamiruError(f"Outline CSV does not exist: {path}")
        if path.suffix.casefold() not in {".csv", ".tsv"}:
            raise FukamiruError("Dissertation outline import requires a CSV or TSV file")
        raw = path.read_bytes()
        text, encoding = self._decode_csv(raw)
        dialect = self._csv_dialect(text, path.suffix.casefold())
        reader = csv.DictReader(io.StringIO(text), dialect=dialect)
        headers = [str(value or "").strip() for value in (reader.fieldnames or [])]
        header_map = self._header_map(headers)
        if "title" not in header_map:
            raise FukamiruError(
                "Outline CSV requires a title column "
                "(title, chapter_title, タイトル, or 章タイトル)"
            )
        rows = [
            dict(row)
            for row in reader
            if any(str(value or "").strip() for value in row.values())
        ]
        if not rows:
            raise FukamiruError("Outline CSV contains no data rows")
        plan, warnings = self._build_outline_plan(
            rows,
            header_map,
            total_target_characters=total_target_characters,
        )
        import_id = str(uuid.uuid4())
        self.connection.execute(
            """
            INSERT INTO dissertation_outline_imports(
                id, dissertation_document_id, source_path, source_sha256,
                source_encoding, headers_json, row_count, plan_json,
                warnings_json, status, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'previewed', ?)
            """,
            (
                import_id,
                dissertation_document_id,
                str(path),
                hashlib.sha256(raw).hexdigest(),
                encoding,
                json.dumps(headers, ensure_ascii=False),
                len(plan),
                json.dumps(plan, ensure_ascii=False, separators=(",", ":")),
                json.dumps(warnings, ensure_ascii=False, separators=(",", ":")),
                utc_now(),
            ),
        )
        self.connection.commit()
        return self.get_outline_import(import_id)

    def confirm_outline_csv(
        self, *, dissertation_document_id: str, import_id: str
    ) -> dict[str, Any]:
        imported = self.connection.execute(
            """
            SELECT * FROM dissertation_outline_imports
            WHERE id = ? AND dissertation_document_id = ?
            """,
            (import_id, dissertation_document_id),
        ).fetchone()
        if imported is None:
            raise FukamiruError(f"Outline import not found: {import_id}")
        if imported["status"] == "applied":
            return {
                "import": self.get_outline_import(import_id),
                "dissertation": self.snapshot(dissertation_document_id),
            }
        if imported["status"] != "previewed":
            raise FukamiruError("Outline import is not available for confirmation")
        root = self.connection.execute(
            """
            SELECT id FROM dissertation_nodes
            WHERE dissertation_document_id = ? AND node_type = 'root'
            """,
            (dissertation_document_id,),
        ).fetchone()
        if root is None:
            raise FukamiruError("Dissertation root outline node is missing")
        plan = json.loads(imported["plan_json"])
        created_ids: dict[str, str] = {"root": str(root["id"])}
        next_orders: dict[str, int] = {}
        now = utc_now()
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            for item in plan:
                parent_id = created_ids.get(str(item["parent_key"]))
                if parent_id is None:
                    raise FukamiruError(
                        f"CSV row {item['source_row_number']} has an unresolved parent"
                    )
                if parent_id not in next_orders:
                    row = self.connection.execute(
                        """
                        SELECT COALESCE(MAX(order_index), -1) AS maximum
                        FROM dissertation_nodes
                        WHERE dissertation_document_id = ? AND parent_id = ?
                        """,
                        (dissertation_document_id, parent_id),
                    ).fetchone()
                    next_orders[parent_id] = int(row["maximum"]) + 1
                node_id = str(uuid.uuid4())
                self.connection.execute(
                    """
                    INSERT INTO dissertation_nodes(
                        id, dissertation_document_id, parent_id, node_type,
                        title, order_index, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        node_id,
                        dissertation_document_id,
                        parent_id,
                        item["node_type"],
                        item["title"],
                        next_orders[parent_id],
                        now,
                    ),
                )
                next_orders[parent_id] += 1
                created_ids[str(item["key"])] = node_id
                self.connection.execute(
                    """
                    INSERT INTO dissertation_outline_directives(
                        id, import_id, outline_node_id, source_row_number,
                        summary, instructions, target_characters, generation_status,
                        source_json, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, 'planned', ?, ?, ?)
                    """,
                    (
                        str(uuid.uuid4()),
                        import_id,
                        node_id,
                        int(item["source_row_number"]),
                        item["summary"],
                        item["instructions"],
                        int(item["target_characters"]),
                        json.dumps(item["source"], ensure_ascii=False, separators=(",", ":")),
                        now,
                        now,
                    ),
                )
            self.connection.execute(
                """
                UPDATE dissertation_outline_imports
                SET status = 'applied', applied_at = ? WHERE id = ?
                """,
                (now, import_id),
            )
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise
        return {
            "import": self.get_outline_import(import_id),
            "dissertation": self.snapshot(dissertation_document_id),
        }

    def get_outline_import(self, import_id: str) -> dict[str, Any]:
        row = self.connection.execute(
            "SELECT * FROM dissertation_outline_imports WHERE id = ?", (import_id,)
        ).fetchone()
        if row is None:
            raise FukamiruError(f"Outline import not found: {import_id}")
        payload = dict(row)
        payload["headers"] = json.loads(payload.pop("headers_json"))
        payload["plan"] = json.loads(payload.pop("plan_json"))
        payload["warnings"] = json.loads(payload.pop("warnings_json"))
        payload["target_characters"] = sum(
            int(item["target_characters"]) for item in payload["plan"]
        )
        payload["import_version"] = OUTLINE_IMPORT_VERSION
        return payload

    @staticmethod
    def _decode_csv(raw: bytes) -> tuple[str, str]:
        for encoding in ("utf-8-sig", "utf-8", "cp932"):
            try:
                return raw.decode(encoding), encoding
            except UnicodeDecodeError:
                continue
        raise FukamiruError("Outline CSV encoding must be UTF-8 or CP932")

    @staticmethod
    def _csv_dialect(text: str, suffix: str) -> type[csv.Dialect] | csv.Dialect:
        try:
            return csv.Sniffer().sniff(text[:8192], delimiters=",\t;")
        except csv.Error:
            return csv.excel_tab if suffix == ".tsv" else csv.excel

    @staticmethod
    def _normalize_header(value: str) -> str:
        value = unicodedata.normalize("NFKC", value).strip().casefold()
        return re.sub(r"[\s\-/]+", "_", value).strip("_")

    @classmethod
    def _header_map(cls, headers: list[str]) -> dict[str, str]:
        aliases = {
            canonical: {cls._normalize_header(alias) for alias in values}
            for canonical, values in _HEADER_ALIASES.items()
        }
        result: dict[str, str] = {}
        for header in headers:
            normalized = cls._normalize_header(header)
            for canonical, values in aliases.items():
                if normalized in values and canonical not in result:
                    result[canonical] = header
        return result

    @classmethod
    def _build_outline_plan(
        cls,
        rows: list[dict[str, Any]],
        header_map: dict[str, str],
        *,
        total_target_characters: int | None,
    ) -> tuple[list[dict[str, Any]], list[str]]:
        plan: list[dict[str, Any]] = []
        warnings: list[str] = []
        title_keys: dict[str, list[str]] = {}
        last_part = "root"
        last_content = "root"
        for index, raw in enumerate(rows, start=2):
            def get(name: str, current: dict[str, Any] = raw) -> str:
                return str(current.get(header_map.get(name, ""), "") or "").strip()

            title = get("title")
            if not title:
                raise FukamiruError(f"CSV row {index} has no title")
            raw_type = unicodedata.normalize("NFKC", get("node_type")).strip().casefold()
            node_type = _NODE_TYPE_ALIASES.get(raw_type or "chapter")
            if node_type is None:
                raise FukamiruError(f"CSV row {index} has an invalid node type: {raw_type}")
            parent_label = get("parent")
            if parent_label:
                matches = title_keys.get(parent_label.casefold(), [])
                if len(matches) != 1:
                    raise FukamiruError(
                        f"CSV row {index} parent must name exactly one earlier title: "
                        f"{parent_label}"
                    )
                parent_key = matches[0]
            elif node_type == "part":
                parent_key = "root"
            elif node_type == "section":
                parent_key = last_content
            elif node_type == "chapter" and last_part != "root":
                parent_key = last_part
            else:
                parent_key = "root"
            target_raw = get("target_characters").replace(",", "").replace("，", "")
            explicit_target: int | None = None
            if target_raw:
                try:
                    explicit_target = int(target_raw)
                except ValueError as exc:
                    raise FukamiruError(
                        f"CSV row {index} has an invalid target character count: {target_raw}"
                    ) from exc
                if explicit_target < 0:
                    raise FukamiruError(
                        f"CSV row {index} target character count cannot be negative"
                    )
            key = f"row-{index}"
            item = {
                "key": key,
                "parent_key": parent_key,
                "source_row_number": index,
                "node_type": node_type,
                "title": title,
                "summary": get("summary"),
                "instructions": get("instructions"),
                "target_characters": explicit_target,
                "explicit_target": explicit_target is not None,
                "source": {str(k): str(v or "") for k, v in raw.items()},
            }
            plan.append(item)
            title_keys.setdefault(title.casefold(), []).append(key)
            if node_type == "part":
                last_part = key
            if node_type in {"chapter", "appendix", "supplement"}:
                last_content = key

        missing = [item for item in plan if item["target_characters"] is None]
        explicit_total = sum(
            int(item["target_characters"] or 0) for item in plan if item["explicit_target"]
        )
        if total_target_characters is None:
            for item in missing:
                item["target_characters"] = DEFAULT_TARGET_CHARACTERS[item["node_type"]]
        else:
            if explicit_total > total_target_characters:
                raise FukamiruError(
                    "Explicit CSV target characters exceed the requested dissertation total"
                )
            remaining = total_target_characters - explicit_total
            weights = [DEFAULT_TARGET_CHARACTERS[item["node_type"]] for item in missing]
            weight_total = sum(weights)
            if weight_total == 0 and remaining:
                warnings.append(
                    "The requested total could not be assigned because all unspecified "
                    "rows are non-content parts."
                )
            allocated = 0
            for position, (item, weight) in enumerate(zip(missing, weights, strict=True)):
                if not weight_total:
                    target = 0
                elif position == len(missing) - 1:
                    target = remaining - allocated
                else:
                    target = int(remaining * weight / weight_total)
                    allocated += target
                item["target_characters"] = target
        for item in plan:
            summary = item["summary"] or "概要はCSVに未指定"
            instructions = item["instructions"] or "章題と概要に沿って論旨を構成する"
            item["generation_brief"] = (
                f"『{item['title']}』を約{int(item['target_characters']):,}字で執筆する。"
                f"概要: {summary} 指示: {instructions}"
            )
        return plan, warnings

    def include_revision(
        self,
        *,
        dissertation_document_id: str,
        outline_node_id: str,
        content_document_id: str,
        included_revision_id: str,
        role: str,
        order_index: int,
    ) -> dict[str, Any]:
        if role not in {"chapter", "appendix", "supplement"}:
            raise FukamiruError(f"Invalid dissertation membership role: {role}")
        outline = self.connection.execute(
            """
            SELECT id FROM dissertation_nodes
            WHERE id = ? AND dissertation_document_id = ?
            """,
            (outline_node_id, dissertation_document_id),
        ).fetchone()
        revision = self.connection.execute(
            """
            SELECT id FROM revisions WHERE id = ? AND document_id = ?
            """,
            (included_revision_id, content_document_id),
        ).fetchone()
        if outline is None:
            raise FukamiruError("Outline node does not belong to this dissertation")
        if revision is None:
            raise FukamiruError("Included Revision does not belong to the content document")
        membership_id = str(uuid.uuid4())
        now = utc_now()
        self.connection.execute(
            """
            INSERT INTO dissertation_memberships(
                id, dissertation_document_id, outline_node_id, content_document_id,
                included_revision_id, role, order_index, status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'active', ?, ?)
            """,
            (
                membership_id,
                dissertation_document_id,
                outline_node_id,
                content_document_id,
                included_revision_id,
                role,
                order_index,
                now,
                now,
            ),
        )
        self.connection.commit()
        return dict(
            self.connection.execute(
                "SELECT * FROM dissertation_memberships WHERE id = ?",
                (membership_id,),
            ).fetchone()
        )

    def refresh_updates(self, dissertation_document_id: str) -> list[dict[str, Any]]:
        dissertation = self.connection.execute(
            """
            SELECT project_id FROM content_documents
            WHERE id = ? AND kind = 'dissertation'
            """,
            (dissertation_document_id,),
        ).fetchone()
        if dissertation is None:
            raise FukamiruError(f"Dissertation not found: {dissertation_document_id}")
        rows = self.connection.execute(
            """
            SELECT m.*, d.current_revision_id
            FROM dissertation_memberships m
            JOIN content_documents d ON d.id = m.content_document_id
            WHERE m.dissertation_document_id = ?
            """,
            (dissertation_document_id,),
        ).fetchall()
        changes: list[dict[str, Any]] = []
        for row in rows:
            current = row["current_revision_id"]
            stale = current is not None and current != row["included_revision_id"]
            status = "update_available" if stale else "active"
            self.connection.execute(
                "UPDATE dissertation_memberships SET status = ?, updated_at = ? WHERE id = ?",
                (status, utc_now(), row["id"]),
            )
            if not stale:
                continue
            existing = self.connection.execute(
                """
                SELECT id FROM change_requests
                WHERE project_id = ? AND object_type = 'dissertation_membership'
                  AND object_id = ? AND request_type = 'included_revision_update'
                  AND status = 'open'
                """,
                (dissertation["project_id"], row["id"]),
            ).fetchone()
            if existing is None:
                request_id = str(uuid.uuid4())
                self.connection.execute(
                    """
                    INSERT INTO change_requests(
                        id, project_id, object_type, object_id, request_type,
                        payload_json, status, created_at
                    ) VALUES (?, ?, 'dissertation_membership', ?,
                              'included_revision_update', ?, 'open', ?)
                    """,
                    (
                        request_id,
                        dissertation["project_id"],
                        row["id"],
                        json.dumps(
                            {
                                "included_revision_id": row["included_revision_id"],
                                "current_revision_id": current,
                            },
                            ensure_ascii=False,
                        ),
                        utc_now(),
                    ),
                )
            else:
                request_id = str(existing["id"])
            changes.append(
                {
                    "membership_id": str(row["id"]),
                    "change_request_id": request_id,
                    "included_revision_id": str(row["included_revision_id"]),
                    "current_revision_id": str(current),
                }
            )
        self.connection.commit()
        return changes

    def snapshot(self, dissertation_document_id: str) -> dict[str, Any]:
        document = self.connection.execute(
            """
            SELECT * FROM content_documents
            WHERE id = ? AND kind = 'dissertation'
            """,
            (dissertation_document_id,),
        ).fetchone()
        if document is None:
            raise FukamiruError(f"Dissertation not found: {dissertation_document_id}")
        self.refresh_updates(dissertation_document_id)
        nodes = [
            dict(row)
            for row in self.connection.execute(
                """
                SELECT * FROM dissertation_nodes
                WHERE dissertation_document_id = ? ORDER BY parent_id, order_index
                """,
                (dissertation_document_id,),
            )
        ]
        directives = {
            str(row["outline_node_id"]): dict(row)
            for row in self.connection.execute(
                """
                SELECT d.* FROM dissertation_outline_directives d
                JOIN dissertation_nodes n ON n.id = d.outline_node_id
                WHERE n.dissertation_document_id = ?
                ORDER BY d.source_row_number
                """,
                (dissertation_document_id,),
            )
        }
        for node in nodes:
            directive = directives.get(str(node["id"]))
            if directive:
                directive["source"] = json.loads(directive.pop("source_json"))
            node["directive"] = directive
        memberships = [
            dict(row)
            for row in self.connection.execute(
                """
                SELECT m.*, d.title AS content_title,
                       d.current_revision_id AS current_content_revision_id
                FROM dissertation_memberships m
                JOIN content_documents d ON d.id = m.content_document_id
                WHERE m.dissertation_document_id = ? ORDER BY m.order_index
                """,
                (dissertation_document_id,),
            )
        ]
        imports = [
            self.get_outline_import(str(row["id"]))
            for row in self.connection.execute(
                """
                SELECT id FROM dissertation_outline_imports
                WHERE dissertation_document_id = ? ORDER BY created_at
                """,
                (dissertation_document_id,),
            )
        ]
        return {
            "document": dict(document),
            "nodes": nodes,
            "memberships": memberships,
            "outline_imports": imports,
        }

    def evaluate_consistency(
        self, *, run_id: str, dissertation_document_id: str
    ) -> dict[str, Any]:
        """Persist deterministic checks across every revision pinned into a thesis."""

        if self.connection.execute(
            "SELECT 1 FROM runs WHERE id = ?", (run_id,)
        ).fetchone() is None:
            raise FukamiruError(f"Run not found: {run_id}")
        snapshot = self.snapshot(dissertation_document_id)
        evaluation_id = str(uuid.uuid4())
        self.connection.execute(
            """
            INSERT INTO dissertation_evaluation_runs(
                id, run_id, dissertation_document_id, evaluator_version,
                status, started_at
            ) VALUES (?, ?, ?, ?, 'running', ?)
            """,
            (
                evaluation_id,
                run_id,
                dissertation_document_id,
                WHOLE_DISSERTATION_EVALUATOR_VERSION,
                utc_now(),
            ),
        )
        self.connection.commit()
        try:
            findings = self._consistency_findings(snapshot)
            repository = DefectRepository(self.connection)
            defect_ids: list[str] = []
            counts: dict[str, int] = {}
            for evaluator_name, finding, object_type, object_id in findings:
                defect_id = repository.record(
                    run_id=run_id,
                    finding=finding,
                    source_asset_id="",
                    object_type=object_type,
                    object_id=object_id,
                    details={
                        "dissertation_evaluation_id": evaluation_id,
                        "evaluator": evaluator_name,
                    },
                )
                self.connection.execute(
                    """
                    INSERT INTO dissertation_evaluation_defects(
                        dissertation_evaluation_id, defect_id, evaluator_name
                    ) VALUES (?, ?, ?)
                    """,
                    (evaluation_id, defect_id, evaluator_name),
                )
                defect_ids.append(defect_id)
                counts[finding.severity] = counts.get(finding.severity, 0) + 1
            summary = {
                "finding_count": len(defect_ids),
                "severity_counts": counts,
                "defect_ids": defect_ids,
                "pinned_revision_count": len(snapshot["memberships"]),
                "outline_node_count": len(snapshot["nodes"]),
            }
            self.connection.execute(
                """
                UPDATE dissertation_evaluation_runs
                SET status = 'completed', summary_json = ?, completed_at = ?
                WHERE id = ?
                """,
                (json.dumps(summary, ensure_ascii=False), utc_now(), evaluation_id),
            )
            self.connection.commit()
        except Exception as exc:
            self.connection.execute(
                """
                UPDATE dissertation_evaluation_runs
                SET status = 'failed', summary_json = ?, completed_at = ? WHERE id = ?
                """,
                (json.dumps({"error": str(exc)}, ensure_ascii=False), utc_now(), evaluation_id),
            )
            self.connection.commit()
            raise
        return self.get_consistency_evaluation(evaluation_id)

    def get_consistency_evaluation(self, evaluation_id: str) -> dict[str, Any]:
        row = self.connection.execute(
            "SELECT * FROM dissertation_evaluation_runs WHERE id = ?", (evaluation_id,)
        ).fetchone()
        if row is None:
            raise FukamiruError(f"Dissertation evaluation not found: {evaluation_id}")
        result = dict(row)
        result["summary"] = json.loads(result.pop("summary_json"))
        repository = DefectRepository(self.connection)
        result["defects"] = [
            repository.get(str(item["defect_id"]))
            for item in self.connection.execute(
                """
                SELECT defect_id FROM dissertation_evaluation_defects
                WHERE dissertation_evaluation_id = ? ORDER BY rowid
                """,
                (evaluation_id,),
            )
        ]
        return result

    def _consistency_findings(
        self, snapshot: dict[str, Any]
    ) -> list[tuple[str, DissertationFinding, str, str]]:
        document_id = str(snapshot["document"]["id"])
        memberships = snapshot["memberships"]
        membership_by_node = {
            str(membership["outline_node_id"]): membership for membership in memberships
        }
        findings: list[tuple[str, DissertationFinding, str, str]] = []
        for node in snapshot["nodes"]:
            node_id = str(node["id"])
            node_type = str(node["node_type"])
            membership = membership_by_node.get(node_id)
            if node_type in {"chapter", "appendix", "supplement"} and membership is None:
                findings.append(
                    (
                        "outline_coverage",
                        DissertationFinding(
                            defect_type="dissertation_outline_content_missing",
                            severity="major",
                            excerpt=str(node["title"]),
                            context=f"outline_node_type={node_type}",
                            rationale="The content-bearing outline node has no pinned Revision.",
                        ),
                        "dissertation_node",
                        node_id,
                    )
                )
                continue
            if membership is not None and str(membership["role"]) != node_type:
                findings.append(
                    (
                        "outline_role",
                        DissertationFinding(
                            defect_type="dissertation_membership_role_mismatch",
                            severity="critical",
                            excerpt=str(node["title"]),
                            context=f"node_type={node_type}; role={membership['role']}",
                            rationale="The pinned content role does not match its outline node.",
                        ),
                        "dissertation_membership",
                        str(membership["id"]),
                    )
                )
        for membership in memberships:
            if str(membership["status"]) == "update_available":
                findings.append(
                    (
                        "revision_pin",
                        DissertationFinding(
                            defect_type="dissertation_pinned_revision_stale",
                            severity="major",
                            excerpt=str(membership["content_title"]),
                            context=(
                                f"included={membership['included_revision_id']}; "
                                f"current={membership['current_content_revision_id']}"
                            ),
                            rationale=(
                                "The dissertation remains reproducibly pinned, but a newer "
                                "source Revision requires an explicit Change Request decision."
                            ),
                        ),
                        "dissertation_membership",
                        str(membership["id"]),
                    )
                )
        pinned_revisions = {str(item["included_revision_id"]) for item in memberships}
        included_documents = {str(item["content_document_id"]) for item in memberships}
        if pinned_revisions:
            placeholders = ",".join("?" for _ in pinned_revisions)
            references = self.connection.execute(
                f"""
                SELECT * FROM cross_references
                WHERE revision_id IN ({placeholders})
                """,
                tuple(sorted(pinned_revisions)),
            ).fetchall()
            for reference in references:
                status = str(reference["status"])
                target_document = str(reference["target_document_id"])
                if status != "valid":
                    findings.append(
                        (
                            "cross_reference",
                            DissertationFinding(
                                defect_type=f"dissertation_cross_reference_{status}",
                                severity="major",
                                excerpt=str(reference["label"]),
                                context=f"CrossReference status={status}",
                                rationale="A cross-reference in a pinned Revision is not valid.",
                            ),
                            "cross_reference",
                            str(reference["id"]),
                        )
                    )
                if target_document not in included_documents and target_document != document_id:
                    findings.append(
                        (
                            "cross_reference_scope",
                            DissertationFinding(
                                defect_type="dissertation_cross_reference_target_not_pinned",
                                severity="major",
                                excerpt=str(reference["label"]),
                                context=f"target_document_id={target_document}",
                                rationale=(
                                    "The internal CrossReference targets a document whose "
                                    "Revision is not pinned into this dissertation."
                                ),
                            ),
                            "cross_reference",
                            str(reference["id"]),
                        )
                    )
            findings.extend(self._claim_graph_consistency(pinned_revisions))
        return findings

    def _claim_graph_consistency(
        self, pinned_revisions: set[str]
    ) -> list[tuple[str, DissertationFinding, str, str]]:
        placeholders = ",".join("?" for _ in pinned_revisions)
        claims = self.connection.execute(
            f"SELECT * FROM claims WHERE revision_id IN ({placeholders})",
            tuple(sorted(pinned_revisions)),
        ).fetchall()
        claim_map = {str(claim["id"]): claim for claim in claims}
        findings: list[tuple[str, DissertationFinding, str, str]] = []
        grouped: dict[str, list[sqlite3.Row]] = {}
        for claim in claims:
            normalized = " ".join(str(claim["text"]).casefold().split())
            grouped.setdefault(normalized, []).append(claim)
        for equivalent in grouped.values():
            statuses = {str(claim["status"]) for claim in equivalent}
            modalities = {str(claim["modality"] or "") for claim in equivalent}
            if "supported" in statuses and "rejected" in statuses:
                claim = equivalent[0]
                findings.append(
                    (
                        "claim_consistency",
                        DissertationFinding(
                            defect_type="dissertation_claim_status_conflict",
                            severity="critical",
                            excerpt=str(claim["text"])[:1000],
                            context="statuses=" + ",".join(sorted(statuses)),
                            rationale=(
                                "Equivalent Claims in pinned revisions have conflicting status."
                            ),
                        ),
                        "claim",
                        str(claim["id"]),
                    )
                )
            if len(modalities) > 1:
                claim = equivalent[0]
                findings.append(
                    (
                        "modality_consistency",
                        DissertationFinding(
                            defect_type="dissertation_claim_modality_conflict",
                            severity="major",
                            excerpt=str(claim["text"])[:1000],
                            context="modalities=" + ",".join(sorted(modalities)),
                            rationale=(
                                "Equivalent Claims use inconsistent modality across pinned "
                                "revisions."
                            ),
                        ),
                        "claim",
                        str(claim["id"]),
                    )
                )
        adjacency: dict[str, set[str]] = {claim_id: set() for claim_id in claim_map}
        for relation in self.connection.execute(
            f"""
            SELECT r.* FROM claim_relations r
            JOIN claims source ON source.id = r.source_claim_id
            WHERE source.revision_id IN ({placeholders})
            """,
            tuple(sorted(pinned_revisions)),
        ):
            source = str(relation["source_claim_id"])
            target = str(relation["target_claim_id"])
            if target not in claim_map:
                findings.append(
                    (
                        "claim_scope",
                        DissertationFinding(
                            defect_type="dissertation_claim_relation_target_not_pinned",
                            severity="major",
                            excerpt=source,
                            context=f"target_claim_id={target}",
                            rationale=(
                                "A Claim relation leaves the set of pinned dissertation "
                                "revisions."
                            ),
                        ),
                        "claim",
                        source,
                    )
                )
            elif str(relation["relation_type"]) == "depends_on":
                adjacency[source].add(target)
        cycles: set[tuple[str, ...]] = set()

        def visit(node: str, path: list[str], active: set[str]) -> None:
            if node in active:
                cycles.add(tuple(sorted(path[path.index(node) :])))
                return
            active.add(node)
            path.append(node)
            for target in adjacency[node]:
                visit(target, path, active)
            path.pop()
            active.remove(node)

        for claim_id in sorted(claim_map):
            visit(claim_id, [], set())
        for cycle in sorted(cycles):
            findings.append(
                (
                    "claim_graph",
                    DissertationFinding(
                        defect_type="dissertation_claim_dependency_cycle",
                        severity="major",
                        excerpt=" -> ".join(cycle),
                        context="A cross-revision depends_on cycle was found.",
                        rationale="The whole-dissertation Claim dependency graph must be acyclic.",
                    ),
                    "claim",
                    cycle[0],
                )
            )
        return findings
