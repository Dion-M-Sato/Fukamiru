from __future__ import annotations

import json
import sqlite3
from copy import deepcopy
from pathlib import Path
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from fukamiru.api.app import create_app
from fukamiru.authoring.codex import (
    CodexAuthoringAdapter,
    _validate_codex_output_schema,
    describe_authoring_request,
)
from fukamiru.config import load_settings
from fukamiru.documents.canonical_html import CanonicalHTMLValidator
from fukamiru.errors import FukamiruError
from fukamiru.knowledge.report_bibliography import (
    FIELDS,
    BibliographyUnresolved,
    extract_bibliography,
)
from fukamiru.knowledge.report_contracts import SCHEMAS
from fukamiru.knowledge.report_research import research_report
from fukamiru.knowledge.report_sources import ReportCorpus, normalized, page_kind, quote_key
from fukamiru.knowledge.search_reports import (
    MAX_CONTEXT_CHARACTERS,
    MAX_EXCERPT_CHARACTERS,
    STAGE,
    VERIFY_STAGE,
    create_report,
    prepare_evidence,
)
from fukamiru.storage.database import ClosingSQLiteConnection

QUOTE = "観測ノートは参加者の観測記録を掲載した。<script>bad()</script>"
BODY = QUOTE + "観測記録欄は参加者の意見を紹介し、参加者相互の交流を促した。" * 9
TITLE_PAGE = "観測ノートと参加者\n架空太郎\n東京\n架空研究出版\n2011"


def results() -> list[dict]:
    return [
        {
            "chunk_id": "chunk-a",
            "source_asset_id": "source-a",
            "title": "観測ノートと参加者",
            "kind": "body",
            "path": "C:/sources/<book>.txt",
            "text": BODY,
            "source_span_ids": ["span-a"],
            "retrieval": "hybrid",
            "citation": {"start_page": 12, "end_page": 13},
        }
    ]


def bibliography() -> dict:
    return {
        "source-a": {
            "authors": ["架空太郎"],
            "title": "観測ノートと参加者",
            "publisher": "架空研究出版",
            "publication_place": "東京",
            "publication_date": "2011",
            "source_asset_id": "source-a",
            "proofs": {},
            "verification_status": "pending_audit",
        }
    }


def answer() -> dict:
    paragraph = {
        "text": "観測記録を通じた参加者の参加が見られる。",
        "kind": "content",
        "evidence_ids": ["E1"],
        "supports": [{"evidence_id": "E1", "quote": QUOTE}],
    }
    return {
        "summary": [paragraph],
        "findings": [{**deepcopy(paragraph), "heading": "参加者の参加"}],
        "limitations": ["掲載時期はこの抜粋では確認できない。"],
    }


def verification(payload: dict, *, valid: bool = True) -> dict:
    return {
        "supported": valid,
        "coherent": True,
        "checked_paragraph_ids": [p["paragraph_id"] for p in payload["paragraphs"]],
        "checked_bibliography_ids": [b["reference_id"] for b in payload["bibliographies"]],
        "issues": [] if valid else [{"target": "summary-1", "reason": "原文が支持していない。"}],
    }


def generator(value: dict | None = None, *, valid: bool = True) -> Mock:
    def generate(stage, payload, **_options):
        if stage == VERIFY_STAGE:
            return verification(payload, valid=valid)
        return deepcopy(value or answer())

    return Mock(side_effect=generate)


def test_report_retains_exact_used_evidence_bibliography_and_independent_check() -> None:
    generate = generator()
    original = results()
    unused = {
        **original[0],
        "chunk_id": "unused",
        "source_asset_id": "source-b",
        "text": "この資料には別の主張があり、今回の報告では参照していない。",
    }
    references = {
        **bibliography(),
        "source-b": {
            **bibliography()["source-a"],
            "title": "Unused work",
            "source_asset_id": "source-b",
        },
    }
    untouched = deepcopy(original)
    report = create_report("参加者の参加", [*original, unused], generate, bibliographies=references)
    assert original == untouched
    stage, payload = generate.call_args_list[0].args
    request = describe_authoring_request(stage, payload)
    assert request.stage == STAGE and request.model == "gpt-5.6-sol"
    assert request.payload["evidence"][0]["text"] == BODY
    assert report["evidence"][0]["source_span_ids"] == ["span-a"]
    assert report["evidence"][0]["used_quotes"] == [QUOTE]
    assert report["source_count"] == report["result_count"] == 1
    assert len(report["bibliography"]) == 1
    assert report["bibliography"][0]["verification_status"] == "source_verified"
    assert "Unused work" not in report["canonical_html"]
    assert "この資料には別の主張" not in report["canonical_html"]
    assert '<a href="#evidence-E1">' in report["canonical_html"]
    assert '<a href="#bibliography-R1">' in report["canonical_html"]
    assert "p. 12–13" in report["canonical_html"]
    assert "<script>" not in report["canonical_html"]
    assert "&lt;script&gt;" in report["canonical_html"]
    assert report["verification"]["supported"]
    assert CanonicalHTMLValidator().validate(report["canonical_html"]).valid
    assert [call.args[0] for call in generate.call_args_list] == [STAGE, VERIFY_STAGE]


@pytest.mark.parametrize("ids", [[], ["invented"], ["E1", "E999"]])
def test_uncited_or_invented_references_are_rejected(ids: list[str]) -> None:
    value = answer()
    value["summary"][0]["evidence_ids"] = ids
    with pytest.raises(FukamiruError, match="根拠参照"):
        create_report("query", results(), generator(value), bibliographies=bibliography())


def test_fabricated_quotes_and_title_only_content_are_rejected() -> None:
    value = answer()
    value["summary"][0]["supports"][0]["quote"] = "資料に書かれていない原文を引用した。"
    with pytest.raises(FukamiruError, match="原文"):
        create_report("query", results(), generator(value), bibliographies=bibliography())
    with pytest.raises(FukamiruError, match="本文の根拠"):
        create_report(
            "query",
            [{**results()[0], "kind": "identity"}],
            generator(),
            bibliographies=bibliography(),
        )


def test_semantic_rejection_retries_once_then_stops_without_publishing() -> None:
    generate = generator(valid=False)
    with pytest.raises(FukamiruError, match="再検査"):
        create_report("query", results(), generate, bibliographies=bibliography())
    assert [call.args[0] for call in generate.call_args_list] == [STAGE, VERIFY_STAGE] * 2
    assert generate.call_args_list[2].args[1]["verification_feedback"]


def test_invalid_quote_gets_one_explicit_repair_before_independent_check() -> None:
    bad = answer()
    bad["summary"][0]["supports"][0]["quote"] = "原文には存在しない誤った転記である。"
    drafts = iter([bad, answer()])

    def generate(stage, payload):
        return verification(payload) if stage == VERIFY_STAGE else next(drafts)

    model = Mock(side_effect=generate)
    report = create_report("query", results(), model, bibliographies=bibliography())
    assert report["verification"]["supported"]
    assert [call.args[0] for call in model.call_args_list] == [STAGE, STAGE, VERIFY_STAGE]
    feedback = model.call_args_list[1].args[1]["verification_feedback"]
    assert "原文" in feedback[0]["reason"]
    assert model.call_args_list[1].args[1]["previous_draft"] == bad
    failed = Mock(side_effect=FukamiruError("provider failed"))
    with pytest.raises(FukamiruError, match="provider failed"):
        create_report("query", results(), failed, bibliographies=bibliography())
    assert failed.call_count == 1


def test_verifier_cannot_omit_used_bibliography() -> None:
    def generate(stage, payload):
        if stage == VERIFY_STAGE:
            return {**verification(payload), "checked_bibliography_ids": []}
        return answer()

    with pytest.raises(FukamiruError, match="再検査対象"):
        create_report("query", results(), generate, bibliographies=bibliography())


def test_validation_feedback_identifies_the_paragraph_and_missing_support() -> None:
    value = answer()
    value["summary"][0]["evidence_ids"].append("E2")
    other = {**results()[0], "text": "第二の資料には、この論点の別の事情が記録されている。"}
    generate = generator(value)
    with pytest.raises(FukamiruError, match="summary-1.*不足: E2"):
        create_report("query", [*results(), other], generate, bibliographies=bibliography())
    assert "summary-1" in generate.call_args_list[1].args[1]["verification_feedback"][0]["reason"]


def test_prereading_scope_is_kept_for_audit_not_rendered_as_completed_report() -> None:
    scope = "本文未読のため同定は後で確認する。"
    report = create_report(
        "query", results(), generator(), bibliographies=bibliography(), scope=scope
    )
    assert report["scope"] == scope
    assert scope not in report["canonical_html"]
    assert CanonicalHTMLValidator().validate(report["canonical_html"]).valid


def test_empty_or_unverified_sources_do_not_call_the_model() -> None:
    generate = Mock()
    for values in ([], results()):
        report = create_report("該当なし", values, generate)
        assert report["model"] is None and not report["bibliography"]
        assert report["source_count"] == report["result_count"] == 0
        assert CanonicalHTMLValidator().validate(report["canonical_html"]).valid
    generate.assert_not_called()


def test_budget_and_cross_file_duplicates_preserve_original_evidence() -> None:
    long = [
        {**results()[0], "chunk_id": f"c-{index}", "text": str(index) + "あ" * 20000}
        for index in range(30)
    ]
    evidence = prepare_evidence([long[0], *long])
    assert sum(len(item["text"]) for item in evidence) == MAX_CONTEXT_CHARACTERS
    assert max(len(item["text"]) for item in evidence) == MAX_EXCERPT_CHARACTERS
    assert len({item["chunk_id"] for item in evidence}) == len(evidence)
    assert all(item["excerpt_truncated"] for item in evidence)
    assert len(prepare_evidence([results()[0], {**results()[0], "chunk_id": "other-file"}])) == 1


def test_all_new_stages_have_valid_strict_final_transport_schemas() -> None:
    for stage in SCHEMAS:
        request = describe_authoring_request(stage, {"query": "test"})
        _validate_codex_output_schema(request.schema)
        assert request.schema == CodexAuthoringAdapter.execution_contract(stage)["schema"]


def seed_source(path: Path) -> None:
    with sqlite3.connect(path) as conn:
        conn.execute(
            "INSERT INTO source_assets(id,sha256,byte_size,media_type,ingestion_status,"
            "created_at,updated_at) VALUES ('source-a',?,10,'text/plain','ready','now','now')",
            ("a" * 64,),
        )
        conn.execute(
            "INSERT INTO source_locations(source_asset_id,path,is_primary,is_available,"
            "first_seen_at) VALUES ('source-a','C:/fixture/book.txt',1,1,'now')"
        )
        conn.execute(
            "INSERT INTO document_units(id,source_asset_id,unit_type,title,material_role,"
            "permission,created_at,updated_at) VALUES ('unit-a','source-a','book',"
            "'観測ノートと参加者','secondary','quote','now','now')"
        )
        for page_id, number, text in [("title-page", 1, TITLE_PAGE), ("body-page", 12, BODY)]:
            conn.execute(
                "INSERT INTO pages(id,document_unit_id,page_number,text,created_at) "
                "VALUES (?,'unit-a',?,?,'now')",
                (page_id, number, text),
            )
        conn.execute(
            "INSERT INTO bibliographic_records(id,record_type,title,authors_json,publisher,"
            "publication_date,created_at,updated_at) VALUES ('record-a','book',"
            "'観測ノートと参加者',?,'架空研究出版','2011','now','now')",
            (json.dumps(["架空太郎"]),),
        )
        conn.execute(
            "INSERT INTO source_asset_bibliography(source_asset_id,bibliographic_record_id,"
            "metadata_source,metadata_status,source_fields_json,created_at) "
            "VALUES ('source-a','record-a','html_meta','complete','{}','now')"
        )


def research_generator(stage, payload, **_options):
    if stage == "knowledge_search_plan":
        candidate = payload["candidates"][0]
        return {
            "query_kind": "topic",
            "scope": "参加者の参加を調べる。",
            "ambiguities": [],
            "selections": [
                {
                    "candidate_id": candidate["candidate_id"],
                    "entity": "架空太郎",
                    "relation": "author",
                    "reason": "本文が関連する。",
                    "reading_queries": ["参加者 観測記録"],
                }
            ],
            "exclusions": [],
        }
    if stage == "knowledge_search_bibliography":

        def proof(value):
            return {
                "value": value,
                "page_id": "title-page" if value else "",
                "quote": TITLE_PAGE if value else "",
                "method": "text",
            }

        return {
            **{name: proof("") for name in FIELDS},
            "authors": [proof("架空太郎")],
            "title": proof("観測ノートと参加者"),
            "publisher": proof("架空研究出版"),
            "publication_date": proof("2011"),
            "publication_place": proof("東京"),
            "record_type": "book",
            "unresolved": [],
        }
    if stage == VERIFY_STAGE:
        return verification(payload)
    return answer()


@pytest.fixture
def client(tmp_path: Path):
    config = tmp_path / "test.toml"
    config.write_text(
        f'''[paths]
state_db = "{tmp_path.as_posix()}/state.db"
knowledge_db = "{tmp_path.as_posix()}/knowledge.db"
runtime_root = "{tmp_path.as_posix()}/runtime"
corpus_root = "{tmp_path.as_posix()}/corpus"
ocr_root = "{tmp_path.as_posix()}/ocr"
knowledge_artifacts_root = "{tmp_path.as_posix()}/kb"
faiss_root = "{tmp_path.as_posix()}/faiss"
[retrieval]
require_cuda = false
embedding_device = "cpu"
tokenizer_mode = "unicode"
semantic_enabled = false
''',
        encoding="utf-8",
    )
    with TestClient(create_app(load_settings(config), start_worker=False)) as session:
        seed_source(tmp_path / "knowledge.db")
        session.report_test_db = tmp_path / "knowledge.db"
        yield session


def test_api_reads_pages_verifies_saves_and_reopens_without_regenerating(
    client, monkeypatch
) -> None:
    search = Mock(return_value=results())
    generate = Mock(side_effect=research_generator)
    monkeypatch.setattr("fukamiru.api.app.KnowledgeSearchService.search", search)
    monkeypatch.setattr("fukamiru.api.app.CodexAuthoringAdapter.generate", generate)
    created = client.post("/api/search-reports", json={"query": "  参加者の参加  "})
    assert created.status_code == 201, created.text
    report = created.json()
    assert report["query"] == "参加者の参加" and report["source_count"] == 1
    assert report["bibliography"][0]["corpus_record_id"] == "record-a"
    search.assert_called_once_with("参加者の参加", limit=100, metadata_filters=None)
    listing = client.get("/api/search-reports").json()
    assert listing[0]["id"] == report["id"] and "canonical_html" not in listing[0]
    url = f"/api/search-reports/{report['id']}"
    assert client.get(url).json() == report
    exported = client.get(url + "/export.html")
    assert exported.status_code == 200 and report["canonical_html"] in exported.text
    assert "attachment;" in exported.headers["content-disposition"]
    assert generate.call_count == 4 and search.call_count == 1
    assert client.get("/api/search-reports/missing").status_code == 404
    assert client.post("/api/search-reports", json={"query": "  "}).status_code == 422
    assert client.get("/api/search-reports?limit=0").status_code == 422


def test_api_provider_failure_saves_nothing_and_empty_search_is_honest(client, monkeypatch) -> None:
    monkeypatch.setattr(
        "fukamiru.api.app.KnowledgeSearchService.search", Mock(return_value=results())
    )
    generate = Mock(side_effect=FukamiruError("provider unavailable"))
    monkeypatch.setattr("fukamiru.api.app.CodexAuthoringAdapter.generate", generate)
    assert client.post("/api/search-reports", json={"query": "query"}).status_code == 502
    assert client.get("/api/search-reports").json() == []
    monkeypatch.setattr("fukamiru.api.app.KnowledgeSearchService.search", Mock(return_value=[]))
    response = client.post("/api/search-reports", json={"query": "no hits"})
    assert response.status_code == 201 and response.json()["result_count"] == 0
    assert generate.call_count == 1


def readonly_corpus(client) -> ReportCorpus:
    def connect():
        connection = sqlite3.connect(
            client.report_test_db.as_uri() + "?mode=ro", uri=True, factory=ClosingSQLiteConnection
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        return connection

    return ReportCorpus(connect)


def test_metadata_only_discovery_and_reading_are_readonly_and_preserve_body(client) -> None:
    corpus = readonly_corpus(client)
    candidates = corpus.candidates("架空太郎", [])
    assert len(candidates) == 1 and candidates[0]["metadata_match"]
    assert candidates[0]["literal_match"]
    source = corpus.read(candidates[0])
    assert source["pages"][0]["text"] == TITLE_PAGE
    body = corpus.body(source, ["参加者 観測記録"])
    assert len(body) == 1 and body[0]["text"] == BODY
    assert body[0]["pages"][0]["number"] == 12
    bibliography, images = extract_bibliography(source, corpus, research_generator)
    assert bibliography["publisher"] == "架空研究出版" and not images
    assert bibliography["proofs"]["publication_date"]["page_id"] == "title-page"


def test_name_queries_do_not_promote_unmatched_semantic_candidates(client) -> None:
    def select_unmatched(stage, payload, **options):
        assert stage == "knowledge_search_plan"
        return {**research_generator(stage, payload, **options), "query_kind": "name"}

    generate = Mock(side_effect=select_unmatched)
    report = research_report("Beauregard", results(), readonly_corpus(client), generate)
    assert generate.call_count == 1
    assert not report["evidence"] and not report["bibliography"]
    assert report["selection_audit"][0]["outcome"] == "excluded"


def test_image_transcriptions_support_identity_without_replacing_ocr_or_stale_doubts(
    client, tmp_path: Path
) -> None:
    corpus = readonly_corpus(client)
    image = tmp_path / "title.png"
    image.write_bytes(b"synthetic image bytes; provider is mocked")
    corpus.images = Mock(return_value={"title-page": image})
    image_quote = "観測ノートと参加者：原本表題紙で確認した完全な題名"
    doubt = "表題紙を読む前なので完全な題名は未確認。"
    observed = {}

    def generate(stage, payload, **options):
        value = research_generator(stage, payload, **options)
        if stage == "knowledge_search_plan":
            value["ambiguities"] = [doubt]
        elif stage == "knowledge_search_bibliography":
            value["title"].update(value=image_quote, quote=image_quote, method="image")
        elif stage == STAGE:
            observed.update(payload)
            value["summary"][0]["evidence_ids"].append("E2")
            value["summary"][0]["supports"].append(
                {"evidence_id": "E2", "quote": image_quote}
            )
        elif stage == VERIFY_STAGE:
            assert options["image_paths"] == [image]
        return value

    report = research_report("参加者の参加", results(), corpus, generate)
    identity = next(item for item in report["evidence"] if item["kind"] == "identity")
    assert identity["pages"][0]["text"] == TITLE_PAGE
    assert image_quote not in TITLE_PAGE and image_quote in identity["text"]
    assert identity["bibliographic_transcriptions"][0]["quote"] == image_quote
    assert identity["bibliographic_transcriptions"][0]["image_sha256"]
    assert report["bibliography"][0]["title"] == image_quote
    assert observed["candidate_ambiguities"] == [doubt]
    assert report["candidate_ambiguities"] == [doubt]
    assert doubt not in report["limitations"] and doubt not in report["canonical_html"]


def test_title_dedication_and_index_are_not_substantive_body() -> None:
    for text in [
        "Abelard and the Early Middle Ages",
        "A MES FRERES\nLE Dr G. BEAUREGARD,\nAncien externe des hôpitaux de Paris.",
        "BAYARD: p. 39, 42.\nBEAUCLAIR: p. 295.\nBEAUJEU: p. 359.\nBEAUMARCHAIS: p. 172.",
    ]:
        assert page_kind({"text": text}) != "body"


def test_catalogue_cover_and_headerless_contents_cannot_displace_the_introduction() -> None:
    cover = "A long catalogue title. " * 12 + "Source gallica.bnf.fr / Bibliothèque nationale"
    contents = "CHAPITRE I. Sujet\n" + "Rubrique et sous-partie .... 25\n" * 9
    introduction = "INTRODUCTION. " + "Un argument suivi examine les causes et les effets. " * 10
    assert page_kind({"text": cover}) == "identity"
    assert page_kind({"text": contents}) == "navigation"
    assert page_kind({"text": "ERRATA\nPages Lignes\n4 2 correction"}) == "navigation"
    assert page_kind({"text": "CHAPITRE I.\n" + introduction}) == "body"
    pages = [
        {"page_id": str(i), "document_unit_id": "unit", "number": i + 1,
         "text": text, "kind": page_kind({"text": text})}
        for i, text in enumerate([cover, introduction, contents])
    ]
    body = ReportCorpus(lambda: None).body(
        {"source_asset_id": "book", "title": "Book", "path": "book.pdf",
         "sha256": "a" * 64, "pages": pages}, ["sujet"]
    )
    assert len(body) == 1 and body[0]["text"] == introduction


def test_image_initial_cannot_expand_an_author_without_its_full_name_quote(
    client, tmp_path: Path
) -> None:
    with sqlite3.connect(client.report_test_db) as connection:
        connection.execute("UPDATE pages SET text=text||'\nBeauregard, Paul' WHERE id='title-page'")
    corpus = readonly_corpus(client)
    image = tmp_path / "title.png"
    image.write_bytes(b"synthetic page image; model is mocked")
    corpus.images = Mock(return_value={"title-page": image})
    source = corpus.read(corpus.candidates("架空太郎", [])[0])
    calls = []

    def generate(stage, payload, **options):
        calls.append(deepcopy(payload))
        value = research_generator(stage, payload, **options)
        value["authors"] = [{"value": "Paul Beauregard", "page_id": "title-page",
            "quote": "P. BEAUREGARD", "method": "image"}]
        if len(calls) == 2:
            value["authors"][0].update(quote="Beauregard, Paul", method="text")
        return value

    bibliography, _images = extract_bibliography(source, corpus, generate)
    assert bibliography["authors"] == ["Paul Beauregard"]
    assert bibliography["proofs"]["authors-1"]["quote"] == "Beauregard, Paul"
    assert len(calls) == 2 and "Paul Beauregard" in calls[1]["validation_feedback"]
    assert calls[1]["previous_extraction"]["authors"][0]["quote"] == "P. BEAUREGARD"


def test_text_proofs_cannot_invent_publisher_or_use_unattached_images(client) -> None:
    corpus = readonly_corpus(client)
    source = corpus.read(corpus.candidates("架空太郎", [])[0])
    for altered in [{"value": "架空出版社"}, {"method": "image"}]:

        def generate(stage, payload, altered=altered, **options):
            value = research_generator(stage, payload, **options)
            value["publisher"].update(altered)
            return value

        with pytest.raises(BibliographyUnresolved):
            extract_bibliography(source, corpus, generate)


def test_unknown_plan_ids_and_bibliography_provider_failures_stop(client) -> None:
    corpus = readonly_corpus(client)

    def unknown(stage, payload, **options):
        value = research_generator(stage, payload, **options)
        value["selections"][0]["candidate_id"] = "unknown"
        return value

    with pytest.raises(FukamiruError, match="読取計画"):
        research_report("参加者の参加", results(), corpus, unknown)

    def provider_failure(stage, payload, **options):
        if stage == "knowledge_search_bibliography":
            raise FukamiruError("provider failed")
        return research_generator(stage, payload, **options)

    with pytest.raises(FukamiruError, match="provider failed"):
        research_report("参加者の参加", results(), corpus, provider_failure)


def test_name_normalization_does_not_change_japanese_voicing_or_quotation_letters() -> None:
    assert normalized("BÉAUREGARD") == normalized("Beauregard")
    assert normalized("カク") != normalized("ガク")
    assert quote_key("A B\nC") == quote_key("ABC")
    assert quote_key("économie") != quote_key("economie")


def test_long_chapters_keep_a_query_window_and_explicit_character_offsets() -> None:
    corpus = ReportCorpus(lambda: None)
    pages = [
        {
            "page_id": "overview",
            "document_unit_id": "unit",
            "number": 1,
            "kind": "body",
            "text": "Introduction. " + "Overview material. " * 700,
        },
        {
            "page_id": "answer",
            "document_unit_id": "unit",
            "number": 2,
            "kind": "body",
            "text": "Context. " * 1000
            + "Unique research target. "
            + "Following discussion. " * 700,
        },
    ]
    source = {
        "source_asset_id": "book",
        "title": "Book",
        "path": "book.epub",
        "sha256": "a" * 64,
        "pages": pages,
    }
    evidence = corpus.body(source, ["Unique research target"])
    assert any("Unique research target" in item["text"] for item in evidence)
    assert sum(len(item["text"]) for item in evidence) <= 10000
    assert all(item["context_truncated"] for item in evidence)
    for item in evidence:
        for page in item["pages"]:
            original = next(p for p in pages if p["page_id"] == page["page_id"])
            assert page["text"] == original["text"][page["start_char"] : page["end_char"]]
