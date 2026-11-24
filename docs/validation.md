# Validation

This file records reproducible checks of the distributed source. Test data is fictional;
validation must not depend on a personal database, manuscript or previous run.

## Required checks

```powershell
.venv/Scripts/python.exe -m ruff check .
.venv/Scripts/python.exe -m pytest
npm --prefix ui run check
python scripts/check_publication.py
git diff --check
```

Workflow data and the UI-linked diagrams are generated from executable definitions.
Check their synchronization with the diagram generation scripts in `scripts/`.
Use isolated databases and ports for integration checks. Never apply test migrations
to a daily-use database or use its records as fixtures.

## Results

- Python suite: 1931 passed, no failures or skips.
- Identity, source-preservation and HTML API contracts: 280 passed, no failures or skips.
- UI syntax and unit checks: 56 passed, no failures or skips.
- Ruff and Git whitespace checks: passed.
- Executable workflow graph and UI-linked diagram synchronization: passed.
- Publication guard: passed, including 12 rejected private-file cases and 4 allowed source cases.

The installed HTTP test dependency emits a deprecation warning. It does not affect test results.
External providers are simulated with fictional responses or a loopback HTTP server.
Live external inference, search, OCR services and local model quality are not certified by these checks.
HTML structure, shared styles, citation formatting and unchanged canonical content are checked;
visual comparison in a real browser remains manual.

## RustXNG v0.30.0 Standard

- The public-source client returned four HTTPS candidates for a public example query
  through a loopback RustXNG v0.30.0 service running with `--profile standard`.
- The workflow diagram generator reproduced the checked-in data for the UI-linked diagram.
- The Python suite passed all 1,931 tests. All 56 UI tests passed, along with Ruff,
  the Git whitespace check, and the publication guard.

## Review workspace and node generation management (2026-11-10)

The review workspace keeps selection, overall comments and decisions in the center,
with per-question answers and isolated HTML in the right pane. Submitted answers and
LLM-overreach judgments are retained as review history and carried into later writing
and audit requests. Optimistic revisions protect concurrent edits and late valid drafts.

Human-requested regeneration starts a new candidate generation while retaining fixed
arguments, protected human text and author answers. Proofreading candidates and logs
are scoped to that generation. Citation identity and source capabilities remain checked.
Limited recovery paths preserve failed inputs, prior results and bibliographic provenance.
Existing migrations are unchanged; migrations 0053 through 0055 add the required state.

The distributed source keeps its HTML output, module names and independent fictional
examples. No personal manuscripts, live Run records, databases, local configuration or
operational logs are part of this change. External inference and live providers were not
used for these checks.

- Python suite: 1,997 passed, no failures or skips (849.33 seconds).
- Focused generation, review, migration, bibliography and diagram tests: 84 passed.
- Fictional excerpt and readable-question regressions after fixture editing: 7 passed.
- UI syntax and unit checks: 58 passed.
- Headless Edge: review switching, autosave, checkbox-only submission, reload, conflict
  recovery, HTML isolation, enlarged HTML and keyboard tabs passed. Desktop and 390px
  mobile screenshots were inspected; the return action and layout were correct.
  No external preview requests or JavaScript errors occurred.
- Ruff, workflow graph generation check, diagram JavaScript syntax and Git whitespace:
  passed. The UI-linked current graph has 71 nodes and 87 edges.
- Publication guard: 449 tracked source files passed, including the staged new files.

The pytest configuration was explicitly checked to import the API and node repository
from this public checkout rather than the shared environment's editable installation.
The installed HTTP test dependency continues to emit its existing deprecation warning.

## 0.33.0 — section proofreading and saved research reports (2026-11-21)

The section-once proofreading profile preserves LLM-jp-4.1 33B BF16/high, one request
per section, frozen HTML/citations/human blocks, Sol's limited scope review and
Luna's formatting rules. Managed HF Jobs retain exact receipts, completed-result
reuse, no automatic resubmission of uncertain requests, ownership-bound cleanup
and time/cost limits. Static equivalence checks covered 16 correction/transport
modules and 58 Codex execution contracts, including final schemas and model settings.

Saved Knowledge Search reports read source context, verify original-page
bibliography and independently check claims and exact quotes. Corpus updates retain
interrupted attempts and expose measured progress. The public HTML preview and
generic bibliography/recovery contracts remain in use. Examples are independently
authored fictional cases; personal manuscripts, bibliography collections, live IDs,
credentials, deployment records and local configuration are excluded.

An embedding build now waits for its resource sampler to finish before returning.
A sampler stopped during a slow GPU query cannot open a late database connection.
The deterministic regression rejects the previous short-join behavior. Index/search
checks retain their original assertions; no cleanup errors or failures are ignored.

Validation also covers:

- Complete Python suite: 2,336 passed, no failures, errors or skips (616.35 seconds,
  eight workers). The slow-sampler regression and FAISS tests are included.
- A new minimal Python environment with no HF, OCR SDK, Torch or GiNZA extras:
  initialization, live loopback health/UI/workflows/search/report-list requests and
  shutdown passed using empty isolated databases, with no external inference.
- Upgrade from state schema 0055 to 0057 retains a saved Run and all applied
  checksums. All 80 previously published SQL files remain byte-identical; 0056 and
  0057 are appended. Fresh initialization and repeat initialization also pass.
- All three manuscript profile diagrams and the ingestion/search diagrams reproduce
  executable contracts. The current section-once graph has 64 nodes and 79 edges.
- UI syntax/unit checks: 69 passed, no failures or skips. The checked-in npm lockfile
  reproduces the browser test dependency.
- Headless Edge: review switching, autosave, concurrent drafts, checkbox-only
  answers, HTML isolation, upright citation titles, report creation, saved history,
  error retention, evidence links, HTML download, selection races and mobile closure
  passed. Desktop and 390px mobile screenshots were inspected.
- Ruff and Git whitespace checks passed. Publication checks include new untracked
  source, HF token formats, live HF endpoint literals and forbidden private files.
- Publication boundary checks passed for 507 source files. Known private identifiers
  (84) and personal literals were absent. Added material contains none of the 34
  checked private-collection titles. Private commit history is not a parent of this
  release. The source archive is built from the reviewed Git tree and checked too.

The environment used Python 3.13.5 and Node.js 24 on Windows. HTTP test dependencies
emit their deprecation warning. Model replies and external providers are controlled
fixtures; this release check did not launch paid HF Jobs, send manuscripts to external
models or certify live GPU/BGE-M3 performance or linguistic quality.

## NDLOCR-Lite 2.0.0 (2026-11-24)

- The Windows installer pins the official PyPI release `ndlocr-lite==2.0.0`, prepares
  its four default models with upstream SHA-256 verification, applies the JSON-only
  PDF compatibility patch, and checks the installed CLI version.
- PDF OCR calls the CLI entry point so downloaded model-cache resolution runs.
  Multi-page PDFs explicitly produce one aggregate JSON. Partial-page recognition
  uses the installed model manifest, and its cache policy excludes old-model results.
- Synthetic regressions cover page order and blank pages, recognized old-form
  characters, cached model selection, stale passage-cache rejection, and patch
  execution/idempotence/rejection of unsupported upstream implementations.
- API-key tests modify only their target environment variable and retain unrelated
  native environment entries on Windows. Authentication and text assertions remain
  strict. The focused OCR, passage and Git checkpoint selection passed 30 tests.
- README and setup instructions describe installation and upgrade requirements.
  No personal configuration, model files or operational records are distributed.
- Complete Python suite: 2,342 passed, with no failures, errors or skips (809.70
  seconds, eight workers). Eight existing HTTP test deprecation warnings were emitted.
- UI syntax/unit checks: 69 passed. Repository Ruff, Git whitespace checks and the
  publication guard passed; the guard inspected all 509 distributable source files.
