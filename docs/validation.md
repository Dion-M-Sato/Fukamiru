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
