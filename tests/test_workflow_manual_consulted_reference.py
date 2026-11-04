from __future__ import annotations

import pytest

from fukamiru.errors import FukamiruError
from fukamiru.literature import LiteratureCandidate
from fukamiru.revisions.references import JapaneseReferenceService


def _candidate() -> LiteratureCandidate:
    return LiteratureCandidate(
        provider="ndl_search",
        provider_record_id="upper-initial",
        title="メタヒストリー",
        authors=("ヘイドン・ホワイト",),
        publication_title=None,
        publisher="作品社",
        publication_date="2017",
        record_type="book",
        doi=None,
        url=None,
        oa_url=None,
        oa_status=None,
        license_url=None,
        provider_payload={
            "isbn": "9780000000001",
            "volume": "上巻",
            "edition": "初版",
            "is_translation": True,
            "translators": ["初版訳者"],
        },
    )


def test_v22_explicit_scope_mismatch_can_be_entered_as_consulted_with_cas() -> None:
    from test_manuscript_rewrite import ManuscriptRewriteTests

    case = ManuscriptRewriteTests()
    case.setUp()
    try:
        entry = case._reference_entry(volume="下巻", edition="改訳版")
        entry["provenance"] = {
            **dict(entry["provenance"]),
            "volume": {"kind": "source_explicit", "label": "原稿に明記"},
            "edition": {"kind": "source_explicit", "label": "原稿に明記"},
        }
        unresolved = JapaneseReferenceService.resolve_with_candidates(
            entry, [_candidate()], workflow_version=22
        )
        proposal = case._persist_v22_reference_proposal(unresolved)

        item = next(
            item
            for item in proposal["context"]["reference_metadata_confirmation_items"]
            if item["reference_id"] == unresolved["reference_id"]
        )
        assert item["confirmation_kind"] == "consulted_manifestation"
        assert item["manual_manifestation_entry"] is True
        assert item["volume"] == "下巻"
        assert item["edition"] == "改訳版"

        updated = case.service.confirm_reference_metadata_v22(
            proposal["id"],
            unresolved["reference_id"],
            proposal["proposed_sha256"],
            is_translation=True,
            translators=["改訳版訳者"],
            metadata={
                "volume": "下巻",
                "edition": "改訳版",
                "original_title": "Metahistory",
                "translated_title": "メタヒストリー",
            },
        )

        confirmed = updated["context"]["reference_entries"][0]
        assert confirmed["consulted_status"] == "user_confirmed"
        assert confirmed["resolution_status"] == "manifestation_confirmed"
        assert confirmed["work_identity_status"] == "user_confirmed_manual_manifestation"
        assert confirmed["edition_status"] == "user_entered_consulted_manifestation"
        assert confirmed["volume"] == "下巻"
        assert confirmed["edition"] == "改訳版"
        assert confirmed["translators"] == ["改訳版訳者"]
        assert confirmed["final_revision_blocked"] is False
        assert updated["context"]["reference_metadata_confirmation_items"] == []
        assert updated["context"]["reference_approval"]["final_revision_blocked"] is False
        history = updated["context"]["reference_metadata_confirmation_history"][-1]
        assert history["reference_kind"] == "consulted_manifestation"
        assert history["classification_change"] is None

        with pytest.raises(FukamiruError, match="Proposal changed"):
            case.service.confirm_reference_metadata_v22(
                proposal["id"],
                unresolved["reference_id"],
                proposal["proposed_sha256"],
                is_translation=True,
                translators=["改訳版訳者"],
            )
    finally:
        case.tearDown()
