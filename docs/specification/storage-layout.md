# Storage layout

All relative configured paths resolve from the Fukamiru checkout, independently
of the shell's current directory. Absolute paths may be set in ignored
`config/local.toml` for a separate data volume or an existing model installation.

| Path | Responsibility | Git |
| --- | --- | --- |
| `src`, `ui`, `migrations`, `tools` | Application source | Yes |
| `config/local.example.toml` | Portable example configuration | Yes |
| `config/local.toml` | Machine-local paths and commands | No |
| `data/state.db` | Runs, revisions, reviews, claims | No |
| `data/knowledge.db` | Sources, spans, chunks, bibliography | No |
| `runtime` | Logs, cache, PID, build scratch | No |
| `corpus/sources` | Large source assets | No |
| `corpus/ocr` | OCR derivatives | No |
| `corpus/knowledge` | Derived retrieval artifacts | No |
| `corpus/vector-index/faiss` | Immutable FAISS snapshots | No |

SQLite, active Git repositories and Qdrant storage must use a supported local
filesystem, such as NTFS on Windows. Do not place them on exFAT. Corpus files and
immutable FAISS snapshots may use a separate volume; FAISS builds use the local
runtime scratch directory before publishing versioned snapshots.

Research projects have no global fixed parent. A user selects a path and the UI registers its absolute path in `state.db`. One project maps to one Git repository. Canonical prose artifacts are HTML; databases, PDFs, logs and vector indexes are excluded.
