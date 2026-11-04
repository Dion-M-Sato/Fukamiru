from __future__ import annotations

import hashlib
import io
import tempfile
import unittest
from email.message import Message
from pathlib import Path

from fukamiru.literature import (
    CrossrefProvider,
    LiteratureCandidate,
    LiteratureDiscoveryService,
    OAAcquisitionService,
    OpenAlexProvider,
    SemanticScholarProvider,
)
from fukamiru.storage.database import SQLiteDatabase


class FakeProvider:
    def __init__(self, name: str, candidate: LiteratureCandidate) -> None:
        self.name = name
        self.candidate = candidate

    def search(self, query: str, *, limit: int) -> list[LiteratureCandidate]:
        del query, limit
        return [self.candidate]


class FakePDFResponse(io.BytesIO):
    def __init__(self, payload: bytes, url: str = "https://example.org/article.pdf") -> None:
        super().__init__(payload)
        self._url = url
        self.headers = Message()
        self.headers["Content-Type"] = "application/pdf"

    def geturl(self) -> str:
        return self._url

    def __enter__(self) -> FakePDFResponse:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()


class LiteratureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        migrations = Path(__file__).resolve().parents[1] / "migrations" / "knowledge"
        self.database = SQLiteDatabase(Path(self.temp.name) / "knowledge.db", migrations)
        self.database.migrate()
        self.connection = self.database.connect()

    def tearDown(self) -> None:
        self.connection.close()
        self.temp.cleanup()

    def test_candidates_require_review_and_preserve_provider_license(self) -> None:
        openalex_candidate = LiteratureCandidate(
            provider="openalex",
            provider_record_id="https://openalex.org/W1",
            title="Translation Studies",
            authors=("Author One",),
            publication_title="Journal",
            publisher="Publisher",
            publication_date="2026-01-02",
            record_type="article",
            doi="10.1234/example",
            url="https://openalex.org/W1",
            oa_url="https://example.org/article.pdf",
            oa_status="gold",
            license_url="https://creativecommons.org/licenses/by/4.0/",
            provider_payload={"id": "W1"},
        )
        service = LiteratureDiscoveryService(
            self.connection,
            providers={"openalex": FakeProvider("openalex", openalex_candidate)},
        )

        search = service.search(provider="openalex", query="translation", limit=5)
        candidate = search["candidates"][0]
        self.assertEqual(candidate["review_status"], "pending")
        self.assertEqual(
            self.connection.execute("SELECT COUNT(*) FROM bibliographic_records").fetchone()[0],
            0,
        )

        approved = service.review(candidate["id"], decision="approve")
        provenance = self.connection.execute(
            "SELECT * FROM bibliographic_provenance WHERE bibliographic_record_id = ?",
            (approved["bibliographic_record_id"],),
        ).fetchone()
        self.assertEqual(approved["review_status"], "approved")
        self.assertEqual(provenance["provider"], "openalex")
        self.assertEqual(
            provenance["license_url"], "https://creativecommons.org/licenses/by/4.0/"
        )

    def test_provider_payloads_are_normalized(self) -> None:
        openalex = OpenAlexProvider()._candidate(
            {
                "id": "https://openalex.org/W2",
                "doi": "https://doi.org/10.1000/XYZ",
                "display_name": "Open work",
                "publication_year": 2025,
                "type": "article",
                "authorships": [{"author": {"display_name": "Ada Lovelace"}}],
                "primary_location": {"source": {"display_name": "Open Journal"}},
                "open_access": {"oa_status": "green"},
                "best_oa_location": {
                    "pdf_url": "https://example.org/open.pdf",
                    "license": "cc-by",
                },
            }
        )
        crossref = CrossrefProvider()._candidate(
            {
                "DOI": "10.1000/XYZ",
                "title": ["<i>Crossref</i> work"],
                "author": [{"given": "Ada", "family": "Lovelace"}],
                "published": {"date-parts": [[2025, 2, 3]]},
                "container-title": ["Crossref Journal"],
                "type": "journal-article",
                "URL": "https://doi.org/10.1000/XYZ",
                "license": [{"URL": "https://creativecommons.org/licenses/by/4.0/"}],
                "link": [
                    {
                        "URL": "https://example.org/work.pdf",
                        "content-type": "application/pdf",
                    }
                ],
            }
        )
        self.assertEqual(openalex.doi, "10.1000/xyz")
        self.assertEqual(openalex.authors, ("Ada Lovelace",))
        self.assertEqual(crossref.publication_date, "2025-2-3")
        self.assertEqual(crossref.title, "Crossref work")
        self.assertEqual(crossref.oa_url, "https://example.org/work.pdf")

        semantic_scholar = SemanticScholarProvider(api_key="test-key")
        semantic = semantic_scholar._candidate(
            {
                "paperId": "paper-1",
                "title": "Semantic work",
                "authors": [{"name": "Ada Lovelace"}],
                "venue": "Semantic Journal",
                "publicationDate": "2025-02-03",
                "publicationTypes": ["JournalArticle"],
                "externalIds": {"DOI": "https://doi.org/10.1000/XYZ"},
                "url": "https://www.semanticscholar.org/paper/paper-1",
                "openAccessPdf": {
                    "url": "https://example.org/semantic.pdf",
                    "status": "GREEN",
                    "license": "CCBY",
                },
            }
        )
        self.assertEqual(semantic.provider_record_id, "paper-1")
        self.assertEqual(semantic.doi, "10.1000/xyz")
        self.assertEqual(semantic.oa_url, "https://example.org/semantic.pdf")
        self.assertEqual(semantic_scholar._headers()["x-api-key"], "test-key")

    def test_approved_oa_pdf_is_staged_hashed_and_published(self) -> None:
        candidate = LiteratureCandidate(
            provider="openalex",
            provider_record_id="W-OA",
            title="Open paper",
            authors=("Author",),
            publication_title="Journal",
            publisher=None,
            publication_date="2026",
            record_type="article",
            doi="10.1/open",
            url="https://openalex.org/W-OA",
            oa_url="https://example.org/article.pdf",
            oa_status="gold",
            license_url="https://creativecommons.org/licenses/by/4.0/",
            provider_payload={"id": "W-OA"},
        )
        discovery = LiteratureDiscoveryService(
            self.connection,
            providers={"openalex": FakeProvider("openalex", candidate)},
        )
        pending = discovery.search(provider="openalex", query="open", limit=1)[
            "candidates"
        ][0]
        with self.assertRaisesRegex(Exception, "approved Candidate"):
            OAAcquisitionService(
                self.connection,
                corpus_root=Path(self.temp.name) / "corpus",
            ).acquire(pending["id"])
        discovery.review(pending["id"], decision="approve")
        payload = b"%PDF-1.7\nfixture"

        acquired = OAAcquisitionService(
            self.connection,
            corpus_root=Path(self.temp.name) / "corpus",
            opener=lambda *args, **kwargs: FakePDFResponse(payload),
        ).acquire(pending["id"])

        self.assertEqual(acquired["status"], "completed")
        self.assertEqual(acquired["content_sha256"], hashlib.sha256(payload).hexdigest())
        self.assertEqual(acquired["byte_size"], len(payload))
        self.assertTrue(Path(acquired["stored_path"]).is_file())
        self.assertEqual(Path(acquired["stored_path"]).read_bytes(), payload)
        row = self.connection.execute(
            "SELECT review_status FROM literature_candidates WHERE id = ?", (pending["id"],)
        ).fetchone()
        self.assertEqual(row["review_status"], "acquired")
        self.assertEqual(
            acquired["license_url"], "https://creativecommons.org/licenses/by/4.0/"
        )
        repeated = OAAcquisitionService(
            self.connection,
            corpus_root=Path(self.temp.name) / "corpus",
            opener=lambda *args, **kwargs: self.fail("download repeated"),
        ).acquire(pending["id"])
        self.assertEqual(repeated["id"], acquired["id"])


if __name__ == "__main__":
    unittest.main()
