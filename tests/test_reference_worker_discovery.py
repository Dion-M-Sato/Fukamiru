"""Fixture-backed V29 worker dispatch and isolated SQLite persistence."""

from __future__ import annotations

import copy
import json
from dataclasses import replace

import pytest

from fukamiru.revisions.references import JapaneseReferenceService
from fukamiru.storage.payloads import PayloadStore
from fukamiru.workflow.graph import EdgeSpec, NodeSpec, WorkflowDefinition

HTML = (
    '<article id="DOC" lang="ja"><section id="REF-SEC">'
    '<h2 id="REF-H">参考文献</h2><p id="REF-1">ブルデュー『芸術の規則』</p>'
    "</section></article>"
)


@pytest.fixture
def worker_case():
    from test_worker import WorkerTests

    case = WorkerTests()
    case.setUp()
    try:
        yield case
    finally:
        case.tearDown()


def _candidate(candidate_id: str, isbn: str, year: str, volume: str) -> dict:
    return {
        "candidate_id": candidate_id,
        "title": "芸術の規則",
        "record_type": "book",
        "publisher": "藤原書店",
        "year": year,
        "volume": volume,
        "isbn": isbn,
        "publication_language": "ja",
        "provider": "ndl_search",
        "url": f"https://ndlsearch.ndl.go.jp/books/{candidate_id}",
        "responsibility_sources": [
            {
                "provider": "ndl_search",
                "url": f"https://ndlsearch.ndl.go.jp/books/{candidate_id}",
                "responsibility_source": "exact_record_detail",
                "responsibility_raw": ["ピエール・ブルデュー[著]", "石井, 洋二郎訳"],
            }
        ],
    }


@pytest.mark.parametrize(
    "volume,expected_state",
    [
        (None, "MULTIPLE_MANIFESTATIONS_READY"),
        ("2", "SINGLE_MANIFESTATION_RESOLVED"),
        ("unready", "SINGLE_MANIFESTATION_RESOLVED"),
    ],
)
def test_worker_node8_persistence_node21_node22(
    worker_case, monkeypatch, volume: str | None, expected_state: str
) -> None:
    definition = WorkflowDefinition(
        name="manuscript_rewrite",
        version="29",
        title="Isolated V29 reference worker fixture",
        entry_node="resolve_reference_metadata",
        nodes=[
            NodeSpec(
                id="resolve_reference_metadata",
                handler="manuscript_rewrite.resolve_reference_metadata",
                title="Resolve references",
            ),
            NodeSpec(
                id="render_manuscript_references",
                handler="manuscript_rewrite.render_references",
                title="Render references",
            ),
            NodeSpec(
                id="validate_reference_quality",
                handler="manuscript_rewrite.reference_quality_gate",
                title="Validate references",
            ),
        ],
        edges=[
            EdgeSpec(source="resolve_reference_metadata", target="render_manuscript_references"),
            EdgeSpec(source="render_manuscript_references", target="validate_reference_quality"),
        ],
    )
    worker_case.workflows["manuscript_rewrite"] = definition
    worker_case.worker.workflows["manuscript_rewrite"] = definition
    worker_case.worker.workflow_versions[(definition.name, definition.version)] = definition
    candidates = [
        _candidate("vol1", "4-89434-009-7", "1995", "1"),
        _candidate("vol2", "4-89434-030-5", "1996", "2"),
    ]
    if volume == "unready":
        candidates[1]["responsibility_sources"] = []
        candidates[1]["authors"] = []

    def fixture_resolution(cls, entry: dict, _hits: list, *, workflow_version: int) -> dict:
        del cls, workflow_version
        return {
            **copy.deepcopy(entry),
            "candidate_matches": copy.deepcopy(candidates),
            "expanded_reference_entries": [],
            "resolution_status": "manifestations_retained",
            "required_missing_fields": [],
        }

    monkeypatch.setattr(
        JapaneseReferenceService,
        "resolve_with_candidates",
        classmethod(fixture_resolution),
    )
    source = {
        "reference_id": "r-art",
        "source_block_id": "REF-1",
        "entry_ordinal": 0,
        "parse_status": "parsed",
        "original_text": "ブルデュー『芸術の規則』",
        "title": "芸術の規則",
        "author": "ブルデュー",
        "record_type": "book_or_journal",
        "provenance": {
            "title": {"kind": "source_explicit", "label": "原稿に明記"},
            "author": {"kind": "source_explicit", "label": "原稿に明記"},
        },
        "selection_evidence": {"volume": volume} if volume == "2" else {},
    }
    run_id = worker_case._create_run(
        "manuscript_rewrite",
        extra_inputs={
            "reference_entries": [source],
            "proposed_html": HTML,
            "use_online_research": False,
            "reference_rule_set": {"id": "fixture-v29"},
        },
    )
    assert all(worker_case.worker.run_once(lane="ingestion_cpu") for _ in range(3))
    with worker_case.databases.state.connect() as connection:
        rows = connection.execute(
            "SELECT node_id, status, output_json FROM node_runs WHERE run_id=? ORDER BY rowid",
            (run_id,),
        ).fetchall()
        store = PayloadStore(connection)
        outputs = {
            row["node_id"]: store.unpack(json.loads(row["output_json"]))
            for row in rows
            if row["output_json"]
        }
    assert [row["status"] for row in rows] == ["completed"] * 3
    resolved = outputs["resolve_reference_metadata"]["reference_entries"][0]
    assert resolved["reference_state_v29"] == expected_state
    assert outputs["render_manuscript_references"]["reference_render_report"]
    issues = outputs["validate_reference_quality"]["reference_quality_gate"]["issues"]
    target_codes = {item["code"] for item in issues if item.get("reference_id") == "r-art"}
    if volume is None:
        assert not target_codes
        assert len(resolved["expanded_reference_entries"]) == 2
    elif volume == "2":
        assert resolved["selected_candidate_id"] == "vol2"
        assert resolved["year"] == "1996"
        assert resolved["isbn"] == "4-89434-030-5"
        assert target_codes == set()
    else:
        assert resolved["selected_candidate_id"] == "vol1"
        assert resolved["output_candidate_count"] == 1
        assert len(resolved["review_leads"]) == 1
        assert not resolved["expanded_reference_entries"]
        assert target_codes == set()


def test_worker_online_node8_dispatch_persists_through_node22(
    worker_case,
    monkeypatch,
) -> None:
    """Exercise the real queued worker route with deterministic online evidence."""
    definition = WorkflowDefinition(
        name="manuscript_rewrite",
        version="29",
        title="V29 online worker fixture",
        entry_node="resolve_reference_metadata",
        nodes=[
            NodeSpec(
                id="resolve_reference_metadata",
                handler="manuscript_rewrite.resolve_reference_metadata",
                title="Resolve",
            ),
            NodeSpec(
                id="render_manuscript_references",
                handler="manuscript_rewrite.render_references",
                title="Render",
            ),
            NodeSpec(
                id="validate_reference_quality",
                handler="manuscript_rewrite.reference_quality_gate",
                title="Validate",
            ),
        ],
        edges=[
            EdgeSpec(source="resolve_reference_metadata", target="render_manuscript_references"),
            EdgeSpec(source="render_manuscript_references", target="validate_reference_quality"),
        ],
    )
    worker_case.workflows["manuscript_rewrite"] = definition
    worker_case.worker.workflows["manuscript_rewrite"] = definition
    worker_case.worker.workflow_versions[(definition.name, definition.version)] = definition
    worker_case.worker.settings = replace(
        worker_case.settings,
        translation=replace(worker_case.settings.translation, academic_research_online=True),
    )
    monkeypatch.setattr(
        worker_case.worker,
        "_manuscript_inference_backend",
        lambda *_args: None,
    )
    discovery_calls = []

    def fake_discover(entry, providers, hypotheses=None, *, workflow_version):
        discovery_calls.append((workflow_version, [provider.name for provider in providers]))
        return {
            **copy.deepcopy(entry),
            "candidate_matches": [
                _candidate("vol1", "4-89434-009-7", "1995", "1"),
                _candidate("vol2", "4-89434-030-5", "1996", "2"),
            ],
            "expanded_reference_entries": [],
            "resolution_status": "manifestations_retained",
            "search_attempts": [{"provider": "fixture", "attempts": []}],
            "required_missing_fields": [],
        }

    monkeypatch.setattr("fukamiru.worker.runner.discover_reference", fake_discover)
    source = {
        "reference_id": "r-art",
        "source_block_id": "REF-1",
        "entry_ordinal": 0,
        "parse_status": "parsed",
        "original_text": "ブルデュー『芸術の規則』",
        "title": "芸術の規則",
        "author": "ブルデュー",
        "record_type": "book_or_journal",
        "provenance": {
            "title": {"kind": "source_explicit"},
            "author": {"kind": "source_explicit"},
        },
    }
    run_id = worker_case._create_run(
        "manuscript_rewrite",
        extra_inputs={
            "reference_entries": [source],
            "proposed_html": HTML,
            "use_online_research": True,
            "reference_rule_set": {"id": "fixture-v29"},
        },
    )
    assert all(worker_case.worker.run_once(lane="ingestion_cpu") for _ in range(3))
    with worker_case.databases.state.connect() as connection:
        rows = connection.execute(
            "SELECT node_id, status, output_json FROM node_runs WHERE run_id=? ORDER BY rowid",
            (run_id,),
        ).fetchall()
        store = PayloadStore(connection)
        outputs = {
            row["node_id"]: store.unpack(json.loads(row["output_json"]))
            for row in rows
            if row["output_json"]
        }
    assert [row["status"] for row in rows] == ["completed"] * 3
    assert discovery_calls == [
        (
            29,
            [
                "ndl_search",
                "open_library",
                "openalex",
                "crossref",
                "semantic_scholar",
            ],
        )
    ]
    assert (
        outputs["resolve_reference_metadata"]["reference_entries"][0]["reference_state_v29"]
        == "MULTIPLE_MANIFESTATIONS_READY"
    )
    assert outputs["render_manuscript_references"]["reference_render_report"]
    assert "reference_quality_gate" in outputs["validate_reference_quality"]


def test_worker_phase_f_three_track_persistence_e2e(
    worker_case,
    monkeypatch,
) -> None:
    """Run one Phase F fixture per track through the queued worker and SQLite."""
    definition = WorkflowDefinition(
        name="manuscript_rewrite",
        version="29",
        title="V29 Phase F worker fixture",
        entry_node="resolve_reference_metadata",
        nodes=[
            NodeSpec(
                id="resolve_reference_metadata",
                handler="manuscript_rewrite.resolve_reference_metadata",
                title="Resolve",
            ),
            NodeSpec(
                id="render_manuscript_references",
                handler="manuscript_rewrite.render_references",
                title="Render",
            ),
            NodeSpec(
                id="validate_reference_quality",
                handler="manuscript_rewrite.reference_quality_gate",
                title="Validate",
            ),
        ],
        edges=[
            EdgeSpec(source="resolve_reference_metadata", target="render_manuscript_references"),
            EdgeSpec(source="render_manuscript_references", target="validate_reference_quality"),
        ],
    )
    worker_case.workflows["manuscript_rewrite"] = definition
    worker_case.worker.workflows["manuscript_rewrite"] = definition
    worker_case.worker.workflow_versions[(definition.name, definition.version)] = definition
    worker_case.worker.settings = replace(
        worker_case.settings,
        translation=replace(worker_case.settings.translation, academic_research_online=True),
    )
    monkeypatch.setattr(
        worker_case.worker,
        "_manuscript_inference_backend",
        lambda *_args: None,
    )

    def person(name: str, role: str = "author") -> dict:
        parts = name.split()
        western = len(parts) > 1
        return {
            "role": role,
            "display_name": name,
            "name_profile": "western_latin" if western else "east_asian",
            "family": parts[-1] if western else name,
            "given": " ".join(parts[:-1]) if western else None,
            "responsibility_role_confirmed": True,
            "person_display_name_confirmed": True,
            "person_authority_confirmed": False,
        }

    def book(candidate_id: str, title: str, year: str, language: str = "en") -> dict:
        return {
            "candidate_id": candidate_id,
            "title": title,
            "record_type": "book",
            "publisher": "Fixture Press",
            "year": year,
            "publication_language": language,
            "authors": ["Gilbert Simondon"],
            "url": f"https://catalog.example/{candidate_id}",
            "bibliography_people": [person("Gilbert Simondon")],
        }

    relation_translation = {
        **book("a-ja", "技術的対象の存在様態について", "2024", "ja"),
        "authors": ["Gilbert Simondon"],
        "original_title": "Du mode d'existence des objets techniques",
        "original_title_relation_verified": True,
        "original_title_audit": [
            {
                "original_title": "Du mode d'existence des objets techniques",
                "source_url": "https://catalog.example/a-ja",
                "parser_rule": "ndl_original_title_section",
            }
        ],
        "bibliography_people": [person("Gilbert Simondon")],
    }
    track_candidates = {
        "track-a": [
            relation_translation,
            book("a-1", "Du mode d'existence des objets techniques", "1958"),
            book("a-2", "Du mode d'existence des objets techniques", "2012"),
        ],
        "track-b": [
            {
                "candidate_id": "b-1",
                "title": "性の歴史",
                "volume": "1",
                "record_type": "book",
                "publisher": "新潮社",
                "year": "1986",
                "publication_language": "ja",
                "authors": ["ミシェル・フーコー"],
                "translators": ["渡辺守章"],
                "is_translation": True,
                "url": "https://catalog.example/b-1",
                "responsibility_sources": [
                    {
                        "provider": "fixture",
                        "url": "https://catalog.example/b-1",
                        "responsibility_source": "exact_record_detail",
                        "responsibility_raw": ["フーコー, ミシェル[著]", "渡辺, 守章訳"],
                    }
                ],
            }
        ],
        "track-c": [
            {
                "candidate_id": "c-1",
                "title": "架空資料の比較分析",
                "record_type": "nonstandard_contribution",
                "year": "2022",
                "publication_language": "ja",
                "authors": ["山田花子"],
                "container_title": "架空論集",
                "pages": None,
                "pages_status": "unknown_allowed",
                "pages_required": False,
                "url": "https://research.example/c-1",
                "responsibility_sources": [
                    {
                        "provider": "fixture",
                        "url": "https://research.example/c-1",
                        "responsibility_source": "web_explicit_author_metadata",
                        "responsibility_raw": ["山田, 花子[著]"],
                    }
                ],
                "field_provenance": {
                    "title": [{"source": "https://research.example/c-1"}],
                    "container_title": [{"source": "https://shop.example/container"}],
                },
            }
        ],
    }

    def fake_discover(entry, providers, hypotheses=None, *, workflow_version):
        del providers, hypotheses
        assert workflow_version == 29
        return {
            **copy.deepcopy(entry),
            "candidate_matches": copy.deepcopy(track_candidates[entry["reference_id"]]),
            "expanded_reference_entries": [],
            "resolution_status": "manifestations_retained",
            "search_attempts": [{"provider": "fixture", "attempts": []}],
            "required_missing_fields": [],
        }

    monkeypatch.setattr("fukamiru.worker.runner.discover_reference", fake_discover)
    sources = [
        {
            "reference_id": "track-a",
            "source_block_id": "REF-A",
            "entry_ordinal": 0,
            "parse_status": "parsed",
            "original_text": "技術的対象の存在様式について",
            "title": "技術的対象の存在様式について",
            "author": "Gilbert Simondon",
            "record_type": "book_or_journal",
            "provenance": {
                "title": {"kind": "source_explicit", "label": "原稿に明記"},
                "author": {"kind": "source_explicit", "label": "原稿に明記"},
            },
        },
        {
            "reference_id": "track-b",
            "source_block_id": "REF-B",
            "entry_ordinal": 1,
            "parse_status": "parsed",
            "original_text": "性の歴史Ⅰ",
            "title": "性の歴史Ⅰ",
            "author": "フーコー",
            "record_type": "book_or_journal",
            "provenance": {
                "title": {"kind": "source_explicit", "label": "原稿に明記"},
                "author": {"kind": "source_explicit", "label": "原稿に明記"},
            },
        },
        {
            "reference_id": "track-c",
            "source_block_id": "REF-C",
            "entry_ordinal": 2,
            "parse_status": "parsed",
            "original_text": "架空資料の比較分析",
            "title": "架空資料の比較分析",
            "author": "山田花子",
            "record_type": "other_contribution",
            "provenance": {
                "title": {"kind": "source_explicit", "label": "原稿に明記"},
                "author": {"kind": "source_explicit", "label": "原稿に明記"},
            },
        },
    ]
    html = (
        '<article id="DOC" lang="ja"><section id="REF-SEC"><h2 id="REF-H">参考文献</h2>'
        '<p id="REF-A">技術的対象の存在様式について</p>'
        '<p id="REF-B">性の歴史Ⅰ</p>'
        '<p id="REF-C">架空資料の比較分析</p>'
        "</section></article>"
    )
    run_id = worker_case._create_run(
        "manuscript_rewrite",
        extra_inputs={
            "reference_entries": sources,
            "proposed_html": html,
            "use_online_research": True,
            "reference_rule_set": {"id": "phase-f-v29"},
        },
    )
    processed = [worker_case.worker.run_once(lane="ingestion_cpu") for _ in range(3)]
    with worker_case.databases.state.connect() as connection:
        rows = connection.execute(
            "SELECT node_id, status, output_json FROM node_runs WHERE run_id=? ORDER BY rowid",
            (run_id,),
        ).fetchall()
        store = PayloadStore(connection)
        outputs = {
            row["node_id"]: store.unpack(json.loads(row["output_json"]))
            for row in rows
            if row["output_json"]
        }
    assert processed == [True] * 3
    assert [row["status"] for row in rows] == ["completed"] * 3
    resolved = {
        item["reference_id"]: item
        for item in outputs["resolve_reference_metadata"]["reference_entries"]
    }
    assert resolved["track-a"]["reference_output_mode_v29"] == "MULTIPLE"
    assert resolved["track-b"]["reference_output_mode_v29"] == "SINGLE"
    assert resolved["track-c"]["reference_output_mode_v29"] == "SINGLE"
    assert resolved["track-c"]["candidate_matches"][0]["pages_status"] == "unknown_allowed"
    assert outputs["render_manuscript_references"]["reference_render_report"]
    issues = outputs["validate_reference_quality"]["reference_quality_gate"]["issues"]
    assert not [item for item in issues if item.get("reference_id") in resolved]


@pytest.fixture
def fictional_http_provider(monkeypatch):
    """Exercise real loopback HTTP while refusing every external URL."""
    import threading
    import urllib.parse
    import urllib.request
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    from fukamiru.literature.service import LiteratureCandidate

    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query).get("q", [""])[0]
            requests.append(query)
            if "記録" in query or "Records" in query or "Bob" in query:
                title, author = "Synthetic Records", "Bob Example"
            elif "比較分析" in query or "山田" in query:
                title, author = "架空資料の比較分析", "山田花子"
            else:
                title, author = "Synthetic Organization", "Alice Example"
            body = json.dumps({"title": title, "author": author}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    endpoint = f"http://127.0.0.1:{server.server_port}"
    original_open = urllib.request.urlopen

    def loopback_only(url, *args, **kwargs):
        target = url.full_url if isinstance(url, urllib.request.Request) else str(url)
        assert urllib.parse.urlsplit(target).netloc == urllib.parse.urlsplit(endpoint).netloc
        return original_open(url, *args, **kwargs)

    monkeypatch.setattr(urllib.request, "urlopen", loopback_only)

    class Provider:
        name = "fictional_catalog"

        def __init__(self, **_kwargs):
            pass

        def search(self, query, *, limit):
            assert limit > 0
            with urllib.request.urlopen(
                endpoint + "/search?" + urllib.parse.urlencode({"q": query}), timeout=3
            ) as response:
                record = json.load(response)
            title, author = record["title"], record["author"]
            url = "https://catalog.example.test/fictional-record"
            return [
                LiteratureCandidate(
                    provider=self.name,
                    provider_record_id=title,
                    title=title,
                    authors=(author,),
                    publication_title=None,
                    publisher="Example Press",
                    publication_date="2001",
                    record_type="book",
                    doi=None,
                    url=url,
                    oa_url=None,
                    oa_status=None,
                    license_url=None,
                    provider_payload={
                        "publication_language": "ja" if author == "山田花子" else "en",
                        "responsibility_sources": [
                            {
                                "provider": self.name,
                                "url": url,
                                "responsibility_source": "exact_record_detail",
                                "responsibility_raw": [author + " 著"],
                            }
                        ],
                    },
                )
            ]

    for name in (
        "NDLSearchProvider",
        "OpenLibraryProvider",
        "OpenAlexProvider",
        "CrossrefProvider",
        "SemanticScholarProvider",
    ):
        monkeypatch.setattr("fukamiru.worker.runner." + name, Provider)
    monkeypatch.setattr(
        "fukamiru.revisions.reference_search.general_web_search", lambda *_a, **_k: []
    )
    monkeypatch.setattr(
        "fukamiru.revisions.reference_search.publisher_page",
        lambda url: {"url": url, "status": "synthetic_unavailable"},
    )
    try:
        yield endpoint
        assert requests, "The worker must perform real HTTP requests"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def test_worker_http_provider_persists_reference_results(
    worker_case,
    monkeypatch,
    fictional_http_provider,
) -> None:
    """Persist fictional HTTP provider results through the actual worker graph."""
    assert fictional_http_provider
    definition = WorkflowDefinition(
        name="manuscript_rewrite",
        version="29",
        title="V29 Phase G live worker",
        entry_node="resolve_reference_metadata",
        nodes=[
            NodeSpec(
                id="resolve_reference_metadata",
                handler="manuscript_rewrite.resolve_reference_metadata",
                title="Resolve",
            ),
            NodeSpec(
                id="render_manuscript_references",
                handler="manuscript_rewrite.render_references",
                title="Render",
            ),
            NodeSpec(
                id="validate_reference_quality",
                handler="manuscript_rewrite.reference_quality_gate",
                title="Validate",
            ),
        ],
        edges=[
            EdgeSpec(source="resolve_reference_metadata", target="render_manuscript_references"),
            EdgeSpec(source="render_manuscript_references", target="validate_reference_quality"),
        ],
    )
    worker_case.workflows["manuscript_rewrite"] = definition
    worker_case.worker.workflows["manuscript_rewrite"] = definition
    worker_case.worker.workflow_versions[(definition.name, definition.version)] = definition
    worker_case.worker.settings = replace(
        worker_case.settings,
        translation=replace(
            worker_case.settings.translation,
            academic_research_online=True,
            academic_research_timeout_seconds=5,
        ),
    )
    query_map = {
        "g-6": (["Synthetic Organization"], ["Alice Example"]),
        "g-7": (
            ["Synthetic Records"],
            ["Bob Example"],
        ),
        "g-35": ([], ["山田花子"]),
    }

    def inference_backend(_claimed, method_name, _context=None):
        def plan(selected, **_kwargs):
            if method_name == "infer_reference_entries":
                return {
                    "inferences": [
                        {
                            "reference_id": item["reference_id"],
                            "fields": {
                                "record_type": None,
                                "publisher": None,
                                "publication_place": None,
                                "series": None,
                                "year": None,
                                "container_title": None,
                                "volume": None,
                                "issue": None,
                                "pages": None,
                            },
                            "rationale": "No unsupported fields are inferred.",
                            "confidence": 0.0,
                        }
                        for item in selected
                    ]
                }
            return {
                "queries": [
                    {
                        "reference_id": item["reference_id"],
                        "titles": query_map[item["reference_id"]][0],
                        "authors": query_map[item["reference_id"]][1],
                    }
                    for item in selected
                ]
            }

        return (
            plan
            if method_name
            in {
                "plan_reference_queries",
                "infer_reference_entries",
            }
            else None
        )

    monkeypatch.setattr(worker_case.worker, "_manuscript_inference_backend", inference_backend)

    def invoke_inference(_claimed, method_name, selected=None, **arguments):
        backend = inference_backend(_claimed, method_name)
        assert callable(backend)
        return backend(selected or arguments.get("entries") or [], **arguments)

    monkeypatch.setattr(worker_case.worker, "_invoke_manuscript_inference", invoke_inference)
    monkeypatch.setattr(
        worker_case.worker.translator,
        "infer_reference_entries",
        lambda entries, rule_set: inference_backend(None, "infer_reference_entries")(
            entries,
            rule_set=rule_set,
        ),
        raising=False,
    )
    sources = [
        {
            "reference_id": "g-6",
            "source_block_id": "REF-6",
            "entry_ordinal": 0,
            "parse_status": "parsed",
            "original_text": "アリス・エグザンプル『架空の運営研究』",
            "title": "架空の運営研究",
            "author": "アリス・エグザンプル",
            "record_type": "book_or_journal",
        },
        {
            "reference_id": "g-7",
            "source_block_id": "REF-7",
            "entry_ordinal": 1,
            "parse_status": "parsed",
            "original_text": "ボブ・エグザンプル『架空の記録研究』",
            "title": "架空の記録研究",
            "author": "ボブ・エグザンプル",
            "record_type": "book_or_journal",
        },
        {
            "reference_id": "g-35",
            "source_block_id": "REF-35",
            "entry_ordinal": 2,
            "parse_status": "parsed",
            "original_text": "自著『架空資料の比較分析』",
            "title": "架空資料の比較分析",
            "author": "自著",
            "record_type": "other_contribution",
            "is_self_authored": True,
        },
    ]
    html = (
        '<article id="DOC" lang="ja"><section id="REF-SEC"><h2 id="REF-H">参考文献</h2>'
        '<p id="REF-6">架空の運営研究</p><p id="REF-7">架空の記録研究</p>'
        '<p id="REF-35">架空資料の比較分析</p></section></article>'
    )
    run_id = worker_case._create_run(
        "manuscript_rewrite",
        extra_inputs={
            "reference_entries": sources,
            "proposed_html": html,
            "use_online_research": True,
            "reference_rule_set": {"id": "phase-g-v29"},
        },
    )
    processed = [worker_case.worker.run_once(lane="ingestion_cpu") for _ in range(3)]
    with worker_case.databases.state.connect() as connection:
        rows = connection.execute(
            "SELECT node_id,status,output_json,error_json "
            "FROM node_runs WHERE run_id=? ORDER BY rowid",
            (run_id,),
        ).fetchall()
        store = PayloadStore(connection)
        outputs = {
            row["node_id"]: store.unpack(json.loads(row["output_json"]))
            for row in rows
            if row["output_json"]
        }
    assert processed == [True] * 3, [
        (row["node_id"], row["status"], row["error_json"]) for row in rows
    ]
    assert [row["status"] for row in rows] == ["completed"] * 3
    resolved = outputs["resolve_reference_metadata"]["reference_entries"]
    assert {item["reference_id"] for item in resolved} == {"g-6", "g-7", "g-35"}
    assert all(item.get("search_attempts") for item in resolved)
    assert outputs["render_manuscript_references"]["reference_render_report"]
    assert "reference_quality_gate" in outputs["validate_reference_quality"]


def test_worker_http_provider_foreign_fallback(
    worker_case,
    monkeypatch,
    fictional_http_provider,
) -> None:
    """Verify foreign fallback through HTTP, SQLite, rendering and validation."""
    assert fictional_http_provider
    definition = WorkflowDefinition(
        name="manuscript_rewrite",
        version="29",
        title="V29 Phase H live worker",
        entry_node="resolve_reference_metadata",
        nodes=[
            NodeSpec(
                id="resolve_reference_metadata",
                handler="manuscript_rewrite.resolve_reference_metadata",
                title="Resolve",
            ),
            NodeSpec(
                id="render_manuscript_references",
                handler="manuscript_rewrite.render_references",
                title="Render",
            ),
            NodeSpec(
                id="validate_reference_quality",
                handler="manuscript_rewrite.reference_quality_gate",
                title="Validate",
            ),
        ],
        edges=[
            EdgeSpec(source="resolve_reference_metadata", target="render_manuscript_references"),
            EdgeSpec(source="render_manuscript_references", target="validate_reference_quality"),
        ],
    )
    worker_case.workflows["manuscript_rewrite"] = definition
    worker_case.worker.workflows["manuscript_rewrite"] = definition
    worker_case.worker.workflow_versions[(definition.name, definition.version)] = definition
    worker_case.worker.settings = replace(
        worker_case.settings,
        translation=replace(
            worker_case.settings.translation,
            academic_research_online=True,
            academic_research_timeout_seconds=7,
        ),
    )

    def inference_backend(_claimed, method_name, _context=None):
        def plan(selected, **_kwargs):
            if method_name == "infer_reference_entries":
                return {
                    "inferences": [
                        {
                            "reference_id": item["reference_id"],
                            "fields": {
                                "record_type": None,
                                "publisher": None,
                                "publication_place": None,
                                "series": None,
                                "year": None,
                                "container_title": None,
                                "volume": None,
                                "issue": None,
                                "pages": None,
                            },
                            "rationale": "No unsupported fields are inferred.",
                            "confidence": 0.0,
                        }
                        for item in selected
                    ]
                }
            return {
                "queries": [
                    {
                        "reference_id": item["reference_id"],
                        "titles": ["Synthetic Organization"],
                        "authors": ["Alice Example"],
                    }
                    for item in selected
                ]
            }

        return (
            plan
            if method_name
            in {
                "plan_reference_queries",
                "infer_reference_entries",
            }
            else None
        )

    monkeypatch.setattr(worker_case.worker, "_manuscript_inference_backend", inference_backend)

    def invoke_inference(_claimed, method_name, selected=None, **arguments):
        backend = inference_backend(_claimed, method_name)
        assert callable(backend)
        return backend(selected or arguments.get("entries") or [], **arguments)

    monkeypatch.setattr(worker_case.worker, "_invoke_manuscript_inference", invoke_inference)
    monkeypatch.setattr(
        worker_case.worker.translator,
        "infer_reference_entries",
        lambda entries, rule_set: inference_backend(None, "infer_reference_entries")(
            entries,
            rule_set=rule_set,
        ),
        raising=False,
    )
    source = {
        "reference_id": "h-6",
        "source_block_id": "REF-6",
        "entry_ordinal": 0,
        "parse_status": "parsed",
        "original_text": "アリス・エグザンプル『架空の運営研究』",
        "title": "架空の運営研究",
        "author": "アリス・エグザンプル",
        "record_type": "book_or_journal",
        "provenance": {
            "title": {"kind": "source_explicit", "label": "原稿に明記"},
            "author": {"kind": "source_explicit", "label": "原稿に明記"},
        },
    }
    html = (
        '<article id="DOC" lang="ja"><section id="REF-SEC"><h2 id="REF-H">参考文献</h2>'
        '<p id="REF-6">架空の運営研究</p></section></article>'
    )
    run_id = worker_case._create_run(
        "manuscript_rewrite",
        extra_inputs={
            "reference_entries": [source],
            "proposed_html": html,
            "use_online_research": True,
            "reference_rule_set": {"id": "phase-h-v29"},
        },
    )
    processed = [worker_case.worker.run_once(lane="ingestion_cpu") for _ in range(3)]
    with worker_case.databases.state.connect() as connection:
        rows = connection.execute(
            "SELECT node_id,status,output_json,error_json "
            "FROM node_runs WHERE run_id=? ORDER BY rowid",
            (run_id,),
        ).fetchall()
        store = PayloadStore(connection)
        outputs = {
            row["node_id"]: store.unpack(json.loads(row["output_json"]))
            for row in rows
            if row["output_json"]
        }
    assert processed == [True] * 3, [
        (row["node_id"], row["status"], row["error_json"]) for row in rows
    ]
    assert [row["status"] for row in rows] == ["completed"] * 3
    resolved = outputs["resolve_reference_metadata"]["reference_entries"][0]
    if resolved["reference_output_mode_v29"] != "SINGLE":
        # An unavailable catalog cannot establish a particular manifestation.
        # The live test must prove a reasoned rejection, not require fabricated
        # metadata during an outage. Node execution still has to complete above.
        assert resolved["reference_output_mode_v29"] in {"REVIEW", "UNRESOLVED"}
        assert any(
            row["provider"] == "open_library" and row["status"] == "provider_unavailable"
            for row in resolved["provider_statuses"]
        ), resolved["provider_statuses"]
        assert "provider_unavailable" in resolved["search_completeness"]["incompleteness_reasons"]
        assert resolved["title"] == source["title"]
        assert resolved.get("foreign_manifestation_fallback_used") is not True
        assert resolved.get("selection_basis") != "foreign_manifestation_fallback"
        assert not resolved.get("expanded_reference_entries")
        assert resolved["v29_required_missing_fields"]
        assert resolved["v29_review_reasons"]
        assert resolved["user_action_required"]
        assert resolved["final_revision_blocked"] is True
        gate = outputs["validate_reference_quality"]["reference_quality_gate"]
        assert gate["passed"] is False
        assert any(
            item["code"] == "reference.final_revision_blocked" and item.get("severity") == "error"
            for item in gate["issues"]
        )
        return
    assert resolved["reference_output_mode_v29"] == "SINGLE"
    assert resolved["selection_basis"] == "foreign_manifestation_fallback"
    assert resolved["foreign_manifestation_fallback_used"] is True
    assert resolved["translation_side_discovery_completed"] is True
    assert resolved["title"].casefold() == "synthetic organization"
    rendered = outputs["render_manuscript_references"]["proposed_html"]
    assert "Synthetic Organization" in rendered
    target_issues = [
        item
        for item in outputs["validate_reference_quality"]["reference_quality_gate"]["issues"]
        if str(item.get("reference_id") or "").startswith("h-6")
    ]
    assert not [item for item in target_issues if item.get("severity") == "error"]
