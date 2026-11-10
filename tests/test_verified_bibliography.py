from copy import deepcopy

import pytest

from fukamiru.errors import FukamiruError
from fukamiru.revisions.bibliography_footnote_style import format_footnote_reference
from fukamiru.revisions.bibliography_style import format_reference
from fukamiru.revisions.verified_bibliography import verified_book_reference


def test_verified_book_repair_preserves_source_identity_and_requires_matching_primary_metadata():
    reference = {"reference_id": "ref-one", "provider": "knowledge_base",
                 "provider_record_id": "asset-one", "record_type": "document",
                 "title": "検証太郎_検証の本 (検証出版, 2016)", "authors": [],
                 "bibliography_people": [], "verification_status": "unverified"}
    correction = {"reference_id": "ref-one", "title": "検証の本", "authors": ["検証太郎"],
                  "translators": [], "publisher": "検証出版", "year": "2016",
                  "isbn": "9780306406157", "publication_language": "ja",
                  "source_kind": "publisher", "source_url": "https://publisher.example/book"}
    original = deepcopy(reference)
    fixed = verified_book_reference(reference, correction, checked_at="now")
    assert reference == original
    assert fixed["provider_record_id"] == original["provider_record_id"]
    assert fixed["source_import_title"] == original["title"]
    assert fixed["verification_status"] == "unverified"
    assert fixed["provenance"]["authors"]["source_url"] == correction["source_url"]
    assert format_footnote_reference(fixed).supported
    assert format_reference(fixed).supported
    for invalid in ({"title": "別の本"}, {"isbn": "9780306406158"},
                    {"source_url": "http://publisher.example/book"},
                    {"verification_status": "verified"}):
        with pytest.raises(FukamiruError):
            verified_book_reference(reference, {**correction, **invalid}, checked_at="now")
    assert reference == original
