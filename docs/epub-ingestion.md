# EPUB ingestion

Corpus Sync and single-source ingestion accept `.epub` files alongside PDF, TXT,
Markdown and HTML. No additional runtime package is required: extraction uses Python's
ZIP, XML and HTML parsers so installing EPUB support does not change the existing PDF or
RAG dependency set.

## Extraction model

- The EPUB package document and spine determine reading order.
- Each non-empty spine XHTML/HTML resource becomes an ordered `Page`. This is a virtual
  chapter boundary, not a claim about print pagination.
- EPUB 3 navigation labels are preferred for `pages.printed_label`; the first chapter
  heading is the fallback.
- `h1`–`h6`, paragraphs, list items, block quotes, definition terms/descriptions and
  figure captions become role-bearing `SourceSpan` rows. Chunking and lexical/semantic
  indexing then use the existing pipeline unchanged.
- Package title and language populate the top-level `DocumentUnit` when callers leave
  language on `auto`. Content SHA-256 remains the duplicate identity, so byte-identical
  copies are reused even at a different path.
- `head`, navigation, script, style, template and SVG content is excluded from body text.

## Explicit failures and limits

Ingestion fails the individual Corpus Sync item without aborting other items when the
EPUB is corrupt, lacks a package/spine resource, uses an undecodable declared character
encoding, contains unsafe archive paths, has encrypted/DRM-protected spine content, or
contains no extractable body text. An image-only publication reports
`EPUB_IMAGE_ONLY`; EPUB OCR is not currently available.

Archive safety limits are 512 MiB compressed input, 10,000 ZIP entries, 128 MiB per
expanded entry and 1 GiB total expanded content. These fixed conservative limits prevent
oversized input and ZIP bombs from exhausting the ingestion worker. EPUB resources that
are not listed in the spine are not indexed. CSS-generated text, canvas content, MathML
semantics, media overlays, footnote backlinks and nested section hierarchy are not
reconstructed beyond their visible text blocks.

## Real-file smoke test

1. Copy one unprotected EPUB into a folder below the configured
   `managed_sources` directory. Keep the original file unchanged.
2. In Knowledge Base, select that folder and run Corpus Sync.
3. Confirm the item becomes `ready` (or `reused` for a previously ingested byte-identical
   book), then search for one distinctive sentence from an early chapter and one from a
   later chapter.
4. Verify search citations increase in chapter/spine order and that the chapter label is
   present in the stored Page metadata. A direct database check is:

   ```sql
   SELECT p.page_number, p.printed_label, substr(p.text, 1, 120)
   FROM pages p
   JOIN document_units d ON d.id = p.document_unit_id
   JOIN source_assets a ON a.id = d.source_asset_id
   WHERE a.managed_path = 'C:\path\to\book.epub'
   ORDER BY p.page_number;
   ```

5. For a negative smoke, try a DRM-protected or image-only sample and confirm the Corpus
   item is `failed` with an encryption/DRM or `EPUB_IMAGE_ONLY` explanation. Such files
   are not sent into the PDF OCR branch.
