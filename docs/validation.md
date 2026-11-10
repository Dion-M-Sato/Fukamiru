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
