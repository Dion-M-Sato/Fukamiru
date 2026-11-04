"""Run the long-lived Corpus Sync ingestion lanes outside the API process."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from fukamiru.config import load_settings
from fukamiru.knowledge.corpus import CorpusSyncService
from fukamiru.storage.database import DatabasePair


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--batch-id", required=True)
    parser.add_argument("--result-file", type=Path, required=True)
    args = parser.parse_args()

    settings = load_settings(args.config)
    databases = DatabasePair.from_settings(settings)
    result = CorpusSyncService(databases, settings).ingest_ready_sources(
        args.run_id, args.batch_id
    )
    temporary = args.result_file.with_suffix(".json.tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(result, stream, ensure_ascii=True)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, args.result_file)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
