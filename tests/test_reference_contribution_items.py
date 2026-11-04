"""A researcher profile may confirm a work only within one listed item."""

from fukamiru.revisions.reference_search import publisher_page
from fukamiru.revisions.reference_web import (
    SearchLead,
    enrich_from_web_v29,
    parse_researcher_contribution_items,
)

PROFILE_URL = "https://researchmap.jp/Koji_Shirae"
MENTION = "架空資料の比較分析"
FULL_TITLE = MENTION + "——『星の記録』における行為と竜"
ITEM_URL = PROFILE_URL + "/misc/47521953"


def _researchmap_item(title: str, path: str, author: str, details: str) -> str:
    return (
        '<li class="list-group-item rm-cv-disclosed">'
        '<div class="rm-cv-list-content">'
        f'<div><a class="rm-cv-list-title" href="{path}">{title}</a>&nbsp;</div>'
        f'<div class="rm-cv-list-author">{author}</div>'
        f'<div>{details}</div>'
        '</div></li>'
    )


def _profile_html() -> str:
    return '<ul class="rm-cv-list-group">' + ''.join((
        _researchmap_item(
            "フレドリック・ジェイムソンの種子 : 『ポストモダニズム』を読むための覚書",
            "/Koji_Shirae/published_papers/47521479", "別の著者",
            "現代思想&nbsp;49(7) 115-123&nbsp;2021年6月",
        ),
        _researchmap_item(
            FULL_TITLE, "/Koji_Shirae/misc/47521953", "山田 花子",
            "架空論集&nbsp;131-176&nbsp;2022年11月",
        ),
        _researchmap_item(
            "侵犯的リアリズムと思考する原形質", "/Koji_Shirae/misc/47521633",
            "別の著者", "Rhetorica&nbsp;72-86&nbsp;2018年11月",
        ),
    )) + '</ul>'


def _resolve_profile(
    document: str, *, items: list[dict] | None = None,
    structured_metadata: dict | None = None,
) -> dict:
    def search(query: str) -> list[SearchLead]:
        return [SearchLead(PROFILE_URL, "山田 花子 | researchmap", FULL_TITLE, 1)] if (
            "site:researchmap.jp" in query
        ) else []

    def fetch(url: str) -> dict:
        assert url == PROFILE_URL
        return {
            "url": url, "status": "retrieved", "title": "山田 花子 | researchmap",
            "structured_metadata": structured_metadata or {},
            "metadata_excerpt": document,
            "contribution_listing": True,
            "contribution_items": (
                parse_researcher_contribution_items(document, url)
                if items is None else items
            ),
        }

    return enrich_from_web_v29(
        {"reference_id": "self-work", "title": MENTION, "author": "自著",
         "is_self_authored": True},
        search=search, fetch=fetch,
    )


def test_researchmap_profile_confirms_only_one_bounded_work() -> None:
    document = _profile_html()
    items = parse_researcher_contribution_items(document, PROFILE_URL)
    assert len(items) == 3
    assert items[1]["title"] == FULL_TITLE
    assert items[1]["source_url"] == ITEM_URL
    assert items[1]["container_title"] == "架空論集"
    assert items[1]["pages"] == "131-176"

    result = _resolve_profile(document)
    candidate = result["candidate_matches"][0]
    assert candidate["title"] == FULL_TITLE
    assert candidate["authors"] == ["山田花子"]
    assert candidate["container_title"] == "架空論集"
    assert candidate["year"] == "2022"
    assert candidate["provider_record_id"] == ITEM_URL
    assert candidate["url"] == ITEM_URL
    assert result["contribution_listing_audit"][0]["matching_item_count"] == 1
    assert "フレドリック" not in candidate["title"]


def test_researchmap_flat_profile_cannot_confirm_a_work() -> None:
    result = _resolve_profile(
        FULL_TITLE + " 山田 花子 架空論集 131-176 2022 "
        "フレドリック・ジェイムソンの種子 現代思想 2021",
        items=[],
        structured_metadata={
            "title": MENTION, "record_type": "article", "authors": ["山田花子"],
            "year": "2022", "publisher": "架空論集",
        },
    )
    assert result["nonstandard_candidate_count"] == 0
    assert result["contribution_listing_audit"][0]["matching_item_count"] == 0


def test_ambiguous_profile_items_cannot_confirm_a_work() -> None:
    document = _profile_html().replace(
        "</ul>",
        _researchmap_item(
            MENTION + "——別の論考", "/Koji_Shirae/misc/47521954",
            "山田 花子", "別の掲載先&nbsp;10-20&nbsp;2023年1月",
        ) + "</ul>",
    )
    result = _resolve_profile(document)
    assert result["nonstandard_candidate_count"] == 0
    assert result["contribution_listing_audit"][0]["matching_item_count"] == 2


def test_publisher_page_retains_researchmap_item_boundaries(monkeypatch) -> None:
    class Response:
        headers = type("Headers", (), {"get_content_charset": lambda self: "utf-8"})()

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self, _size: int) -> bytes:
            return _profile_html().encode()

    class Opener:
        def open(self, _request, *, timeout: float):
            assert timeout == 15
            return Response()

    monkeypatch.setattr(
        "fukamiru.revisions.reference_search.socket.getaddrinfo",
        lambda *_args, **_kwargs: [(None, None, None, None, ("8.8.8.8", 443))],
    )
    monkeypatch.setattr(
        "fukamiru.revisions.reference_search.urllib.request.build_opener",
        lambda *_args: Opener(),
    )
    page = publisher_page(PROFILE_URL)
    assert page["status"] == "retrieved"
    assert page["contribution_listing"] is True
    assert len(page["contribution_items"]) == 3
    assert page["contribution_items"][1]["record_id"] == ITEM_URL
