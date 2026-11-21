import json

import pytest

from fukamiru.revisions import RevisionService
from fukamiru.storage.payloads import PayloadStore
from fukamiru.storage.runs import RunRepository


@pytest.mark.parametrize("problem", [None, "wrong_field", "first_attempt", "unknown_basis"])
@pytest.mark.parametrize("prior_recoveries", [False, True])
def test_field_anchor_replay_requires_second_failure_and_preserves_all_checkpoints(
    case, tmp_path, problem, prior_recoveries,
):
    from fukamiru.authoring import concept_fit
    from fukamiru.storage.events import EventRepository

    run_id, original = source_run(case, tmp_path)
    original["propositions"] = [{"id": "p1", "text": "共同作業で制作する。"},
                                {"id": "p2", "text": "ハビトゥスを定義して用いる。"}]
    native = {"claims": [{"id": "c1", "text": "共同作業で制作する。"},
                          {"id": "c2", "text": "ハビトゥスを定義して用いる。"}]}
    review = {"units": [{"unit_id": "/claims/99/text" if problem == "wrong_field"
                         else "/claims/0/text", "concepts": [{
                             "term": "共有知", "basis_ids": ["p1"], "verdict": "appropriate",
                             "must_keep": False, "reason": "制作過程を説明する。",
                         }]}, {"unit_id": "/claims/1/text", "concepts": [{
                             "term": "ハビトゥス", "basis_ids": [
                                 "invented" if problem == "unknown_basis" else "p2",
                             ], "verdict": "appropriate", "must_keep": True,
                             "reason": "定義済みの概念。",
                         }]}], "omissions": []}
    error = {"type": "NonRetryableFukamiruError", "message":
             "Concept review excerpt is not in its field: /claims/0/text: '共有知'"}
    base = "authoring_build_argument_graph_batch_9_repair"
    with case.app.state.databases.state.connect() as c:
        c.execute("UPDATE node_runs SET node_id='build_argument_graph',status='failed',"
                  "error_json=?,input_json=? WHERE run_id=?",
                  (json.dumps(error), json.dumps(original), run_id))
        c.execute("UPDATE runs SET status='failed',error_json=? WHERE id=?",
                  (json.dumps(error), run_id))
        node_id = c.execute("SELECT id FROM node_runs WHERE run_id=?", (run_id,)).fetchone()[0]
        if prior_recoveries:
            for policy in ("authoring-v32-bounded-concept-repair-v2",
                           "authoring-v32-basis-identity-review-v1",
                           "authoring-v32-verification-routing-recovery-v1"):
                EventRepository(c).append(run_id, "run.failed_authoring_node_replayed",
                                          object_type="node_run", object_id=node_id,
                                          payload={"policy": policy})
        attempt = "0" if problem == "first_attempt" else "1"
        name = concept_fit.digest([base + ":concept-review:invalid:" + attempt,
                                   concept_fit.digest(native)])[:24]
        report = tmp_path / (name + ".json")
        report.write_text(json.dumps({"status": "invalid_review", "stage": "argument_graph_v31",
                                      "error": error["message"], "initial_review": review,
                                      "candidate_sha256": concept_fit.digest(native)}),
                          encoding="utf-8")
        c.execute("INSERT INTO run_artifacts(id,run_id,node_run_id,artifact_type,path,"
                  "media_type,title,created_at) VALUES('anchor-report',?,?,"
                  "'authoring_concept_fit_report',?,'application/json','review','2026-11-12')",
                  (run_id, node_id, str(report)))
        c.execute(
            "INSERT INTO llm_result_checkpoints(id,run_id,node_id,stage,canonical_stage,"
            "operation_name,request_fingerprint_sha256,full_prompt_sha256,cache_family_sha256,"
            "result_json,validation_json,status,created_at,updated_at) "
            "VALUES('anchor-native',?,'build_argument_graph','argument_graph_v31',"
            "'argument_graph_v31',?,?,?,?,?,?,'accepted','2026-11-12','2026-11-12')",
            (run_id, base, 'a' * 64, 'b' * 64, 'c' * 64, json.dumps(native), '{}'),
        )
        before = tuple(c.execute("SELECT * FROM llm_result_checkpoints").fetchone())
        c.commit()
    url = f"/api/runs/{run_id}/replay-failed-authoring-node"
    response = case.client.post(url)
    assert response.status_code == (409 if problem else 200), response.text
    with case.app.state.databases.state.connect() as c:
        assert tuple(c.execute("SELECT * FROM llm_result_checkpoints").fetchone()) == before
        if problem is None:
            payload = json.loads(c.execute("SELECT payload_json FROM event_log WHERE run_id=? "
                "AND event_type='run.failed_authoring_node_replayed' ORDER BY rowid DESC LIMIT 1",
                (run_id,),
            ).fetchone()[0])
            assert payload["previous_node_error"] == error
            assert payload["policy"] == "authoring-v32-field-anchor-review-v1"
            c.execute("UPDATE node_runs SET status='failed',error_json=? WHERE run_id=?",
                      (json.dumps(error), run_id))
            c.execute("UPDATE runs SET status='failed',error_json=? WHERE id=?",
                      (json.dumps(error), run_id))
            c.commit()
    if problem is None:
        assert case.client.post(url).status_code == 409


@pytest.mark.parametrize("problem", [None, "no_work_state", "missing_native", "invalid_edit"])
@pytest.mark.parametrize("second_round", [False, True])
def test_cancelled_graph_replay_repairs_lost_verification_notes_once(
    case, tmp_path, problem, second_round,
):
    from fukamiru.storage.events import EventRepository

    run_id, original = source_run(case, tmp_path)
    native = {"text": "対象の性質を述べる。" if problem == "no_work_state"
              else "外部根拠が未解決であるため、この主張の根拠には含めない。"}
    changes = {"edits": [{"unit_id": "/unknown" if problem == "invalid_edit" else "/text",
                           "operation": "replace", "text": "媒体の配置を分析する。"}]}
    base = "authoring_build_argument_graph_batch_8_repair"
    with case.app.state.databases.state.connect() as c:
        c.execute("UPDATE node_runs SET node_id='build_argument_graph',status='running',"
                  "input_json=? WHERE run_id=?", (json.dumps(original), run_id))
        c.execute("UPDATE runs SET status='running' WHERE id=?", (run_id,))
        node_id = c.execute("SELECT id FROM node_runs WHERE run_id=?", (run_id,)).fetchone()[0]
        for policy in ("authoring-v32-bounded-concept-repair-v2",
                       "authoring-v32-basis-identity-review-v1"):
            EventRepository(c).append(run_id, "run.failed_authoring_node_replayed",
                                      object_type="node_run", object_id=node_id,
                                      payload={"policy": policy})
        checkpoints = [("repair_concept_fit_v32", base + ":concept-repair", changes)]
        if problem != "missing_native":
            checkpoints.insert(0, ("argument_graph_v31", base, native))
        if second_round:
            checkpoints.append(("repair_concept_fit_v32", base + ":concept-repair:2",
                                {"edits": []}))
        for i, (stage, operation, result) in enumerate(checkpoints):
            c.execute(
                "INSERT INTO llm_result_checkpoints(id,run_id,node_id,stage,canonical_stage,"
                "operation_name,request_fingerprint_sha256,full_prompt_sha256,cache_family_sha256,"
                "result_json,validation_json,status,created_at,updated_at) "
                "VALUES(?,?,'build_argument_graph',?,?,?,?,?,?,?,?,'accepted',?,?)",
                (f"routing-{i}", run_id, stage, stage, operation, str(i) * 64,
                 'b' * 64, 'c' * 64, json.dumps(result), '{}', f"2026-11-12T00:0{i}",
                 f"2026-11-12T00:0{i}"),
            )
        before = [tuple(r) for r in c.execute("SELECT * FROM llm_result_checkpoints")]
        c.commit()
    assert case.client.post(f"/api/runs/{run_id}/cancel").status_code == 200
    url = f"/api/runs/{run_id}/replay-failed-authoring-node"
    response = case.client.post(url)
    assert response.status_code == (409 if problem else 200), response.text
    with case.app.state.databases.state.connect() as c:
        assert [tuple(r) for r in c.execute("SELECT * FROM llm_result_checkpoints")] == before
        if problem is None:
            assert c.execute(
                "SELECT status FROM runs WHERE id=?", (run_id,),
            ).fetchone()[0] == "queued"
            assert c.execute(
                "SELECT status FROM node_runs WHERE id=?", (node_id,),
            ).fetchone()[0] == "queued"
            replay = json.loads(c.execute(
                "SELECT payload_json FROM event_log WHERE run_id=? "
                "AND event_type='run.failed_authoring_node_replayed' ORDER BY rowid DESC LIMIT 1",
                (run_id,),
            ).fetchone()[0])
            assert replay["previous_node_error"]["type"] == "RunCancelled"
            assert replay["policy"] == "authoring-v32-verification-routing-recovery-v1"
    if problem is None:
        assert case.client.post(f"/api/runs/{run_id}/cancel").status_code == 200
        assert case.client.post(url).status_code == 409


@pytest.mark.parametrize("problem", [None, "invented", "wrong_term", "missing_native"])
@pytest.mark.parametrize("previous_bounded", [False, True])
def test_basis_identity_replay_revalidates_saved_result_and_is_bounded(
    case, tmp_path, problem, previous_bounded,
):
    from fukamiru.authoring import concept_fit

    run_id, original = source_run(case, tmp_path)
    original["evidence_coverage"] = [{"obligation_id": "evidence:known"}]
    native = {"text": "外部的事実性"}
    review = {"units": [{"unit_id": "/text", "concepts": [{
        "term": "別の語" if problem == "wrong_term" else "外部的事実性",
        "basis_ids": ["evidence:invented" if problem == "invented" else "evidence:known"],
        "verdict": "appropriate", "must_keep": False, "reason": "提示された確認項目。",
    }]}], "omissions": []}
    error = {"type": "NonRetryableFukamiruError", "message": "Concept review invented a basis ID"}
    base = "authoring_build_argument_graph_batch_4"
    with case.app.state.databases.state.connect() as c:
        c.execute("UPDATE node_runs SET node_id='build_argument_graph',status='failed',"
                  "error_json=?,input_json=? WHERE run_id=?",
                  (json.dumps(error), json.dumps(original), run_id))
        c.execute("UPDATE runs SET status='failed',error_json=? WHERE id=?",
                  (json.dumps(error), run_id))
        report = tmp_path / "invalid-review.json"
        report.write_text(json.dumps({
            "status": "invalid_review", "stage": "argument_graph_v31", "error": error["message"],
            "candidate_sha256": concept_fit.digest(native), "initial_review": review,
        }), encoding="utf-8")
        node_id = c.execute("SELECT id FROM node_runs WHERE run_id=?", (run_id,)).fetchone()[0]
        if previous_bounded:
            from fukamiru.storage.events import EventRepository

            EventRepository(c).append(run_id, "run.failed_authoring_node_replayed",
                                      object_type="node_run", object_id=node_id, payload={
                                          "policy": "authoring-v32-bounded-concept-repair-v2",
                                      })
        c.execute("INSERT INTO run_artifacts(id,run_id,node_run_id,artifact_type,path,"
                  "media_type,title,created_at) VALUES('basis-report',?,?,"
                  "'authoring_concept_fit_report',?,'application/json','review','2026-11-12')",
                  (run_id, node_id, str(report)))
        checkpoints = []
        if problem != "missing_native":
            checkpoints.insert(0, ("argument_graph_v31", base, native))
        for i, (stage, operation, result) in enumerate(checkpoints):
            c.execute(
                "INSERT INTO llm_result_checkpoints(id,run_id,node_id,stage,canonical_stage,"
                "operation_name,request_fingerprint_sha256,full_prompt_sha256,cache_family_sha256,"
                "result_json,validation_json,status,created_at,updated_at) "
                "VALUES(?,?,'build_argument_graph',?,?,?,?,?,?,?,?,'accepted',?,?)",
                (f"basis-{i}", run_id, stage, stage, operation, str(i) * 64,
                 'b' * 64, 'c' * 64, json.dumps(result), '{}', f"2026-11-12T00:0{i}",
                 f"2026-11-12T00:0{i}"),
            )
        before = [tuple(r) for r in c.execute("SELECT * FROM llm_result_checkpoints")]
        c.commit()
    url = f"/api/runs/{run_id}/replay-failed-authoring-node"
    response = case.client.post(url)
    assert response.status_code == (409 if problem else 200), response.text
    with case.app.state.databases.state.connect() as c:
        assert [tuple(r) for r in c.execute("SELECT * FROM llm_result_checkpoints")] == before
        if problem is None:
            replay = json.loads(c.execute(
                "SELECT payload_json FROM event_log WHERE run_id=? "
                "AND event_type='run.failed_authoring_node_replayed' ORDER BY rowid DESC LIMIT 1",
                (run_id,),
            ).fetchone()[0])
            assert replay["previous_node_error"] == error
            assert replay["policy"] == "authoring-v32-basis-identity-review-v1"
            c.execute("UPDATE node_runs SET status='failed',error_json=? WHERE run_id=?",
                      (json.dumps(error), run_id))
            c.execute("UPDATE runs SET status='failed',error_json=? WHERE id=?",
                      (json.dumps(error), run_id))
            c.commit()
    if problem is None:
        assert case.client.post(url).status_code == 409


def test_internal_evidence_role_replay_preserves_checkpoints_and_failure(case, tmp_path):
    run_id, original = source_run(case, tmp_path)
    text = "青年誌史への適用内容は外部資料によって検証されていない。"
    graph = {"nodes": [{"node_type": "evidence", "text": text}]}
    error = {"type": "NonRetryableFukamiruError", "message":
             "Concept repair remains unresolved: " + json.dumps([{
                 "unit_id": "/nodes/0/text", "term": text,
                 "verdict": "wording", "must_keep": False,
             }], ensure_ascii=False)}
    with case.app.state.databases.state.connect() as c:
        c.execute("UPDATE node_runs SET node_id='build_argument_graph',status='failed',"
                  "error_json=?,input_json=? WHERE run_id=?",
                  (json.dumps(error), json.dumps(original), run_id))
        c.execute("UPDATE runs SET status='failed',error_json=? WHERE id=?",
                  (json.dumps(error), run_id))
        c.execute(
            "INSERT INTO llm_result_checkpoints(id,run_id,node_id,stage,canonical_stage,"
            "operation_name,request_fingerprint_sha256,full_prompt_sha256,cache_family_sha256,"
            "result_json,validation_json,status,created_at,updated_at) "
            "VALUES('concept-role',?,'build_argument_graph','argument_graph_v31',"
            "'argument_graph_v31','graph',?,?,?,?,?,'accepted','2026-11-12','2026-11-12')",
            (run_id, 'a' * 64, 'b' * 64, 'c' * 64, json.dumps(graph), '{}'),
        )
        checkpoint = tuple(c.execute("SELECT * FROM llm_result_checkpoints").fetchone())
        c.commit()
    url = f"/api/runs/{run_id}/replay-failed-authoring-node"
    response = case.client.post(url)
    assert response.status_code == 200, response.text
    with case.app.state.databases.state.connect() as c:
        assert tuple(c.execute("SELECT * FROM llm_result_checkpoints").fetchone()) == checkpoint
        replay = json.loads(c.execute(
            "SELECT payload_json FROM event_log WHERE run_id=? "
            "AND event_type='run.failed_authoring_node_replayed'", (run_id,),
        ).fetchone()[0])
        assert replay["previous_node_error"] == error
        assert replay["policy"] == "authoring-v32-internal-evidence-role-review-v1"
        c.execute("UPDATE node_runs SET status='failed',error_json=? WHERE run_id=?",
                  (json.dumps(error), run_id))
        c.execute("UPDATE runs SET status='failed',error_json=? WHERE id=?",
                  (json.dumps(error), run_id))
        c.commit()
    assert case.client.post(url).status_code == 409


@pytest.mark.parametrize("problem", [None, "missing_repair", "wrong_term", "second_repair"])
def test_first_repair_regression_replay_requires_matching_saved_checkpoints(
    case, tmp_path, problem,
):
    from fukamiru.authoring import concept_fit

    run_id, original = source_run(case, tmp_path)
    native = {"nodes": [{"id": "w1", "node_type": "warrant", "text": "媒体は混成している。"}],
              "claim_decisions": [{"claim_id": "c1", "adopted_text": "媒体は混成している。"}]}
    changes = {"edits": [{"unit_id": "/nodes/0/text", "operation": "replace",
                           "text": "当初から媒体は混成している。"}]}
    concept = {"term": "別の語" if problem == "wrong_term" else "当初から",
               "meaning": "新しい歴史的前提", "application_basis": "共存だけを示す資料",
               "argument_function": "起源まで一般化する。", "basis_ids": [],
               "verdict": "unsupported_premise", "must_keep": False,
               "reason": "元資料にない前提を修復で追加した。"}
    review = {"units": [{"unit_id": "/nodes/0/text", "concepts": [concept]},
                        {"unit_id": "/claim_decisions/0/adopted_text", "concepts": []}],
              "omissions": []}
    error = {"type": "NonRetryableFukamiruError", "message":
             "Concept repair remains unresolved: " + json.dumps(
                 concept_fit.findings(review), ensure_ascii=False,
             )}
    base = "authoring_build_argument_graph_batch_1_repair"
    checkpoints = [("argument_graph_v31", base, native),
                   ("repair_concept_fit_v32", base + ":concept-repair", changes),
                   ("review_concept_fit_v32", base + ":concept-recheck", review)]
    if problem == "missing_repair":
        checkpoints.pop(1)
    if problem == "second_repair":
        checkpoints.append(("repair_concept_fit_v32", base + ":concept-repair:2", changes))
    with case.app.state.databases.state.connect() as c:
        c.execute("UPDATE node_runs SET node_id='build_argument_graph',status='failed',"
                  "error_json=?,input_json=? WHERE run_id=?",
                  (json.dumps(error), json.dumps(original), run_id))
        c.execute("UPDATE runs SET status='failed',error_json=? WHERE id=?",
                  (json.dumps(error), run_id))
        for i, (stage, operation, result) in enumerate(checkpoints):
            c.execute(
                "INSERT INTO llm_result_checkpoints(id,run_id,node_id,stage,canonical_stage,"
                "operation_name,request_fingerprint_sha256,full_prompt_sha256,cache_family_sha256,"
                "result_json,validation_json,status,created_at,updated_at) "
                "VALUES(?,?,'build_argument_graph',?,?,?,?,?,?,?,?,'accepted',?,?)",
                (f"bounded-{i}", run_id, stage, stage, operation, str(i) * 64,
                 'b' * 64, 'c' * 64, json.dumps(result), '{}', f"2026-11-12T00:0{i}",
                 f"2026-11-12T00:0{i}"),
            )
        before = [tuple(r) for r in c.execute("SELECT * FROM llm_result_checkpoints")]
        c.commit()
    url = f"/api/runs/{run_id}/replay-failed-authoring-node"
    response = case.client.post(url)
    assert response.status_code == (409 if problem else 200), response.text
    with case.app.state.databases.state.connect() as c:
        assert [tuple(r) for r in c.execute("SELECT * FROM llm_result_checkpoints")] == before
        if problem is None:
            payload = json.loads(c.execute(
                "SELECT payload_json FROM event_log WHERE run_id=? "
                "AND event_type='run.failed_authoring_node_replayed'", (run_id,),
            ).fetchone()[0])
            assert payload["previous_node_error"] == error
            assert payload["policy"] == "authoring-v32-bounded-concept-repair-v2"
            c.execute("UPDATE node_runs SET status='failed',error_json=? WHERE run_id=?",
                      (json.dumps(error), run_id))
            c.execute("UPDATE runs SET status='failed',error_json=? WHERE id=?",
                      (json.dumps(error), run_id))
            c.commit()
    if problem is None:
        assert case.client.post(url).status_code == 409


@pytest.fixture
def case():
    from test_api import APITests

    instance = APITests()
    instance.setUp()
    try:
        yield instance
    finally:
        instance.tearDown()


def source_run(case, tmp_path):
    with case.app.state.databases.state.connect() as c:
        revisions = RevisionService(c)
        project = revisions.register_project(
            name="Render restart", root_path=str(tmp_path / "project")
        )
        document = revisions.create_document(project_id=project, kind="paper", title="Draft")
        context = {
            "plan_id": "original-plan", "plan_revision": 1, "document_id": document,
            "source_document_id": "original-note", "source_revision_id": "original-revision",
            "research_problem": {"research_question": "Question"},
            "section_contracts": [{"section_id": "S"}], "draft_sections": [{"section_id": "S"}],
            "proposed_html": '<article id="D" lang="ja"><section id="S"><h2 id="H">比較</h2>'
                             '<p id="P">作品を比較する。</p></section></article>',
            "note_text": "原文" * 3000, "evidence_uses": [], "reference_entries": [],
            "human_question_feedback": {"answers": [{"answer": "資料は手元にない。"}]},
            "fixed_assets": {"asset_hash": "unchanged"}, "evidence_identity_hash": "unchanged",
            "final_path": "old-submission.html", "authoring_proposal_id": "old-proposal",
            "final_quality_sha256": "stale", "reference_rendered_html_v26": "stale",
            "submission_validation_gate": {"stale": True},
            "frozen_candidate_v26": {"html_sha256": "stale"},
            "prose_redraft_report_v31": {"stale": True},
            "paragraph_transition_report_v32": {"stale": True},
        }
        created = RunRepository(c).create(
            workflow_name="manuscript_generator", workflow_version="32",
            entry_node="render_evidence_footnotes", project_id=project,
            inputs=PayloadStore(c).pack(context),
        )
        c.execute("UPDATE node_runs SET status='completed',output_json='{}' WHERE run_id=?",
                  (created.run_id,))
        c.execute("UPDATE runs SET status='waiting_human' WHERE id=?", (created.run_id,))
        c.commit()
    return created.run_id, context


@pytest.mark.parametrize("problem", [None, "unknown_claim", "wrong_error", "wrong_node"])
def test_audit_object_replay_requires_resolvable_saved_targets(case, tmp_path, problem):
    run_id, original = source_run(case, tmp_path)
    original["section_contracts"] = [{"section_id": "S", "claim_ids": ["c35"]}]
    original["audit_findings"] = [{"object_type": "claim",
                                   "object_id": "missing" if problem == "unknown_claim"
                                   else "c35", "message": "証拠対応を修正する。"}]
    error = {"type": "ValidationError", "message": "finding references a section outside "
             "the audit target" if problem != "wrong_error" else "other failure"}
    node = "audit_structure" if problem == "wrong_node" else "classify_audit_findings"
    with case.app.state.databases.state.connect() as c:
        c.execute("UPDATE node_runs SET node_id=?,status='failed',error_json=?,input_json=? "
                  "WHERE run_id=?", (node, json.dumps(error), json.dumps(original), run_id))
        c.execute("UPDATE runs SET status='failed',error_json=? WHERE id=?",
                  (json.dumps(error), run_id))
        c.commit()
    response = case.client.post(f"/api/runs/{run_id}/replay-failed-authoring-node")
    assert response.status_code == (200 if problem is None else 409), response.text
    if problem is None:
        with case.app.state.databases.state.connect() as c:
            payload = json.loads(c.execute(
                "SELECT payload_json FROM event_log WHERE run_id=? AND "
                "event_type='run.failed_authoring_node_replayed' ORDER BY rowid DESC LIMIT 1",
                (run_id,),
            ).fetchone()[0])
            assert payload["previous_node_error"] == error
            assert payload["policy"] == "authoring-audit-object-section-recovery-v1"
            assert PayloadStore(c).unpack(json.loads(c.execute(
                "SELECT input_json FROM node_runs WHERE run_id=?", (run_id,),
            ).fetchone()[0])) == original


@pytest.mark.parametrize("problem", [None, "invented", "ambiguous", "wrong_field", "first_failure"])
def test_prose_basis_replay_requires_archived_second_failure_and_unique_namespace_typo(
    case, tmp_path, problem,
):
    from fukamiru.authoring import concept_fit as fit

    run_id, original = source_run(case, tmp_path)
    original["argument_nodes"] = [{"id": "b08-r-c1", "text": "経験から形成される性向。"}]
    if problem == "ambiguous":
        original["argument_nodes"].append({"id": "b07-r-c1", "text": "別の根拠。"})
    original["japanese_redrafting_work"] = {"sections": [{"base": {
        "paragraphs": [{"text": "ハビトゥスは経験から形成される性向である。"}],
    }}]}
    native = {"text": "ハビトゥスは経験から形成される性向である。"}
    rejected = {"units": [{"unit_id": "/wrong" if problem == "wrong_field" else "/text",
                           "concepts": [{"term": "ハビトゥス", "basis_ids": [
                               "invented" if problem == "invented" else "b09-r-c1",
                           ], "verdict": "appropriate", "must_keep": True,
                               "reason": "定義された専門概念。"}]}], "omissions": []}
    error = {"type": "NonRetryableFukamiruError", "message": "Concept review invented a basis ID"}
    base = "japanese_paragraph_redraft_0_1"
    attempt = "0" if problem == "first_failure" else "1"
    name = fit.digest([base + ":concept-review:invalid:" + attempt, fit.digest(native)])[:24]
    report = tmp_path / (name + ".json")
    report.write_text(json.dumps({"stage": "japanese_paragraph_redraft_v31",
                                  "status": "invalid_review", "error": error["message"],
                                  "candidate_sha256": fit.digest(native),
                                  "initial_review": rejected}), encoding="utf-8")
    with case.app.state.databases.state.connect() as c:
        c.execute("UPDATE node_runs SET node_id='redraft_japanese_paragraphs',status='failed',"
                  "error_json=?,input_json=? WHERE run_id=?",
                  (json.dumps(error), json.dumps(original), run_id))
        c.execute("UPDATE runs SET status='failed',error_json=? WHERE id=?",
                  (json.dumps(error), run_id))
        node_id = c.execute("SELECT id FROM node_runs WHERE run_id=?", (run_id,)).fetchone()[0]
        c.execute("INSERT INTO run_artifacts(id,run_id,node_run_id,artifact_type,path,"
                  "media_type,title,created_at) VALUES('basis-report',?,?,"
                  "'authoring_concept_fit_report',?,'application/json','review','2026-11-12')",
                  (run_id, node_id, str(report)))
        c.execute("INSERT INTO llm_result_checkpoints(id,run_id,node_id,stage,canonical_stage,"
                  "operation_name,request_fingerprint_sha256,full_prompt_sha256,cache_family_sha256,"
                  "result_json,validation_json,status,created_at,updated_at) "
                  "VALUES('basis-native',?,'redraft_japanese_paragraphs',"
                  "'japanese_paragraph_redraft_v31','japanese_paragraph_redraft_v31',"
                  "?,?,?,?,?,?,'accepted','2026-11-12','2026-11-12')",
                  (run_id, base, 'a' * 64, 'b' * 64, 'c' * 64, json.dumps(native), '{}'))
        checkpoint = tuple(c.execute("SELECT * FROM llm_result_checkpoints").fetchone())
        c.commit()
    url = f"/api/runs/{run_id}/replay-failed-authoring-node"
    response = case.client.post(url)
    assert response.status_code == (200 if problem is None else 409), response.text
    with case.app.state.databases.state.connect() as c:
        assert tuple(c.execute("SELECT * FROM llm_result_checkpoints").fetchone()) == checkpoint
        if problem is None:
            event = json.loads(c.execute("SELECT payload_json FROM event_log WHERE run_id=? "
                "AND event_type='run.failed_authoring_node_replayed' ORDER BY rowid DESC LIMIT 1",
                (run_id,)).fetchone()[0])
            assert event["policy"] == "authoring-v32-prose-basis-diagnostics-v1"
            assert event["previous_node_error"] == error
            c.execute("UPDATE node_runs SET status='failed',error_json=? WHERE run_id=?",
                      (json.dumps(error), run_id))
            c.execute("UPDATE runs SET status='failed',error_json=? WHERE id=?",
                      (json.dumps(error), run_id))
            c.commit()
    if problem is None:
        assert case.client.post(url).status_code == 409


def test_render_restart_branches_saved_input_and_preserves_source(case, tmp_path):
    run_id, original = source_run(case, tmp_path)
    with case.app.state.databases.state.connect() as c:
        before_run = tuple(c.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone())
        before_nodes = [tuple(r) for r in c.execute(
            "SELECT * FROM node_runs WHERE run_id=?", (run_id,)
        )]
    response = case.client.post(f"/api/runs/{run_id}/restart-authoring-from-footnotes")
    assert response.status_code == 201, response.text
    created = response.json()
    assert created["run_id"] != run_id
    assert created["restart_node_id"] == "render_evidence_footnotes"
    with case.app.state.databases.state.connect() as c:
        assert tuple(c.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()) == before_run
        assert [tuple(r) for r in c.execute("SELECT * FROM node_runs WHERE run_id=?", (run_id,))
                ] == before_nodes
        new = RunRepository(c).get(created["run_id"])["input"]
        nodes = c.execute("SELECT node_id,status FROM node_runs WHERE run_id=?",
                          (created["run_id"],)).fetchall()
        assert [tuple(r) for r in nodes] == [("render_evidence_footnotes", "queued")]
    for key in ("proposed_html", "note_text", "draft_sections", "fixed_assets",
                "evidence_identity_hash", "human_question_feedback", "plan_id"):
        assert new[key] == original[key]
    for key in ("final_path", "authoring_proposal_id", "final_quality_sha256",
                "reference_rendered_html_v26", "frozen_candidate_v26",
                "submission_validation_gate", "prose_redraft_report_v31",
                "paragraph_transition_report_v32"):
        assert key not in new
    assert new["authoring_render_restart_lineage"]["source_run_id"] == run_id
    discarded = new["authoring_render_restart_lineage"]["discarded_result_hashes"]
    assert "final_quality_sha256" in discarded


@pytest.mark.parametrize("problem", ["active", "version", "missing", "failed_tail", "approved"])
def test_render_restart_refuses_unsafe_source_without_creating_run(case, tmp_path, problem):
    run_id, original = source_run(case, tmp_path)
    with case.app.state.databases.state.connect() as c:
        if problem == "active":
            c.execute("UPDATE runs SET status='running' WHERE id=?", (run_id,))
        elif problem == "version":
            c.execute("UPDATE runs SET workflow_version='31' WHERE id=?", (run_id,))
        elif problem == "missing":
            original.pop("proposed_html")
            c.execute("UPDATE node_runs SET input_json=? WHERE run_id=?",
                      (json.dumps(original), run_id))
        elif problem == "failed_tail":
            c.execute("UPDATE node_runs SET status='failed' WHERE run_id=?", (run_id,))
        else:
            RevisionService(c).commit_revision(
                document_id=original["document_id"], canonical_html=original["proposed_html"],
                editor_type="human", operation="create", message="approved",
            )
        c.commit()
        before = c.execute("SELECT count(*) FROM runs").fetchone()[0]
    response = case.client.post(f"/api/runs/{run_id}/restart-authoring-from-footnotes")
    assert response.status_code == 409, response.text
    with case.app.state.databases.state.connect() as c:
        assert c.execute("SELECT count(*) FROM runs").fetchone()[0] == before


def test_render_restart_unknown_source_is_404(case):
    response = case.client.post("/api/runs/missing/restart-authoring-from-footnotes")
    assert response.status_code == 404


def test_argument_restart_rebuilds_both_join_branches_and_preserves_old_run(
    case, tmp_path, monkeypatch,
):
    run_id, original = source_run(case, tmp_path)
    original.update({
        "research_question": {"text": "作品の比較は何を示すか。"},
        "claims": [{"id": "c1", "text": "作品を比較する。"}],
        "propositions": [{"id": "p1", "text": "作品が同じ誌面に載る。"}],
        "classifications": [{"proposition_id": "p1", "type": "observation"}],
        "evidence_context": [{"id": "e1", "text": "確認済みの資料"}],
        "claim_scopes": [{"proposition": "old scope"}],
    })
    with case.app.state.databases.state.connect() as c:
        c.execute("UPDATE node_runs SET node_id='build_argument_graph',input_json=? WHERE run_id=?",
                  (json.dumps(original), run_id))
        c.commit()
        before = tuple(c.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone())
        before_nodes = [tuple(r) for r in c.execute(
            "SELECT * FROM node_runs WHERE run_id=?", (run_id,),
        )]
    submitted = {"policy": "human decisions are not evidence", "answers": [{
        "key": "question-key", "answer": "資料は手元にない。", "llm_overreach": True,
    }], "overall_comments": []}
    monkeypatch.setattr("fukamiru.review_feedback.feedback_context", lambda *args: submitted)
    response = case.client.post(f"/api/runs/{run_id}/restart-authoring-from-argument")
    assert response.status_code == 201, response.text
    created = response.json()
    assert created["restart_node_id"] == "build_argument_graph"
    with case.app.state.databases.state.connect() as c:
        assert tuple(c.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()) == before
        assert [tuple(r) for r in c.execute("SELECT * FROM node_runs WHERE run_id=?", (run_id,))
                ] == before_nodes
        new = RunRepository(c).get(created["run_id"])["input"]
        assert new["plan_id"] != original["plan_id"]
        assert new["evidence_context"] == original["evidence_context"]
        assert new["human_question_feedback"] == submitted
        assert "human decisions are not evidence" in new["human_review"]
        assert "claim_scopes" not in new and "proposed_html" not in new
        nodes = c.execute("SELECT node_id,input_json,status FROM node_runs WHERE run_id=?",
                          (created["run_id"],)).fetchall()
        assert {row["node_id"] for row in nodes} == {
            "build_argument_graph", "build_source_preservation_map",
        }
        for row in nodes:
            assert row["status"] == "queued"
            unpacked = PayloadStore(c).unpack(json.loads(row["input_json"]))
            assert unpacked["_join_generations"] == new["_join_generations"]
            assert unpacked["_join_generations"]["plan_manuscript_structure"]

def audit_source(case, tmp_path):
    from fukamiru.revisions.prose_redraft import digest

    run_id, context = source_run(case, tmp_path)
    context['frozen_candidate_v26'] = {'html_sha256': digest(context['proposed_html'])}
    context['prose_redraft_report_v31'] = {
        'target_html_sha256': digest(context['proposed_html']),
        'structure_unchanged': True, 'locked_content_unchanged': True, 'anchors_valid': True,
    }
    context['human_review_items'] = [{'review_item_id': 'preserve', 'reason': '人間確認'}]
    with case.app.state.databases.state.connect() as c:
        c.execute("UPDATE node_runs SET node_id='audit_final_findings',input_json=? WHERE run_id=?",
                  (json.dumps(context), run_id))
        c.execute('UPDATE runs SET output_json=? WHERE id=?',
                  (json.dumps({'proposed_html': context['proposed_html']}), run_id))
        c.commit()
    return run_id, context


def test_final_audit_restart_preserves_frozen_html_and_source_history(case, tmp_path):
    run_id, original = audit_source(case, tmp_path)
    tables = ('runs', 'node_runs', 'event_log', 'run_artifacts', 'llm_result_checkpoints')
    def snapshot(c):
        return {table: [tuple(r) for r in c.execute(
            f'SELECT * FROM {table} WHERE ' + ('id=?' if table == 'runs' else 'run_id=?'),
            (run_id,),
        )] for table in tables}
    with case.app.state.databases.state.connect() as c:
        before = snapshot(c)
    response = case.client.post(f'/api/runs/{run_id}/restart-authoring-from-final-audit')
    assert response.status_code == 201, response.text
    created = response.json()
    with case.app.state.databases.state.connect() as c:
        assert snapshot(c) == before
        new = RunRepository(c).get(created['run_id'])['input']
        assert [(r['node_id'], r['status']) for r in c.execute(
            'SELECT node_id,status FROM node_runs WHERE run_id=?', (created['run_id'],),
        )] == [('audit_final_findings', 'queued')]
    for key in ('proposed_html', 'frozen_candidate_v26', 'human_review_items',
                'prose_redraft_report_v31', 'paragraph_transition_report_v32', 'plan_id'):
        assert new[key] == original[key]
    assert 'authoring_proposal_id' not in new and 'final_path' not in new
    assert new['authoring_tail_restart_lineage']['source_run_id'] == run_id


@pytest.mark.parametrize('problem', ['active', 'approved', 'stale', 'later_html',
                                     'missing_handoff', 'invalid_handoff', 'failed', 'missing'])
def test_final_audit_restart_rejects_unsafe_snapshot(case, tmp_path, problem):
    run_id, context = audit_source(case, tmp_path)
    with case.app.state.databases.state.connect() as c:
        if problem == 'active':
            c.execute("UPDATE runs SET status='running' WHERE id=?", (run_id,))
        elif problem == 'approved':
            RevisionService(c).commit_revision(
                document_id=context['document_id'], canonical_html=context['proposed_html'],
                editor_type='human', operation='create', message='approved',
            )
        elif problem == 'failed':
            c.execute("UPDATE node_runs SET status='failed' WHERE run_id=?", (run_id,))
        elif problem == 'later_html':
            c.execute('UPDATE runs SET output_json=? WHERE id=?',
                      (json.dumps({'proposed_html': 'different'}), run_id))
        else:
            if problem == 'stale':
                context['frozen_candidate_v26']['html_sha256'] = 'stale'
            elif problem == 'missing_handoff':
                context.pop('prose_redraft_report_v31')
            elif problem == 'invalid_handoff':
                context['prose_redraft_report_v31']['anchors_valid'] = False
            else:
                context.pop('research_problem')
            c.execute('UPDATE node_runs SET input_json=? WHERE run_id=?',
                      (json.dumps(context), run_id))
        c.commit()
        before = c.execute('SELECT count(*) FROM runs').fetchone()[0]
    response = case.client.post(f'/api/runs/{run_id}/restart-authoring-from-final-audit')
    assert response.status_code == 409, response.text
    with case.app.state.databases.state.connect() as c:
        assert c.execute('SELECT count(*) FROM runs').fetchone()[0] == before


def test_final_audit_restart_unknown_source_is_404(case):
    response = case.client.post('/api/runs/missing/restart-authoring-from-final-audit')
    assert response.status_code == 404
