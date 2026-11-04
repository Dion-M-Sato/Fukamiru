"""Public source discovery and the actual OCR-to-RAG handoff contract."""

from __future__ import annotations

import io
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from pypdf import PdfReader, PdfWriter
from pypdf.generic import NameObject, TextStringObject

from fukamiru.authoring.source_ingestion import ResearchSourceIngestion
from fukamiru.authoring.source_reads import known_work_read, reuse_local_passages
from fukamiru.literature.content import read_oa_pdf
from fukamiru.literature.service import LiteratureCandidate
from fukamiru.literature.web import _Body, supplement_queries, validate_endpoint
from fukamiru.ocr.models import OCRPage, OCRResult
from fukamiru.ocr.service import PDFOCRService
from fukamiru.revisions.references import JapaneseReferenceService
from fukamiru.storage.runs import RunRepository
from fukamiru.workflow.templates import research_source_ingestion_workflow


@pytest.fixture
def case():
    from test_worker import WorkerTests

    value = WorkerTests()
    value.setUp()
    yield value
    value.tearDown()


def enabled(case):
    case.worker.settings = replace(case.settings, translation=replace(
        case.settings.translation, academic_research_online=True))


def claim_task(case):
    case._create_run("manuscript_rewrite")
    return replace(case.worker._claim(lane="ingestion_cpu"),
                   workflow_name="manuscript_generator", workflow_version="32")


def source_record():
    return {"provider": "openalex", "provider_record_id": "work", "title": "食事の研究",
            "source_url": "https://example.org/study", "source_type": "online_paper",
            "authors": ["研究者"], "query": "食事 文化", "excerpt": "食事文化を調査した。",
            "provenance": {"doi": "10.1234/food", "record_type": "article",
                           "publication_date": "2024", "publication_title": "文化研究"}}


def test_supplement_uses_short_supplied_terms_and_preserves_book_kind():
    plan = {"query": "ブックオフ 読書 棚分類 ジェンダー", "source_kind": "mixed",
            "variants": ["ブックオフ 文化", "secondhand book shelving gender"]}
    assert supplement_queries(plan)[0] == "ブックオフ 読書"
    assert "ブックオフ 文化" in supplement_queries(plan)
    assert plan["source_kind"] == "mixed"


@pytest.mark.parametrize("url", ["https://example.org", "http://127.0.0.1:123/a",
                                      "http://user@localhost", "file:///tmp/search"])
def test_rustxng_endpoint_is_only_a_configured_loopback_service(url):
    with pytest.raises(ValueError):
        validate_endpoint(url)


def test_visible_page_text_excludes_script_and_navigation():
    parser = _Body()
    parser.feed("<nav>別の資料</nav><article><p>調査結果。</p></article><script>偽の指示</script>")
    assert "調査結果。" in "".join(parser.parts)
    assert "別の資料" not in "".join(parser.parts)
    assert "偽の指示" not in "".join(parser.parts)


@pytest.mark.parametrize("payload", [
    {"results": [], "error": "all engines failed"},
    {"results": [], "unresponsive_engines": [["bing", "Too many requests"]]},
])
def test_engine_outage_is_not_cached_as_a_successful_empty_search(monkeypatch, payload):
    import fukamiru.literature.web as web

    monkeypatch.setattr(web.urllib.request, "build_opener", lambda *a: SimpleNamespace(
        open=lambda *a, **k: io.BytesIO(json.dumps(payload).encode())))
    with pytest.raises(ValueError, match="RustXNG"):
        web.rustxng_search("food", endpoint="http://127.0.0.1:29483", timeout=1)


def test_covered_rag_claim_never_calls_auxiliary_web(case, monkeypatch):
    import fukamiru.worker.runner as runner

    enabled(case)
    task = claim_task(case)
    monkeypatch.setattr(runner, "rustxng_search", lambda *a, **k: pytest.fail("covered claim"))
    context = {"use_online_research": True, "research_search_plan": [{
        "obligation_id": "O", "query": "食事 文化", "variants": ["food culture"]}],
        "evidence_coverage": [{"obligation_id": "O", "covered": True}]}
    result = case.worker._authoring_supplement_research(task, context)
    assert result["supplemental_web_report"] == []


def test_planner_article_label_does_not_force_research_when_book_already_supports_claim(
    case, monkeypatch,
):
    task = claim_task(case)
    monkeypatch.setattr(case.worker, "_authoring_generate",
                        lambda *a, **k: pytest.fail("no remaining evidence gap"))
    plan = {"obligation_id": "O", "query": "食事 文化", "variants": ["food culture"],
            "source_kind": "article", "origin": "authoring_atomic_proposition"}
    result = case.worker._authoring_bounded_research_retry(task, {
        "use_rag": True, "use_online_research": True, "research_retrieval_stage": "local_first",
        "research_query_assignments": [plan], "research_search_plan": [plan],
        "evidence_coverage": [{"obligation_id": "O", "covered": True}],
        "evidence_context": [{"query": plan["query"], "source_type": "local_rag",
                              "provenance": {"citation": {"record_type": "book"}}}],
    }).context
    assert result["bounded_research_retry_report"]["attempted"] is False


def test_unknown_book_is_kept_as_metadata_lead_and_successful_reads_are_shared(case, monkeypatch):
    import fukamiru.worker.runner as runner

    enabled(case)
    task = claim_task(case)
    calls = {"search": 0, "read": 0}

    def search(*args, **kwargs):
        calls["search"] += 1
        return {"results": [{"url": "https://publisher.example/book", "title": "食事文化の研究",
                 "snippet": "食事文化を扱う書籍", "engines": ["bing"]}],
                "unresponsive_engines": [["other", "Access denied"]]}

    def read(*args, **kwargs):
        calls["read"] += 1
        return {"status": "retrieved", "text": "食事文化を扱う研究書。目次と紹介文。",
                "metadata": {"record_type": "book", "isbn": "9781234567897",
                             "title": "食事文化の研究", "authors": ["研究者"]}}

    monkeypatch.setattr(runner, "rustxng_search", search)
    monkeypatch.setattr(runner, "read_web_source", read)
    context = {"use_online_research": True, "research_search_plan": [{
        "obligation_id": "O", "query": "食事 文化", "variants": ["食事 文化"]}]}
    first = case.worker._authoring_supplement_research(task, context)
    second = case.worker._authoring_supplement_research(task, context)
    assert calls == {"search": 1, "read": 1}
    record = second["research_discoveries"][0]["candidates"][0]
    assert first["research_discoveries"] == second["research_discoveries"]
    assert record["source_type"] == "online_book"
    assert record["provenance"]["source_level"] == "bibliographic_metadata"
    assert record["discovery_quality"]["passed"]
    assert first["supplemental_web_report"][0]["status"] == "partial"
    assert first["supplemental_web_report"][0]["unresponsive_engines"]


def test_unavailable_proxy_is_recorded_and_does_not_cancel_writing(case, monkeypatch):
    import fukamiru.worker.runner as runner

    enabled(case)
    task = claim_task(case)
    def unavailable(*args, **kwargs):
        raise OSError("connection refused")
    monkeypatch.setattr(runner, "rustxng_search", unavailable)
    result = case.worker._authoring_supplement_research(task, {
        "use_online_research": True, "research_search_plan": [{
            "query": "食事 文化", "variants": ["食事 文化"]}]})
    assert result["supplemental_web_report"][0]["status"] == "unavailable"
    assert result["research_discoveries"][0]["candidates"] == []


def test_book_reference_type_and_identifiers_survive_machine_suggestion():
    record = source_record()
    record.update(policy_id="reference-usage-v22", disposition="machine_detected_related",
                  usage_id="U", source_type="online_book")
    record["provenance"].update(record_type="book", isbn="9781234567897", publisher="学術社")
    rendered = JapaneseReferenceService.reference_from_usage_v22(record)
    assert rendered["record_type"] == "book"
    assert rendered["isbn"] == "9781234567897"
    assert rendered["publisher"] == "学術社"
    assert rendered["consulted_status"] == "not_consulted"


def test_unread_book_reaches_machine_suggestions_without_becoming_evidence(case, monkeypatch):
    task = claim_task(case)
    monkeypatch.setattr(case.worker, "_readonly_manuscript_audit",
                        lambda *a, **k: pytest.fail("metadata is not book body"))
    record = source_record()
    record.update(source_type="online_book", confidence=1,
                  discovery_quality={"passed": True})
    record["provenance"].update(source_level="bibliographic_metadata", record_type="book")
    result = case.worker._manuscript_validate_evidence_support(task, {
        "research_search_plan": [{"obligation_id": "O", "query": record["query"],
            "subclaims": ["食事文化を調査した。"], "policy": "authoring-atomic-evidence-v1"}],
        "research_discoveries": [{"query": record["query"], "candidates": [record]}],
    }).context
    assert result["evidence_context"] == []
    assert result["reference_usage_records"][0]["disposition"] == "machine_detected_related"
    assert result["reference_usage_records"][0]["passed_to_draft"] is False


def test_keyword_overlap_does_not_survive_content_review_without_a_relevant_passage(
    case, monkeypatch,
):
    task = claim_task(case)
    record = source_record()
    record.update(confidence=1, discovery_quality={"passed": True})
    record["provenance"].update(source_level="full_text")
    def audit(_task, phase, units, sources):
        return {"verdicts": [{"unit_id": u["unit_id"], "status": "insufficient",
            "rationale": "単語は一致するが異なる対象の資料である。", "citations": []}
            for u in units]}
    monkeypatch.setattr(case.worker, "_readonly_manuscript_audit", audit)
    result = case.worker._manuscript_validate_evidence_support(task, {
        "research_search_plan": [{"obligation_id": "O", "query": record["query"],
            "subclaims": ["食事文化を調査した。"], "policy": "authoring-atomic-evidence-v1"}],
        "research_discoveries": [{"query": record["query"], "candidates": [record]}],
    }).context
    assert result["evidence_context"] == []
    assert result["reference_usage_records"][0]["discovery_quality_passed"] is False
    assert case.worker._validated_machine_reference_sources_v22(result, []) == []


@pytest.mark.parametrize("status,useful", [("supports_subclaim", True),
    ("inference_bridge", True), ("insufficient", False), ("contradicts", True)])
def test_only_grounded_useful_ocr_read_is_enqueued_without_waiting(
    case, monkeypatch, status, useful,
):
    task = claim_task(case)
    record = source_record()
    record.update(confidence=1, discovery_quality={"passed": True})
    record["provenance"].update(source_level="full_text", content_acquisition={
        "status": "retrieved", "ocr_job_id": "job", "content_sha256": "digest"})
    def audit(_task, phase, units, sources):
        return {"verdicts": [{"unit_id": u["unit_id"], "status": status,
            "rationale": "この命題と対応する本文を確認した。",
            "citations": [{"candidate_key": "openalex:work", "quote": record["excerpt"]}]}
            for u in units]}
    monkeypatch.setattr(case.worker, "_readonly_manuscript_audit", audit)
    monkeypatch.setattr(case.worker, "_save_manuscript_evidence",
                        lambda _task, _context, item: item.setdefault("id", "evidence:work"))
    queued = []
    monkeypatch.setattr(ResearchSourceIngestion, "enqueue_useful",
                        lambda self, run_id, item: queued.append(item) or "child")
    result = case.worker._manuscript_validate_evidence_support(task, {
        "research_search_plan": [{"obligation_id": "O", "query": record["query"],
            "subclaims": ["食事文化を調査した。"], "policy": "authoring-atomic-evidence-v1"}],
        "research_discoveries": [{"query": record["query"], "candidates": [record]}],
    }).context
    assert bool(queued) is useful
    assert bool(result["research_source_ingestion_runs"]) is useful
    if status == "contradicts":
        assert result["evidence_context"] == []
        assert queued[0]["provenance"]["research_value"] == "grounded_counterevidence"


def test_exact_url_or_bibliography_reuses_local_passages():
    local = source_record()
    local.update(source_type="local_rag")
    local["provenance"] = {"citation": {"title": local["title"],
        "authors": local["authors"], "year": "2024", "edition": "2"}}
    web = source_record()
    web["source_url"] += "?utm_source=search#section"
    web["provenance"] = {}
    assert reuse_local_passages([local, web]) == [local]
    same_edition = {**web, "source_url": "https://other.example/catalog",
                    "provenance": {"citation": local["provenance"]["citation"]}}
    assert reuse_local_passages([local, same_edition]) == [local]
    other_edition = {**same_edition, "provenance": {"citation": {
        **local["provenance"]["citation"], "edition": "3"}}}
    assert len(reuse_local_passages([local, other_edition])) == 2


def test_encrypted_download_reuses_actual_raster_pipeline_then_continues_rag(case, monkeypatch):
    import fukamiru.literature.content as content
    from fukamiru.ocr.pdf_recovery import file_sha256

    task = claim_task(case)
    writer = PdfWriter()
    writer.add_blank_page(width=120, height=120)
    writer._root_object[NameObject("/Lang")] = TextStringObject("ja-JP")
    writer.encrypt(user_password="", owner_password="displayable-owner")
    stream = io.BytesIO()
    writer.write(stream)
    pdf = stream.getvalue()
    monkeypatch.setattr(content, "public_https", lambda url: True)
    monkeypatch.setattr(content.urllib.request, "build_opener", lambda *a: SimpleNamespace(
        open=lambda *a, **k: io.BytesIO(pdf)))
    # Real rendering and job persistence; only the external OCR inference is replaced.
    calls = []
    class Engine:
        def process(self, source, language):
            calls.append(source)
            reader = PdfReader(source)
            assert not reader.is_encrypted
            assert reader.pages[0].images
            return OCRResult(source, file_sha256(source), language, "ndlocr_lite", (
                OCRPage(1, "食事文化を調査した。本文の根拠として使える記述である。"),))
    monkeypatch.setattr(PDFOCRService, "_engine", lambda self, name: Engine())
    candidate = LiteratureCandidate("openalex", "W1", "食事の研究", ("研究者",), "文化研究",
        None, "2024", "article", "10.1234/food", "https://example.org/study",
        "https://example.org/study.pdf", "green", None, {})
    service = ResearchSourceIngestion(case.databases, case.settings)
    result = read_oa_pdf(candidate, ["食事 文化"], timeout=2, max_bytes=100_000,
        download_dir=case.root / "downloads", recover=lambda path, kind:
        service.read(task.run_id, path, kind))
    assert result["status"] == "retrieved"
    assert result["ocr_engine"] == "ndlocr_lite"
    assert result["locator"] == "p. 1"
    assert len(calls) == 1
    assert service.read(task.run_id, Path(result["source_path"]), "encrypted_pdf")["pages"]
    assert len(calls) == 1
    record = source_record()
    record["provenance"]["content_acquisition"] = {
        k: v for k, v in result.items() if k not in {"excerpt", "pages"}}
    child = service.enqueue_useful(task.run_id, record)
    assert child == service.enqueue_useful(task.run_id, record)
    workflow = research_source_ingestion_workflow()
    case.worker.workflows[workflow.name] = workflow
    case.worker.workflow_versions[(workflow.name, workflow.version)] = workflow
    with case.databases.state.connect() as connection:
        assert connection.execute(
            "SELECT count(*) FROM research_source_handoffs").fetchone()[0] == 1
        assert connection.execute(
            "SELECT status FROM runs WHERE id=?", (task.run_id,)).fetchone()[0] == "running"
    for _ in range(6):
        assert case.worker.run_once(lane="ingestion_cpu")
    assert case.worker.run_once(lane="embedding_gpu")
    with case.databases.state.connect() as connection:
        snapshot = RunRepository(connection).snapshot(child)
        assert snapshot["run"]["status"] == "completed", snapshot
    with case.databases.knowledge.connect() as connection:
        known = known_work_read(connection, {"doi": "10.1234/food"})
        assert known["spans"][0]["text"].startswith("食事文化")
    assert len(calls) == 1


def test_password_required_pdf_is_a_recorded_read_refusal(case, monkeypatch):
    import fukamiru.literature.content as content

    task = claim_task(case)
    writer = PdfWriter()
    writer.add_blank_page(width=120, height=120)
    writer.encrypt(user_password="display-password", owner_password="owner")
    buffer = io.BytesIO()
    writer.write(buffer)
    monkeypatch.setattr(content, "public_https", lambda url: True)
    monkeypatch.setattr(content.urllib.request, "build_opener", lambda *a: SimpleNamespace(
        open=lambda *a, **k: io.BytesIO(buffer.getvalue())))
    candidate = LiteratureCandidate("openalex", "W1", "locked", (), None, None, None,
        "article", None, None, "https://example.org/locked.pdf", "green", None, {})
    result = read_oa_pdf(candidate, [], timeout=2, max_bytes=100_000,
        download_dir=case.root / "downloads", recover=lambda path, kind:
        ResearchSourceIngestion(case.databases, case.settings).read(task.run_id, path, kind))
    assert result["status"] == "unavailable"
    with case.databases.state.connect() as connection:
        assert connection.execute(
            "SELECT count(*) FROM research_source_handoffs").fetchone()[0] == 0


def test_index_guard_pauses_only_the_independent_source_run(case, monkeypatch):
    from fukamiru.knowledge.scalable_index import IndexBuildGuardError

    task = claim_task(case)
    case.worker.settings = replace(case.settings, retrieval=replace(
        case.settings.retrieval, semantic_enabled=True))
    def guard(*args, **kwargs):
        raise IndexBuildGuardError("new index needs review")
    monkeypatch.setattr(case.worker.semantic_index, "build", guard)
    result = case.worker._research_publish_index(task, {"research_parent_run_id": task.run_id})
    assert result.waiting_human is True
    assert result.context["semantic_index"]["status"] == "needs_review"
