from __future__ import annotations

from copy import deepcopy
from uuid import UUID
from xml.etree import ElementTree as ET

from fukamiru.authoring.render import render_canonical_html
from fukamiru.review_feedback import (
    _answered_wrapped_request,
    question_context,
    question_key,
    question_text,
    review_target,
)


def test_authoring_sentence_alias_resolves_to_the_actual_canonical_paragraph():
    html = render_canonical_html(plan_id="plan-one", sections=[{
        "section_id": "section-body-001", "title": "仮想展示の構成", "article_role": "body",
        "paragraphs": [{"text": "最初の段落。"}, {"text": "対象の段落。別の文も含む。"}],
    }])
    nodes = {node.get("id"): node for node in ET.fromstring(html).iter() if node.get("id")}
    item = {"section_id": "section-body-001", "sentence_id": "section-body-001-P02-S01"}
    before = deepcopy(item)
    resolved = review_target(item, nodes, {})
    assert resolved["target_id"] == "P-plan-one-section-body-001-002"
    assert resolved["requested_target_id"] == item["sentence_id"]
    assert resolved["target"] == "仮想展示の構成"
    assert resolved["excerpt"] == "対象の段落。別の文も含む。"
    assert resolved["excerpt_scope"] == "paragraph"
    assert item == before
    assert question_key(item) == question_key(before)

    # An unknown paragraph must not silently point to some other text.
    missing = review_target({**item, "sentence_id": "section-body-001-P99-S01"}, nodes, {})
    assert missing["excerpt"] == ""
    assert missing["target_id"] == "section-body-001-P99-S01"
    section = review_target({"section_id": "section-body-001"}, nodes, {})
    assert section["excerpt_scope"] == "section"
    assert "最初の段落。" in section["excerpt"]
    exact = review_target({**item, "sentence_id": resolved["target_id"]}, nodes, {})
    assert exact["excerpt"] == resolved["excerpt"]


def test_reader_question_keeps_diagnostics_and_author_claim_separate():
    item = {"category": "required_citation", "reason": (
        f"必須引用 {UUID(int=17)} の対応が未解決: "
        "required source has no citation-to-claim evidence mapping。"
        "著者記録の主張: 展示の配置は観察順序に影響する。"
    )}
    raw = deepcopy(item)
    question = question_text(item)
    assert question.endswith("ありますか？")
    assert str(UUID(int=17)) not in question
    assert "required source" not in question
    assert question_context(item) == "確認したい主張：展示の配置は観察順序に影響する。"
    assert item == raw
    assert "required source" in item["reason"]
    assert question_text({"category": "user_information", "recommended_check":
                          "想定する読者層について、補足できる情報はありますか？"}) == (
        "想定する読者層について、補足できる情報はありますか？"
    )
    assert question_text({"category": "source", "recommended_check":
                          "資料を追加してください？"}).endswith("ありますか？")
    assert question_text({"category": "unresolved.important", "reason":
        "原著の該当箇所、用語、文脈を確認する。"}).endswith("ありますか？")


def test_answered_wrapper_requires_unchanged_requirement_and_original_scope():
    reason = "この箇所を確認できる資料はありますか。"
    answer = {"question_id": "HR-required-s1-source1", "reason": reason}
    question = {"requested_target_id": "原稿全体", "reason": reason}
    context = {"human_revision_decision": {"review_items": [
        {"scope": "s1:source1", "message": reason}]}}
    before = deepcopy(context)
    assert _answered_wrapped_request(question, context, [answer])
    assert context == before
    assert not _answered_wrapped_request(question, context, [{**answer, "reason": "別の要求"}])
    other = {"human_revision_decision": {"review_items": [
        {"scope": "s2:source1", "message": reason}]}}
    assert not _answered_wrapped_request(question, other, [answer])
    assert not _answered_wrapped_request({**question, "requested_target_id": "s2"},
                                         context, [answer])
