import pytest

from fukamiru.revisions.reference_relation import resolve_work_relations_v29


@pytest.mark.parametrize(
    "japanese,original",
    [
        ("マルクス主義と文学", "Marxism and Literature"),
        ("ポストモダニズム", "Postmodernism, or, The Cultural Logic of Late Capitalism"),
        ("技術的対象の存在様式について", "Du mode d'existence des objets techniques"),
    ],
)
def test_explicit_original_title_links_independent_records(
    japanese: str, original: str,
) -> None:
    url = "https://ndlsearch.ndl.go.jp/books/translation"
    candidates = [
        {
            "candidate_id": "jp", "title": japanese, "publication_language": "ja",
            "url": url,
            "original_title_audit": [{
                "original_title": original, "source_url": url,
                "parser_rule": "ndl_original_title_section",
            }],
        },
        {
            "candidate_id": "original", "title": original,
            "publication_language": "en", "v29_render_status": "render_ready",
            "authors": ["Example Author"], "url": "https://catalog.example/original",
        },
    ]
    result = resolve_work_relations_v29({"title": japanese}, candidates)
    assert result["relation_resolution_attempt_count"] == 1
    assert result["relation_resolution_success_count"] == 1
    edge = result["work_relation_edges"][0]
    assert edge["type"] == "translation_of"
    assert edge["source"] == url
    assert edge["original_record_source"] == "https://catalog.example/original"
    assert edge["source_mention_matches_translation"] is True


def test_title_similarity_or_unlabelled_hint_cannot_confirm_relation() -> None:
    candidates = [
        {"candidate_id": "jp", "title": "マルクス主義と文学", "publication_language": "ja",
         "url": "https://catalog.example/jp", "original_title": "Marxism and Literature"},
        {"candidate_id": "original", "title": "Marxism and Literature",
         "publication_language": "en", "v29_render_status": "render_ready",
         "authors": ["Raymond Williams"], "url": "https://catalog.example/en"},
    ]
    result = resolve_work_relations_v29({"title": "マルクス主義と文学"}, candidates)
    assert result["relation_resolution_attempt_count"] == 1
    assert result["relation_resolution_success_count"] == 0


def test_relation_does_not_prove_source_mentioned_manifestation() -> None:
    url = "https://ndlsearch.ndl.go.jp/books/translation"
    candidates = [
        {"candidate_id": "jp", "title": "技術的対象の存在様態について",
         "publication_language": "ja", "url": url,
         "original_title_audit": [{"original_title": "Du mode d'existence des objets techniques",
                                    "source_url": url,
                                    "parser_rule": "ndl_original_title_section"}]},
        {"candidate_id": "fr", "title": "Du mode d'existence des objets techniques",
         "publication_language": "fr", "v29_render_status": "render_ready",
         "authors": ["Gilbert Simondon"], "url": "https://catalog.example/fr"},
    ]
    result = resolve_work_relations_v29(
        {"title": "技術的対象の存在様式について"}, candidates
    )
    assert result["relation_resolution_success_count"] == 1
    assert result["work_relation_edges"][0]["source_mention_matches_translation"] is False


def test_labelled_original_title_may_have_nonlatin_note_but_not_latin_subtitle() -> None:
    url = "https://ndlsearch.ndl.go.jp/books/translation"
    japanese = {"candidate_id": "jp", "title": "技術的対象の存在様態について",
                "publication_language": "ja", "url": url,
                "original_title_audit": [{
                    "original_title": "DU MODE D'EXISTENCE DES OBJETS TECHNIQUES 簡訳書での翻訳",
                    "source_url": url, "parser_rule": "ndl_original_title_section",
                }]}
    foreign = {"candidate_id": "fr", "title": "Du mode d'existence des objets techniques",
               "publication_language": "fr", "v29_render_status": "render_ready",
               "authors": ["Gilbert Simondon"], "url": "https://catalog.example/fr"}
    result = resolve_work_relations_v29({}, [japanese, foreign])
    assert result["relation_resolution_success_count"] == 1
    japanese["original_title_audit"][0]["original_title"] = (
        "DU MODE D'EXISTENCE DES OBJETS TECHNIQUES extended edition"
    )
    result = resolve_work_relations_v29({}, [japanese, foreign])
    assert result["relation_resolution_success_count"] == 0
