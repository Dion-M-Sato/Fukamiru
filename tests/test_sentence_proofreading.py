from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from fukamiru.revisions.sentence_proofreading import (
    digest,
    extract_sentences,
    preprocess_candidate,
    raw_changed_spans,
)
from fukamiru.revisions.surface_warning import (
    LunaBatchError,
    LunaConfigurationError,
    call_once,
    validate_response,
)
from fukamiru.storage.database import SQLiteDatabase
from fukamiru.worker.sentence_proofreading import (
    _request_generation,
    generate_candidates,
    publish_shadow,
    rank_candidates,
    select_candidates,
)


def _add_generation_columns(database: SQLiteDatabase) -> None:
    # These focused fixtures create only the columns used by their operation.
    # Match migration 0055 after inserting their legacy example records.
    with database.connect() as connection:
        for table in ("manuscript_proofreading_sentences", "manuscript_proofreading_patches",
                      "manuscript_proofreading_title_registry"):
            if connection.execute("SELECT 1 FROM sqlite_master WHERE name=?", (table,)).fetchone():
                connection.execute(f"ALTER TABLE {table} ADD COLUMN generation_id "
                                   "TEXT NOT NULL DEFAULT 'legacy'")


def test_v29_generation_contract_still_uses_one_three_candidate_response() -> None:
    calls = []

    def complete_json(**kwargs):
        calls.append(kwargs)
        return {"candidates": ["候補一。", "候補二。", "候補三。"]}

    adapter = SimpleNamespace(complete_json=complete_json)
    assert _request_generation(adapter, "原文。", 42, 300) == [
        "候補一。", "候補二。", "候補三。"
    ]
    assert len(calls) == 1
    assert calls[0]["json_schema"]["properties"]["candidates"]["minItems"] == 3


def test_v31_generation_samples_three_times_and_keeps_distinct_candidates(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = SQLiteDatabase(tmp_path / "state.db", tmp_path)
    with db.connect() as connection:
        connection.executescript(
            "CREATE TABLE manuscript_proofreading_sentences ("
            "id TEXT,run_id TEXT,status TEXT,block_id TEXT,sentence_ordinal INTEGER,"
            "source_sentence_id TEXT,"
            "original_text TEXT,original_hash TEXT,error_text TEXT);"
            "CREATE TABLE manuscript_proofreading_patches ("
            "id TEXT,run_id TEXT,sentence_id TEXT,candidate_ordinal INTEGER,"
            "original_text TEXT,raw_candidate_text TEXT,normalized_candidate_text TEXT,"
            "original_hash TEXT,candidate_hash TEXT,generation_seed INTEGER,"
            "generation_model TEXT,generation_prompt_version TEXT,frequency INTEGER,"
            "generation_seeds_json TEXT,status TEXT,created_at TEXT,updated_at TEXT);"
        )
        connection.execute(
            "INSERT INTO manuscript_proofreading_sentences "
            "(id,run_id,status,source_sentence_id,original_text,original_hash) "
            "VALUES ('s1','run','extracted','stable','原文。','hash')"
        )
    calls = []

    def complete_json(**kwargs):
        calls.append(kwargs)
        if len(calls) == 2:
            raise RuntimeError("one sample failed")
        return {"candidate": "自然な文。" if len(calls) == 1 else "読みやすい文。"}

    monkeypatch.setattr("fukamiru.worker.sentence_proofreading._log", lambda *_a, **_k: None)
    monkeypatch.setattr("fukamiru.worker.sentence_proofreading._event", lambda *_a, **_k: None)
    _add_generation_columns(db)
    worker = SimpleNamespace(
        databases=SimpleNamespace(state=db),
        llm_jp=SimpleNamespace(model_identity="local-test", complete_json=complete_json),
        settings=SimpleNamespace(translation=SimpleNamespace(
            manuscript_polish_max_output_tokens=300
        )),
        _raise_if_run_cancelled=lambda *_a: None,
        execute_local_llm=lambda operation, **_kwargs: operation(),
    )
    claimed = SimpleNamespace(run_id="run", node_run_id="node")

    result = generate_candidates(worker, claimed, {
        "use_llm_jp": True, "research_problem": {"research_question": "問い"}
    })

    assert len(calls) == 3
    assert len({call["seed"] for call in calls}) == 3
    assert all(call["json_schema"]["required"] == ["candidate"] for call in calls)
    report = result["manuscript_proofreading_generation"]
    assert report.get("generation_error_sentence_count", 0) == 0
    assert report["generation_call_error_count"] == 1
    with db.connect() as connection:
        patches = connection.execute(
            "SELECT normalized_candidate_text,generation_seed "
            "FROM manuscript_proofreading_patches ORDER BY candidate_ordinal"
        ).fetchall()
    assert [row["normalized_candidate_text"] for row in patches] == [
        "自然な文。", "読みやすい文。"
    ]
    assert patches[0]["generation_seed"] == calls[0]["seed"]
    assert patches[1]["generation_seed"] == calls[2]["seed"]


def test_sentence_identity_is_stable_but_run_scoped() -> None:
    html = '<article id="doc"><p id="p1"> 前文。 後文。 </p></article>'
    first = extract_sentences(html, run_id="one", document_id="doc")
    second = extract_sentences(html, run_id="two", document_id="doc")
    assert [item["source_sentence_id"] for item in first] == [
        item["source_sentence_id"] for item in second
    ]
    assert {item["id"] for item in first}.isdisjoint(item["id"] for item in second)
    assert [(item["start_offset"], item["end_offset"]) for item in first] == [
        (1, 4),
        (5, 8),
    ]


def test_sentence_extraction_keeps_title_punctuation_inside_sentence() -> None:
    html = '<article id="doc"><p id="p1">『BLAME!』を扱う。「本当か？」と問う。</p></article>'
    sentences = extract_sentences(html, run_id="run", document_id="doc")
    assert [item["original_text"] for item in sentences] == [
        "『BLAME!』を扱う。",
        "「本当か？」と問う。",
    ]


def test_hard_protection_and_raw_diff() -> None:
    changed_number = preprocess_candidate("2020年に示した。", "2021年に示した。", {})
    assert changed_number["status"] == "hard_reject"
    assert "numeric_content_changed" in changed_number["status_reasons"]
    both = preprocess_candidate(
        "2020年に示した。",
        "2021年に述べた。",
        {},
        [{"type": "preserve_exact_terms", "terms": ["示した"]}],
    )
    assert both["status"] == "hard_reject"
    changed_title = preprocess_candidate(
        "『作品A』を論じる。",
        "『作品B』を論じる。",
        {"作品A": {"evidence_types": ["attributed_work"]}},
    )
    assert changed_title["status"] == "hard_reject"
    assert "protected_bibliographic_work_title_changed" in changed_title["status_reasons"]
    spans = raw_changed_spans("表現を調べる。", "語法を調べる。")
    assert spans and spans[0]["source_text"] == "表現"


@pytest.mark.parametrize(
    ("original", "candidate"),
    [
        ("変化は把握できない。", "変化は把握できません。"),
        ("変化は把握できません。", "変化は把握できない。"),
    ],
)
def test_register_shift_is_always_blocked(original: str, candidate: str) -> None:
    result = preprocess_candidate(original, candidate, {})

    assert result["status"] == "policy_blocked"
    assert "register_shift" in result["status_reasons"]


def test_luna_result_requires_exact_ids() -> None:
    row = {
        "id": "patch-1",
        "label": "clean",
        "confidence": "high",
        "suspicious_spans": [],
        "reason_codes": [],
    }
    assert validate_response({"results": [row]}, ["patch-1"]) == [row]
    with pytest.raises(LunaBatchError, match="missing_duplicate_or_unknown_result_id"):
        validate_response({"results": [row]}, ["patch-2"])


def test_luna_call_records_content_free_usage(tmp_path, monkeypatch) -> None:
    row = {
        "id": "patch-1",
        "label": "clean",
        "confidence": "high",
        "suspicious_spans": [],
        "reason_codes": [],
    }
    events = [
        {
            "type": "item.completed",
            "item": {"type": "agent_message", "text": json.dumps({"results": [row]})},
        },
        {
            "type": "turn.completed",
            "usage": {
                "input_tokens": 90,
                "input_tokens_details": {"cached_tokens": 70},
                "output_tokens": 10,
            },
        },
    ]
    monkeypatch.setattr(
        "fukamiru.revisions.surface_warning.shutil.which", lambda _: "codex"
    )
    monkeypatch.setattr(
        "fukamiru.revisions.surface_warning.subprocess.run",
        lambda *_, **__: SimpleNamespace(
            returncode=0,
            stdout="\n".join(json.dumps(event) for event in events),
            stderr="",
        ),
    )
    observations = []
    result, _latency = call_once(
        [
            {
                "id": "patch-1",
                "original": "原文。",
                "candidate": "候補。",
                "changed_spans": [],
            }
        ],
        isolated=tmp_path,
        observation_callback=observations.append,
    )

    assert result == [row]
    assert observations[0]["usage"]["cached_input_tokens"] == 70
    assert "原文" not in json.dumps(observations[0], ensure_ascii=False)


def test_luna_call_surfaces_structured_model_configuration_failure(
    tmp_path, monkeypatch
) -> None:
    events = [
        {
            "type": "turn.failed",
            "error": {
                "message": (
                    "The 'future-model' model is not supported when using Codex "
                    "with a ChatGPT account."
                )
            },
        }
    ]
    monkeypatch.setattr(
        "fukamiru.revisions.surface_warning.shutil.which", lambda _: "codex"
    )
    monkeypatch.setattr(
        "fukamiru.revisions.surface_warning.subprocess.run",
        lambda *_, **__: SimpleNamespace(
            returncode=1,
            stdout="\n".join(json.dumps(event) for event in events),
            stderr="websocket 403; falling back to HTTP",
        ),
    )

    with pytest.raises(LunaConfigurationError, match="not supported"):
        call_once(
            [
                {
                    "id": "patch-1",
                    "original": "原文。",
                    "candidate": "候補。",
                    "changed_spans": [],
                }
            ],
            isolated=tmp_path,
        )


def test_v30_selection_applies_only_sol_high_confidence_decisions(
    tmp_path,
) -> None:
    db = SQLiteDatabase(tmp_path / "state.db", tmp_path)
    with db.connect() as connection:
        connection.executescript(
            "CREATE TABLE manuscript_proofreading_sentences ("
            "id TEXT,run_id TEXT,status TEXT);"
            "CREATE TABLE manuscript_proofreading_patches ("
            "id TEXT,run_id TEXT,sentence_id TEXT,status TEXT,apply_allowed INTEGER,"
            "selected INTEGER,original_text TEXT,normalized_candidate_text TEXT,"
            "frequency INTEGER,luna_result_json TEXT,candidate_ordinal INTEGER,"
            "updated_at TEXT);"
            "CREATE TABLE manuscript_proofreading_logs ("
            "id TEXT,run_id TEXT,patch_id TEXT,sentence_id TEXT,stage TEXT,"
            "status TEXT,reason TEXT,version TEXT,payload_json TEXT,created_at TEXT);"
            "CREATE TABLE event_log (event_id INTEGER PRIMARY KEY,run_id TEXT,"
            "event_type TEXT,object_type TEXT,object_id TEXT,payload_json TEXT,created_at TEXT);"
        )
        for sentence_id in ("s1", "s2"):
            connection.execute(
                "INSERT INTO manuscript_proofreading_sentences VALUES (?,?,'generated')",
                (sentence_id, "run"),
            )
        clean = json.dumps({"label": "clean", "confidence": "high"})
        rows = [
            ("short", "s1", "表現を調べる。", "表現を検討する。"),
            ("long", "s1", "表現を調べる。", "この表現について詳細に検討する。"),
            ("tie-a", "s2", "結果を述べる。", "結果を示す。"),
            ("tie-b", "s2", "結果を述べる。", "結果を記す。"),
        ]
        connection.executemany(
            "INSERT INTO manuscript_proofreading_patches VALUES "
            "(?, 'run', ?, 'clean', 1, 0, ?, ?, 1, ?, ?, 'now')",
            [(*row, clean, ordinal) for ordinal, row in enumerate(rows)],
        )
    _add_generation_columns(db)
    worker = SimpleNamespace(databases=SimpleNamespace(state=db))
    claimed = SimpleNamespace(run_id="run", node_run_id="node")

    result = select_candidates(
        worker,
        claimed,
        {
            "_proofreading_ranked_candidate_ids": {"s1": "short"},
            "_proofreading_ranked_original_sentence_ids": ["s2"],
            "auto_apply_japanese_proofreading": True,
        },
    )

    assert result["_proofreading_auto_selected_candidate_ids"] == ["short"]
    assert result["manuscript_proofreading_selection"]["selected_candidate_count"] == 1
    assert result["manuscript_proofreading_selection"][
        "waiting_selection_sentence_count"
    ] == 1
    with db.connect() as connection:
        selected = connection.execute(
            "SELECT id FROM manuscript_proofreading_patches WHERE selected=1"
        ).fetchall()
    assert [row["id"] for row in selected] == ["short"]


def test_v30_ranker_includes_original_and_auto_selects_only_high_confidence(
    tmp_path,
) -> None:
    db = SQLiteDatabase(tmp_path / "state.db", tmp_path)
    with db.connect() as connection:
        connection.executescript(
            "CREATE TABLE manuscript_proofreading_sentences ("
            "id TEXT,run_id TEXT,block_id TEXT,sentence_ordinal INTEGER,"
            "original_text TEXT,status TEXT);"
            "CREATE TABLE manuscript_proofreading_patches ("
            "id TEXT,run_id TEXT,sentence_id TEXT,status TEXT,apply_allowed INTEGER,"
            "normalized_candidate_text TEXT,luna_result_json TEXT,candidate_ordinal INTEGER);"
            "CREATE TABLE manuscript_proofreading_logs ("
            "id TEXT,run_id TEXT,patch_id TEXT,sentence_id TEXT,stage TEXT,"
            "status TEXT,reason TEXT,version TEXT,payload_json TEXT,created_at TEXT);"
            "CREATE TABLE event_log (event_id INTEGER PRIMARY KEY,run_id TEXT,"
            "event_type TEXT,object_type TEXT,object_id TEXT,payload_json TEXT,created_at TEXT);"
        )
        connection.executemany(
            "INSERT INTO manuscript_proofreading_sentences VALUES "
            "(?, 'run', 'block', ?, ?, 'generated')",
            [("s1", 0, "文を調べる。"), ("s2", 1, "可能性がある。")],
        )
        clean = json.dumps({"label": "clean", "confidence": "high"})
        connection.executemany(
            "INSERT INTO manuscript_proofreading_patches VALUES "
            "(?, 'run', ?, 'clean', 1, ?, ?, ?)",
            [
                ("p1", "s1", "文を検討する。", clean, 0),
                ("p2", "s2", "事実である。", clean, 0),
            ],
        )

    captured: dict[str, object] = {}

    def generate(_claimed, *, stage, payloads, operation_name):
        captured.update(stage=stage, payloads=payloads, operation_name=operation_name)
        return [
            {
                "decisions": [
                    {
                        "sentence_id": "s1",
                        "selected_candidate_id": "p1",
                        "confidence": "high",
                        "reason_codes": ["grammar_improvement"],
                    },
                    {
                        "sentence_id": "s2",
                        "selected_candidate_id": "p2",
                        "confidence": "medium",
                        "reason_codes": ["modality_risk"],
                    },
                ]
            }
        ]

    _add_generation_columns(db)
    worker = SimpleNamespace(
        databases=SimpleNamespace(state=db),
        _authoring_parallel_generate=generate,
    )
    claimed = SimpleNamespace(run_id="run", node_run_id="node")

    result = rank_candidates(worker, claimed, {})

    assert captured["stage"] == "proofreading_selection"
    sentence_payloads = captured["payloads"][0]["sentences"]
    assert sentence_payloads[0]["original"] == "文を調べる。"
    assert sentence_payloads[0]["right_context"] == "可能性がある。"
    assert result["_proofreading_ranked_candidate_ids"] == {"s1": "p1"}
    assert result["_proofreading_ranked_original_sentence_ids"] == ["s2"]
    assert result["manuscript_proofreading_preference"][
        "auto_selected_sentence_count"
    ] == 1


def test_shadow_surface_normalization_makes_source_changed_patch_stale(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = SQLiteDatabase(tmp_path / "state.db", tmp_path)
    with db.connect() as connection:
        connection.executescript(
            "CREATE TABLE manuscript_proofreading_sentences ("
            "id TEXT,run_id TEXT,block_id TEXT,source_sentence_id TEXT,"
            "start_offset INTEGER,end_offset INTEGER,original_text TEXT,status TEXT);"
            "CREATE TABLE manuscript_proofreading_patches ("
            "id TEXT,run_id TEXT,sentence_id TEXT,status TEXT,status_reasons_json TEXT,"
            "apply_allowed INTEGER,selected INTEGER,applied INTEGER DEFAULT 0,original_hash TEXT,"
            "normalized_candidate_text TEXT,updated_at TEXT);"
            "CREATE TABLE manuscript_proofreading_logs ("
            "id TEXT,run_id TEXT,patch_id TEXT,sentence_id TEXT,stage TEXT,"
            "status TEXT,reason TEXT,version TEXT,payload_json TEXT,created_at TEXT);"
            "CREATE TABLE event_log (event_id INTEGER PRIMARY KEY,run_id TEXT,"
            "event_type TEXT,object_type TEXT,object_id TEXT,payload_json TEXT,created_at TEXT);"
        )
        original = "「A」「B」を比較する。"
        connection.execute(
            "INSERT INTO manuscript_proofreading_sentences VALUES (?,?,?,?,?,?,?,?)",
            ("sentence", "run", "p", "p:s0", 0, len(original), original, "generated"),
        )
        connection.execute(
            "INSERT INTO manuscript_proofreading_patches VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                "patch",
                "run",
                "sentence",
                "clean",
                "[]",
                1,
                1,
                0,
                digest(original),
                "「A」「B」を検討する。",
                "now",
            ),
        )
        connection.execute(
            "INSERT INTO manuscript_proofreading_sentences VALUES (?,?,?,?,?,?,?,?)",
            ("old-fragment", "run", "p", "p:old", 0, 4, "「A」", "segmentation_superseded"),
        )
        connection.execute(
            "INSERT INTO manuscript_proofreading_patches VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                "old-patch",
                "run",
                "old-fragment",
                "input_error",
                '["segmentation_superseded"]',
                0,
                0,
                0,
                digest("「A」"),
                "「B」",
                "now",
            ),
        )
    monkeypatch.setattr(
        "fukamiru.worker.sentence_proofreading.ManuscriptDiscourseService.predicate_candidates",
        lambda *args, **kwargs: [],
    )
    _add_generation_columns(db)
    worker = SimpleNamespace(
        databases=SimpleNamespace(state=db),
        _manuscript_predicate_inventory_record=lambda value: value,
    )
    claimed = SimpleNamespace(run_id="run", node_run_id="node")
    html = '<article id="d"><p id="p">「A」「B」を比較する。</p></article>'
    result = publish_shadow(
        worker,
        claimed,
        {
            "proposed_html": html,
            "_node14_checkpoint": {"source_html_sha256": digest(html)},
        },
    )
    with db.connect() as connection:
        patch = connection.execute(
            "SELECT status,apply_allowed,selected FROM manuscript_proofreading_patches "
            "WHERE id='patch'"
        ).fetchone()
    assert tuple(patch) == ("stale", 0, 0)
    assert result["proposed_html"] != html
    assert result["manuscript_proofreading_shadow"]["auto_applied_count"] == 0
    assert result["manuscript_proofreading_shadow"]["active_candidate_count"] == 1
    assert result["manuscript_proofreading_shadow"]["superseded_candidate_count"] == 1
    assert result["manuscript_surface_quality_gate"]["passed"] is True


def test_selected_clean_candidate_is_applied_to_proposed_manuscript(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = SQLiteDatabase(tmp_path / "state.db", tmp_path)
    with db.connect() as connection:
        connection.executescript(
            "CREATE TABLE manuscript_proofreading_sentences ("
            "id TEXT,run_id TEXT,block_id TEXT,source_sentence_id TEXT,"
            "start_offset INTEGER,end_offset INTEGER,original_text TEXT,status TEXT);"
            "CREATE TABLE manuscript_proofreading_patches ("
            "id TEXT,run_id TEXT,sentence_id TEXT,status TEXT,status_reasons_json TEXT,"
            "apply_allowed INTEGER,selected INTEGER,applied INTEGER DEFAULT 0,original_hash TEXT,"
            "normalized_candidate_text TEXT,updated_at TEXT);"
            "CREATE TABLE manuscript_proofreading_logs ("
            "id TEXT,run_id TEXT,patch_id TEXT,sentence_id TEXT,stage TEXT,"
            "status TEXT,reason TEXT,version TEXT,payload_json TEXT,created_at TEXT);"
            "CREATE TABLE event_log (event_id INTEGER PRIMARY KEY,run_id TEXT,"
            "event_type TEXT,object_type TEXT,object_id TEXT,payload_json TEXT,created_at TEXT);"
        )
        first = "表現を調べる。"
        second = "結果を述べる。"
        block = first + second
        connection.execute(
            "INSERT INTO manuscript_proofreading_sentences VALUES (?,?,?,?,?,?,?,?)",
            ("s1", "run", "p", "p:s0", 0, len(first), first, "generated"),
        )
        connection.execute(
            "INSERT INTO manuscript_proofreading_sentences VALUES (?,?,?,?,?,?,?,?)",
            (
                "s2",
                "run",
                "p",
                "p:s1",
                len(first),
                len(block),
                second,
                "generated",
            ),
        )
        connection.execute(
            "INSERT INTO manuscript_proofreading_patches VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                "patch-1",
                "run",
                "s1",
                "clean",
                "[]",
                1,
                1,
                0,
                digest(first),
                "表現を検討する。",
                "now",
            ),
        )
        connection.execute(
            "INSERT INTO manuscript_proofreading_patches VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                "patch-2",
                "run",
                "s2",
                "clean",
                "[]",
                1,
                0,
                1,
                digest(second),
                "結果を提示する。",
                "now",
            ),
        )
    predicate_sources: list[str] = []

    def capture_predicate_source(canonical_html: str, **kwargs: object) -> list[object]:
        predicate_sources.append(canonical_html)
        return []

    monkeypatch.setattr(
        "fukamiru.worker.sentence_proofreading.ManuscriptDiscourseService.predicate_candidates",
        capture_predicate_source,
    )
    _add_generation_columns(db)
    worker = SimpleNamespace(
        databases=SimpleNamespace(state=db),
        _manuscript_predicate_inventory_record=lambda value: value,
    )
    claimed = SimpleNamespace(run_id="run", node_run_id="node")
    html = f'<article id="d"><p id="p">{block}</p></article>'

    result = publish_shadow(
        worker,
        claimed,
        {
            "proposed_html": html,
            "_node14_checkpoint": {"source_html_sha256": digest(html)},
        },
    )

    assert "表現を検討する。結果を述べる。" in result["proposed_html"]
    assert predicate_sources == [result["proposed_html"]]
    assert result["manuscript_proofreading_shadow"]["selected_count"] == 1
    assert result["manuscript_proofreading_shadow"]["applied_count"] == 1
    assert result["manuscript_candidate_selection"]["block_audits"] == [
        {
            "block_id": "p",
            "status": "applied",
            "patch_ids": ["patch-1"],
            "issues": [],
        }
    ]
    assert result["proofreading_application"]["selected_count"] == 1
    assert result["proofreading_application"]["applied_count"] == 1
    with db.connect() as connection:
        flags = connection.execute(
            "SELECT id,applied FROM manuscript_proofreading_patches ORDER BY id"
        ).fetchall()
    assert [tuple(row) for row in flags] == [("patch-1", 1), ("patch-2", 0)]


def test_locked_authoring_text_rejects_only_overlapping_selected_patch(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = SQLiteDatabase(tmp_path / "state.db", tmp_path)
    with db.connect() as connection:
        connection.executescript(
            "CREATE TABLE manuscript_proofreading_sentences ("
            "id TEXT,run_id TEXT,block_id TEXT,source_sentence_id TEXT,"
            "start_offset INTEGER,end_offset INTEGER,original_text TEXT,status TEXT);"
            "CREATE TABLE manuscript_proofreading_patches ("
            "id TEXT,run_id TEXT,sentence_id TEXT,status TEXT,status_reasons_json TEXT,"
            "apply_allowed INTEGER,selected INTEGER,applied INTEGER DEFAULT 0,original_hash TEXT,"
            "normalized_candidate_text TEXT,updated_at TEXT);"
            "CREATE TABLE manuscript_proofreading_logs ("
            "id TEXT,run_id TEXT,patch_id TEXT,sentence_id TEXT,stage TEXT,"
            "status TEXT,reason TEXT,version TEXT,payload_json TEXT,created_at TEXT);"
            "CREATE TABLE event_log (event_id INTEGER PRIMARY KEY,run_id TEXT,"
            "event_type TEXT,object_type TEXT,object_id TEXT,payload_json TEXT,created_at TEXT);"
        )
        locked = "研究課題を示す。"
        editable = "手順を述べる。"
        block = locked + editable
        for sentence_id, source_sentence_id, start, end, text in (
            ("s1", "p:s0", 0, len(locked), locked),
            ("s2", "p:s1", len(locked), len(block), editable),
        ):
            connection.execute(
                "INSERT INTO manuscript_proofreading_sentences VALUES (?,?,?,?,?,?,?,?)",
                (
                    sentence_id,
                    "run",
                    "p",
                    source_sentence_id,
                    start,
                    end,
                    text,
                    "generated",
                ),
            )
        for patch_id, sentence_id, original, candidate in (
            ("patch-locked", "s1", locked, "研究上の課題を示す。"),
            ("patch-editable", "s2", editable, "手順を説明する。"),
        ):
            connection.execute(
                "INSERT INTO manuscript_proofreading_patches VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    patch_id,
                    "run",
                    sentence_id,
                    "clean",
                    "[]",
                    1,
                    1,
                    0,
                    digest(original),
                    candidate,
                    "now",
                ),
            )
    monkeypatch.setattr(
        "fukamiru.worker.sentence_proofreading.ManuscriptDiscourseService.predicate_candidates",
        lambda *args, **kwargs: [],
    )
    _add_generation_columns(db)
    worker = SimpleNamespace(
        databases=SimpleNamespace(state=db),
        _manuscript_predicate_inventory_record=lambda value: value,
    )
    claimed = SimpleNamespace(run_id="run", node_run_id="node")
    html = f'<article id="d"><p id="p">{block}</p></article>'

    result = publish_shadow(
        worker,
        claimed,
        {
            "proposed_html": html,
            "_node14_checkpoint": {"source_html_sha256": digest(html)},
            "_proofreading_locked_texts": [locked],
        },
    )

    assert "研究課題を示す。手順を説明する。" in result["proposed_html"]
    assert result["manuscript_proofreading_shadow"][
        "locked_contract_rejection_count"
    ] == 1
    assert result["manuscript_proofreading_shadow"]["selected_count"] == 1
    with db.connect() as connection:
        flags = connection.execute(
            "SELECT id,selected,applied FROM manuscript_proofreading_patches ORDER BY id"
        ).fetchall()
        rejection = connection.execute(
            "SELECT reason FROM manuscript_proofreading_logs "
            "WHERE patch_id='patch-locked' AND stage='application'"
        ).fetchone()
    assert [tuple(row) for row in flags] == [
        ("patch-editable", 1, 1),
        ("patch-locked", 0, 0),
    ]
    assert rejection["reason"] == "locked_authoring_contract_text"
