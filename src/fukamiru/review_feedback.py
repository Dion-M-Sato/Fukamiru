"""Review questions and author decisions, kept separate from source evidence."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import xml.etree.ElementTree as ET
from contextlib import suppress
from pathlib import Path
from typing import Any

from fukamiru.errors import FukamiruError
from fukamiru.storage.payloads import PayloadStore
from fukamiru.storage.runs import utc_now

FEEDBACK_POLICY = (
    "人間の回答と全体コメントを生成・修正・再監査に反映してください。"
    "llm_overreach=true は、その要求を人間が過剰と判断した記録です。"
    "同じ対象・同じ要求を繰り返さず、必要なら人間の解釈として扱ってください。"
    "これは外部の主張を支持する資料・出典・検証済み事実ではありません。"
    "回答だけでsource_idsやverification_statusを検証済みに変更してはいけません。"
    "未知の出典IDや壊れた参照など、構造上の不整合は引き続き検査してください。"
)


def question_key(item: dict[str, Any]) -> str:
    """Ignore changing audit IDs, but never waive a different target or requirement."""
    identity = [
        str(item.get(key) or "").strip()
        for key in ("section_id", "sentence_id", "claim_id", "category", "reason")
    ]
    return hashlib.sha256(json.dumps(identity, ensure_ascii=False).encode()).hexdigest()


def _information_request(category: str, reason: str) -> bool:
    return any(word in category for word in (
        "source", "citation", "evidence", "reference", "information", "bibliograph", "research",
    )) or (category == "unresolved.important" and bool(re.search(
        r"(?:原著|参照資料|該当(?:箇所|ページ)|出典).{0,100}確認", reason)))


def question_text(item: dict[str, Any]) -> str:
    reason = str(item.get("reason") or item.get("message") or "").strip()
    check = str(item.get("recommended_check") or "").strip()
    category = str(item.get("category") or "")
    information = _information_request(category, reason)
    if (check.endswith(("？", "?")) and _reader_facing(check)
            and (not information or re.search(r"ありますか[？?]$", check))):
        return check
    if "bibliograph" in category:
        return "この文献の著者・書名・版・出版年を確認できる情報はありますか？"
    if any(word in category for word in ("citation", "reference")):
        return "この箇所に引用する資料や、参照したページの情報はありますか？"
    if information:
        return "この箇所の内容や解釈を確認できる資料・該当ページ・補足情報はありますか？"
    subject = reason if _reader_facing(reason) and len(reason) <= 160 else "この箇所の解釈や表現"
    return f"「{subject}」について、どのように扱うのが適切だと考えますか？"


def _reader_facing(text: str) -> bool:
    return bool(re.search(r"[ぁ-んァ-ン一-龥]", text)) and not re.search(
        r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}"
        r"|\b[A-Za-z]+_[A-Za-z_]+\b|\b(?:SRC|SEC|HRI|HR|AF)-[A-Za-z0-9-]+"
        r"|[A-Za-z]{3,}\s+[A-Za-z]{3,}\s+[A-Za-z]{2,}", text,
    )


def question_context(item: dict[str, Any]) -> str:
    """Keep a readable reason next to the question; retain raw diagnostics separately."""
    reason = str(item.get("reason") or item.get("message") or "").strip()
    claim = re.search(r"著者記録の主張[:：]\s*(.+)", reason, re.DOTALL)
    if claim and _reader_facing(claim[1]):
        return "確認したい主張：" + claim[1].strip()
    reason = reason.replace(
        "固定出典が特定できないため、証拠としての確定を保留した:",
        "この箇所の原典が未確認です。",
    )
    if _reader_facing(reason):
        return reason
    category = str(item.get("category") or "")
    if any(word in category for word in ("source", "citation", "evidence", "reference")):
        return "この箇所の主張と、参照資料との対応を確認したいという指摘です。"
    return "この箇所の解釈や表現について、著者の判断を求める指摘です。"


def review_target(item: dict[str, Any], nodes: dict[str, ET.Element],
                  titles: dict[str, str]) -> dict[str, str]:
    """Resolve authoring sentence aliases to real canonical paragraphs, without guessing."""
    section_id = str(item.get("section_id") or "")
    requested = str(item.get("sentence_id") or section_id)
    node = nodes.get(requested)
    scope = "exact" if node is not None else ""
    sentence = re.fullmatch(r"(.+)-P(\d+)(?:-S\d+)?", requested)
    if sentence:
        section_id = sentence[1]
    section_key = re.sub(r"[^A-Za-z0-9_.:-]+", "-", section_id).strip("-")
    sections = [element for key, element in nodes.items()
                if element.tag.rsplit("}", 1)[-1] == "section"
                and (key == section_id or (section_key and key.endswith("-" + section_key)))]
    section = sections[0] if len(sections) == 1 else None
    if node is not None and section is None:
        parents = [element for element in nodes.values()
                   if element.tag.rsplit("}", 1)[-1] == "section"
                   and any(child is node for child in element.iter())]
        section = parents[0] if len(parents) == 1 else None
    if node is not None and node.tag.rsplit("}", 1)[-1] in {"p", "section"}:
        scope = "paragraph" if node.tag.rsplit("}", 1)[-1] == "p" else "section"
    if node is None and sentence and section is not None:
        paragraphs = [element for element in section.iter()
                      if element.tag.rsplit("}", 1)[-1] == "p"]
        index = int(sentence[2]) - 1
        if 0 <= index < len(paragraphs):
            node, scope = paragraphs[index], "paragraph"
    elif node is None and section is not None:
        node, scope = section, "section"
    title = titles.get(section_id) or "原稿内の該当箇所"
    if section is not None:
        heading = next((element for element in section.iter()
                        if element.tag.rsplit("}", 1)[-1] in {"h1", "h2", "h3"}), None)
        if heading is not None:
            title = "".join(heading.itertext()).strip() or title
    excerpt = "".join(node.itertext()).strip() if node is not None else ""
    if len(excerpt) > 1200:
        excerpt = excerpt[:1200] + "…"
    return {"target": title, "target_id": node.get("id", requested) if node is not None
            else requested, "requested_target_id": requested,
            "excerpt": excerpt, "excerpt_scope": scope}


def _planning_target(target: dict[str, str], context: dict[str, Any]) -> dict[str, str]:
    if target["excerpt"]:
        return target
    requested = target["requested_target_id"]
    for field, label in (("propositions", "元メモの整理内容"),
                         ("claims", "検討対象の主張"), ("argument_nodes", "論証の検討内容")):
        matches = [raw for raw in context.get(field) or []
                   if isinstance(raw, dict) and str(raw.get("id") or "") == requested]
        if len(matches) == 1 and matches[0].get("text"):
            text = str(matches[0]["text"])
            return {**target, "target": label, "excerpt_scope": "planning",
                    "excerpt": text[:1200] + ("…" if len(text) > 1200 else "")}
    return target


def _duplicate_information_request(scope: str, reason: str, items: list[dict]) -> bool:
    if not scope:
        return False
    for item in items:
        if str(item.get("reason") or "") != reason:
            continue
        ids = {str(item.get(key) or "") for key in ("section_id", "sentence_id", "claim_id")}
        ids.update(str(value) for value in item.get("source_ids") or [])
        review_id = str(item.get("review_item_id") or item.get("question_id") or "")
        if scope in ids or review_id.endswith("-" + scope.replace(":", "-")):
            return True
    return False


def _information_scope(raw: dict) -> str:
    return str(raw.get("section_id") or raw.get("object_id") or raw.get("affected_scope")
               or raw.get("scope") or "")


def _answered_wrapped_request(question: dict, context: dict, answers: list[dict]) -> bool:
    """Recover a lost wrapper scope using the unchanged requirement and source item."""
    if question.get("requested_target_id", question.get("target")) != "原稿全体":
        return False
    reason = str(question.get("reason") or "")
    for raw in [*(context.get("required_user_information") or []),
                *((context.get("human_revision_decision") or {}).get("review_items") or [])]:
        if not isinstance(raw, dict):
            continue
        original = (raw.get("reason") or raw.get("message") or raw.get("description")
                    or raw.get("question"))
        if original == reason and _duplicate_information_request(
            _information_scope(raw), reason, answers,
        ):
            return True
    return False


def feedback_context(connection: sqlite3.Connection, run_id: str) -> dict[str, Any]:
    records = []
    for row in connection.execute(
        "SELECT * FROM review_feedback WHERE run_id=? AND submitted_revision IS NOT NULL "
        "ORDER BY submitted_at, review_id", (run_id,),
    ):
        by_id = {item["question_id"]: item for item in json.loads(row["questions_json"])}
        for answer in json.loads(row["submitted_answers_json"]):
            question = by_id[answer["question_id"]]
            records.append({**question, **answer, "review_id": row["review_id"]})
    comments = [dict(row) for row in connection.execute(
        "SELECT review_id, submitted_comment AS comment FROM review_feedback "
        "WHERE run_id=? AND submitted_revision IS NOT NULL AND submitted_comment != '' "
        "ORDER BY submitted_at, review_id", (run_id,),
    )]
    return {"policy": FEEDBACK_POLICY, "answers": records, "overall_comments": comments}


def apply_feedback(context: dict[str, Any], feedback: dict[str, Any]) -> dict[str, Any]:
    decisions = {item["key"]: item for item in feedback.get("answers", [])}
    items = []
    for raw in context.get("human_review_items") or []:
        item = dict(raw)
        answer = decisions.get(question_key(item))
        if answer:
            item["status"] = "dismissed" if answer["llm_overreach"] else "resolved"
        items.append(item)
    updated = {**context, "human_review_items": items, "human_question_feedback": feedback}
    if feedback.get("answers") or feedback.get("overall_comments"):
        # The existing proofreading and audit adapters consume human_review.
        # Keep its original text once, rather than appending the same decisions
        # again at each workflow node or turning them into source evidence.
        base = str(context.get("_human_question_feedback_base_review",
                               context.get("human_review") or ""))
        updated["_human_question_feedback_base_review"] = base
        updated["human_review"] = (
            base + "\n人間のレビュー判断（外部資料の根拠ではありません）:\n"
            + json.dumps(feedback, ensure_ascii=False, sort_keys=True)
        ).strip()
    return updated


class ReviewFeedbackService:
    def __init__(self, connection: sqlite3.Connection):
        self.connection = connection

    def _source(self, review_id: str) -> tuple[dict, dict]:
        review = self.connection.execute(
            "SELECT * FROM review_queue_entries WHERE id=?", (review_id,),
        ).fetchone()
        # Edited reviews leave the open/approved/rejected projection, but their
        # questions, original preview and submitted answers remain readable.
        if review is None:
            review = self.connection.execute(
                "SELECT * FROM human_reviews WHERE id=?", (review_id,),
            ).fetchone()
        if review is None:
            raise FukamiruError("Review not found")
        review = dict(review)
        row = self.connection.execute(
            "SELECT output_json FROM node_runs WHERE id=?", (review.get("node_run_id"),),
        ).fetchone()
        if row is None:
            row = self.connection.execute(
                "SELECT output_json FROM runs WHERE id=?", (review["run_id"],),
            ).fetchone()
        context = PayloadStore(self.connection).unpack(json.loads(row[0] or "{}")) if row else {}
        return review, context

    def _packet(self, review: dict, context: dict) -> tuple[list[dict], str]:
        proposal = None
        if review["object_type"] == "manuscript_rewrite_proposal":
            from fukamiru.revisions.rewrite import ManuscriptRewriteService

            proposal = ManuscriptRewriteService(self.connection).get(review["object_id"])
            context = {**context, **dict(proposal.get("context") or {}),
                       "proposed_html": proposal["proposed_html"]}
        previous_feedback = feedback_context(self.connection, review["run_id"])
        context = apply_feedback(context, previous_feedback)
        items = [dict(item) for item in context.get("human_review_items") or []
                 if isinstance(item, dict)]
        if review["object_type"] == "authoring_graph_invalid_reference":
            packet = context.get("argument_graph_human_review") or {}
            items = [{"reason": str(problem), "category": "graph_reference",
                      "section_id": "論証の対応関係"}
                     for problem in packet.get("problems") or []]
        elif review["object_type"] == "authoring_draft_source_unresolved":
            items = [{"review_item_id": node["id"], "reason": node.get("text", ""),
                      "category": "draft_source", "section_id": "、".join(
                          node.get("section_ids") or [])}
                     for node in (context.get("authoring_draft_source_review") or {}).get(
                         "evidence_nodes", [])]
        elif review["object_type"] == "authoring_plan":
            items = [{**item, "reason": item.get("message") or item.get("description"),
                      "category": item.get("code") or "structure"}
                     for item in context.get("audit_findings") or [] if isinstance(item, dict)]
        elif proposal is not None:
            items = [{"review_item_id": raw.get("review_id"),
                      "section_id": "、".join(raw.get("block_ids") or []),
                      "category": "semantic_interpretation",
                      "reason": raw.get("explanation") or "この箇所の意味の取り方"}
                     for raw in context.get("semantic_review_items") or []]
        # The latest footnoted audit supersedes an older coherence audit, even
        # when its unresolved list is empty. Do not resurrect resolved issues.
        coherence = context.get("footnoted_coherence_unresolved_routes",
                                context.get("coherence_unresolved_routes") or [])
        for raw in coherence or []:
            if not isinstance(raw, dict) or raw.get("resolution_route") not in {
                "human_review", "additional_research",
            }:
                continue
            reason = raw.get("explanation") or raw.get("message")
            if not reason:
                continue
            targets = list(raw.get("block_ids") or [])
            items.append({
                **raw, "review_item_id": "coherence:" + str(raw.get("id") or question_key(raw)),
                "reason": str(reason), "sentence_id": next(iter(targets), None),
                "section_id": str(raw.get("section_id") or "原稿全体"),
                "category": ("coherence_information" if raw["resolution_route"]
                             == "additional_research" else "coherence_interpretation"),
            })
        for raw in [*(context.get("required_user_information") or []),
                    *((context.get("human_revision_decision") or {}).get("review_items") or [])]:
            if not isinstance(raw, dict):
                raw = {"question": str(raw)}
            reason = raw.get("reason") or raw.get("message") or raw.get("description")
            reason = reason or raw.get("question")
            if not reason:
                continue
            scope = _information_scope(raw)
            if _duplicate_information_request(
                scope, str(reason), [*items, *previous_feedback["answers"]],
            ):
                continue
            items.append({
                **raw, "reason": str(reason),
                "category": raw.get("category") or raw.get("code") or "user_information",
                "section_id": scope or "原稿全体",
                "recommended_check": raw.get("question") or raw.get("recommended_check") or "",
            })
        if not items:
            items = [{"reason": review["question"], "category": "overall",
                      "section_id": "原稿全体"}]
        seen = set()
        answered_keys = {answer["key"] for answer in previous_feedback["answers"]}
        questions = []
        for item in items:
            if item.get("status") in {"resolved", "dismissed"}:
                continue
            key = question_key(item)
            if key in seen or key in answered_keys:
                continue
            seen.add(key)
            question_id = str(item.get("review_item_id") or "Q-" + key[:20])
            if any(question["question_id"] == question_id for question in questions):
                question_id += "-" + key[:12]
            questions.append({
                "question_id": question_id,
                "presentation_version": 2, "category": str(item.get("category") or ""),
                "key": key, "question": question_text(item),
                "target": str(item.get("sentence_id") or item.get("section_id") or "原稿全体"),
                "reason": str(item.get("reason") or ""),
                "context_note": question_context(item),
                "source_ids": list(item.get("source_ids") or []),
            })
        preview = str(context.get("proposed_html") or context.get("canonical_html") or "")
        if review["object_type"] in {"run_artifact", "authoring_submission"}:
            artifact = self.connection.execute(
                "SELECT path,media_type FROM run_artifacts WHERE id=?", (review["object_id"],),
            ).fetchone()
            if artifact and artifact["media_type"] == "text/html":
                with suppress(OSError):
                    preview = Path(artifact["path"]).read_text(encoding="utf-8")
        titles = {str(item.get("section_id")): str(item.get("title") or "")
                  for item in context.get("section_contracts") or [] if isinstance(item, dict)}
        nodes = {}
        with suppress(ET.ParseError):
            nodes = {node.get("id"): node for node in ET.fromstring(preview).iter()
                     if node.get("id")}
        items_by_key = {question_key(item): item for item in items}
        for question in questions:
            item = items_by_key[question["key"]]
            question.update(_planning_target(review_target(item, nodes, titles), context))
        return questions, preview

    def get(self, review_id: str) -> dict[str, Any]:
        review, context = self._source(review_id)
        row = self.connection.execute(
            "SELECT * FROM review_feedback WHERE review_id=?", (review_id,),
        ).fetchone()
        if row is None:
            questions, preview = self._packet(review, context)
            self.connection.execute(
                "INSERT OR IGNORE INTO review_feedback("
                "review_id,run_id,questions_json,preview_html,updated_at) VALUES(?,?,?,?,?)",
                (review_id, review["run_id"], json.dumps(questions, ensure_ascii=False),
                 preview, utc_now()),
            )
            row = self.connection.execute(
                "SELECT * FROM review_feedback WHERE review_id=?", (review_id,),
            ).fetchone()
        submitted = feedback_context(self.connection, review["run_id"])["answers"]
        previous = {item["key"]: item for item in submitted}
        questions = json.loads(row["questions_json"])
        if review["status"] == "open":
            retained_ids = {answer["question_id"] for answer in json.loads(row["answers_json"])
                            if answer.get("answer") or answer.get("llm_overreach")}
            earlier_answers = [answer for answer in submitted if answer["review_id"] != review_id]
            questions = [question for question in questions if (
                question["question_id"] in retained_ids
                or not _answered_wrapped_request(question, context, earlier_answers))]
        nodes: dict[str, ET.Element] = {}
        with suppress(ET.ParseError):
            nodes = {node.get("id"): node for node in ET.fromstring(row["preview_html"]).iter()
                     if node.get("id")}
        for question in questions:
            if question.get("presentation_version") != 2:
                # Upgrade presentation in memory only. The original snapshot,
                # question identities, answers and optimistic revision stay intact.
                original = str(question["question"])
                diagnostic = str(question.get("reason") or "")
                category = str(question.get("category") or (
                    "source" if any(word in original + diagnostic for word in (
                        "資料", "出典", "引用", "source", "citation", "evidence",
                    )) else "interpretation"
                ))
                item = {"reason": diagnostic, "category": category,
                        "recommended_check": original if len(original) <= 240 else ""}
                alias = str(question.get("requested_target_id") or question.get("target_id") or "")
                target = review_target({"section_id": alias, "sentence_id": alias}, nodes, {})
                target = _planning_target(target, context)
                if target["target"] == "原稿内の該当箇所":
                    target["target"] = str(question.get("target") or target["target"])
                question.update({**target, "recorded_question": original,
                                 "question": question_text(item),
                                 "context_note": question_context(item),
                                 "presentation_version": 2})
            question["previous_answer"] = previous.get(question["key"])
            if (_information_request(str(question.get("category") or ""),
                                     str(question.get("reason") or ""))
                    and not re.search(r"ありますか[？?]$", question["question"])):
                question.setdefault("recorded_question", question["question"])
                question["question"] = question_text(question)
        return {
            "review_id": review_id, "run_id": review["run_id"], "status": review["status"],
            "questions": questions, "preview_html": row["preview_html"],
            "answers": json.loads(row["answers_json"]),
            "overall_comment": row["overall_comment"], "revision": row["revision"],
            "submitted_revision": row["submitted_revision"],
            "submitted_at": row["submitted_at"],
        }

    def save(self, review_id: str, *, revision: int, answers: list[dict],
             overall_comment: str, submit: bool = False) -> dict[str, Any]:
        packet = self.get(review_id)
        if packet["status"] != "open":
            raise FukamiruError("Resolved review feedback is read-only")
        snapshot = self.connection.execute(
            "SELECT questions_json FROM review_feedback WHERE review_id=?", (review_id,),
        ).fetchone()
        expected = {item["question_id"] for item in json.loads(snapshot[0])}
        ids = [answer.get("question_id") for answer in answers]
        if len(ids) != len(set(ids)) or not set(ids) <= expected:
            raise FukamiruError("Unknown or duplicate question ID")
        cleaned = []
        for answer in answers:
            text = str(answer.get("answer") or "").strip()
            overreach = answer.get("llm_overreach", False)
            if not isinstance(overreach, bool) or len(text) > 10000:
                raise FukamiruError("Invalid answer")
            if text or overreach:
                cleaned.append({"question_id": answer["question_id"], "answer": text,
                                "llm_overreach": overreach})
        if submit and not cleaned and not overall_comment.strip():
            raise FukamiruError("回答、過剰要求の選択、全体コメントのいずれかを入力してください。")
        encoded = json.dumps(cleaned, ensure_ascii=False)
        now = utc_now()
        changed = self.connection.execute(
            "UPDATE review_feedback SET answers_json=?,overall_comment=?,revision=revision+1,"
            "updated_at=? WHERE review_id=? AND revision=?",
            (encoded, overall_comment, now, review_id, revision),
        ).rowcount
        if not changed:
            raise FukamiruError("別の画面で回答が更新されました。再読み込みして確認してください。")
        if submit:
            self.connection.execute(
                "UPDATE review_feedback SET submitted_revision=revision,submitted_answers_json=?,"
                "submitted_comment=?,submitted_at=? WHERE review_id=?",
                (encoded, overall_comment, now, review_id),
            )
        return self.get(review_id)
