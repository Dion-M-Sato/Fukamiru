import json
from io import BytesIO

from fukamiru.literature.service import OpenLibraryProvider


def test_open_library_search_fetches_edition_detail(monkeypatch) -> None:
    provider = OpenLibraryProvider()
    monkeypatch.setattr(provider, "_get", lambda _parameters: {
        "docs": [{
            "key": "/works/OL1W", "title": "A Book", "author_name": ["Jane Doe"],
            "editions": {"docs": [{"key": "/books/OL123M", "title": "A Book"}]},
        }],
    })

    class Response(BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            self.close()

    def fake_open(request, *, timeout, context):
        assert request.full_url == "https://openlibrary.org/books/OL123M.json"
        return Response(json.dumps({
            "title": "A Book", "publishers": ["Example Press"],
            "publish_date": "1998", "isbn_10": ["123456789X"],
            "languages": [{"key": "/languages/eng"}],
        }).encode())

    monkeypatch.setattr("fukamiru.literature.service.urllib.request.urlopen", fake_open)
    candidates = provider.search("A Book", limit=2)
    assert len(candidates) == 1
    item = candidates[0]
    assert item.provider_record_id == "OL123M"
    assert item.publisher == "Example Press"
    assert item.publication_date == "1998"
    assert item.provider_payload["isbn"] == "123456789X"
    assert item.provider_payload["detail_record_status"] == "retrieved"


def test_open_library_rejects_unexpected_edition_key(monkeypatch) -> None:
    provider = OpenLibraryProvider()
    monkeypatch.setattr(provider, "_get", lambda _parameters: {
        "docs": [{"editions": {"docs": [{"key": "//evil.example/path"}]}}],
    })
    assert provider.search("A Book", limit=2) == []
