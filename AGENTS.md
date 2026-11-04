# Repository instructions

This is Fukamiru, a local research workflow workbench.
The Python backend (`src/fukamiru`), UI (`ui`), migrations and configuration
templates all live in this repository. Use `fukamiru` for Python packages,
commands, configuration namespaces and project identifiers.

- Preserve applied migrations; add a new numbered migration after release.
- Keep `state.db` and `knowledge.db` separate. SQLite tables are authoritative;
  SSE is notification and replay only.
- Never put active SQLite, Git or Qdrant storage on exFAT.
- Keep machine paths and model locations in ignored `config/local.toml`.
  Relative configured paths resolve from this repository root.
- Never commit credentials, local config, documents, databases, models,
  virtual environments, Node dependencies, logs or generated jobs.
- Preserve protected human blocks. Canonical prose artifacts are validated HTML.
- Keep workflow definitions, tests and the UI-linked flow diagrams in sync.
- Before committing run `.venv/Scripts/python.exe -m ruff check .`,
  `.venv/Scripts/python.exe -m pytest`, `npm --prefix ui run check` and
  `git diff --check`. Use `.venv/bin/python` on Unix.
- Fix test failures before committing and record validation in `docs/validation.md`.
  Never hide failures with blanket skips, relaxed assertions or renamed outcomes.
- Use independently authored fictional fixtures. Do not copy live run identifiers,
  manuscripts, private bibliography collections or operational incident records.
- Run `python scripts/check_publication.py` before publishing source or archives.
