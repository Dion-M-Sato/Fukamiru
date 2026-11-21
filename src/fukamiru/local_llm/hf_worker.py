"""Source for the private, bounded HF inference worker (no manuscript at launch)."""

from __future__ import annotations

import hashlib
import json
import os
import queue
import re
import subprocess
import sys
import tarfile
import threading
import time
import urllib.request
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


def sha256(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def install(protocol: dict[str, Any], work: Path) -> tuple[Path, Path, dict[str, str]]:
    work.mkdir(parents=True, exist_ok=True)
    for name, digest in protocol["llama_assets"]:
        target = work / name
        url = "https://github.com/ggml-org/llama.cpp/releases/download/"
        urllib.request.urlretrieve(url + protocol["llama_version"] + "/" + name, target)
        if sha256(target) != digest:
            raise RuntimeError("runtime_digest_mismatch")
        with tarfile.open(target) as archive:
            archive.extractall(work / "llama", filter="data")
    subprocess.run(["apt-get", "update", "-qq"], check=True)
    subprocess.run(["apt-get", "install", "-y", "-qq", "--no-install-recommends",
                    "libgomp1", "libcurl4"], check=True)
    subprocess.run([sys.executable, "-m", "pip", "install", "--quiet",
                    "--disable-pip-version-check", "huggingface_hub==1.29.0"], check=True)
    from huggingface_hub import hf_hub_download

    # The pinned model is public. No HF credential is forwarded to this container.
    model = Path(hf_hub_download(
        protocol["model_repo"], protocol["model_file"], revision=protocol["model_revision"],
        local_dir=work / "models", token=False,
    ))
    if model.stat().st_size != protocol["model_bytes"] or sha256(model) != protocol["model_sha256"]:
        raise RuntimeError("model_digest_mismatch")
    binaries = list((work / "llama").rglob("llama-server"))
    if len(binaries) != 1:
        raise RuntimeError("runtime_binary_missing")
    env = os.environ.copy()
    env["LD_LIBRARY_PATH"] = ":".join(sorted({str(binaries[0].parent), *(
        str(p.parent) for p in (work / "llama").rglob("*.so*")
    )}))
    return binaries[0], model, env


def native(path: str, body: Any = None, timeout: float = 30):
    request = urllib.request.Request(
        "http://127.0.0.1:18085" + path,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json"},
    )
    return urllib.request.urlopen(request, timeout=timeout)


class Tasks:
    """Idempotent submission within this Job, with one native inference slot."""

    def __init__(self, parser_type: Any):
        self.parser_type = parser_type
        self.lock = threading.Lock()
        self.pool = ThreadPoolExecutor(max_workers=1)
        self.rows: dict[str, dict[str, Any]] = {}
        self.hashes: dict[str, str] = {}
        self.last_activity = time.monotonic()

    def submit(self, key: str, payload: dict[str, Any]) -> None:
        if not re.fullmatch(r"[0-9a-f]{64}", key) or payload.get("reasoning_effort") != "high":
            raise ValueError("invalid_request_contract")
        digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
        with self.lock:
            self.last_activity = time.monotonic()
            if key in self.rows:
                if self.hashes[key] != digest:
                    raise ValueError("request_identity_conflict")
                return
            self.rows[key] = {"state": "queued"}
            self.hashes[key] = digest
        self.pool.submit(self.infer, key, payload)

    def infer(self, key: str, payload: dict[str, Any]) -> None:
        parser = self.parser_type()
        try:
            with self.lock:
                self.rows[key] = {"state": "running"}
            with native("/v1/chat/completions", {
                **payload, "stream": True, "stream_options": {"include_usage": True},
            }, timeout=1200) as response:
                events: queue.Queue = queue.Queue()

                def read():
                    try:
                        for raw in response:
                            events.put(raw)
                    finally:
                        events.put(None)

                threading.Thread(target=read, daemon=True).start()
                finished_at = None
                while True:
                    try:
                        line = events.get(timeout=1)
                        if line is None:
                            break
                        parser.feed_line(line.decode("utf-8"))
                    except queue.Empty:
                        pass
                    if parser.finished and finished_at is None:
                        finished_at = time.monotonic()
                    if parser.saw_done or (finished_at and time.monotonic() - finished_at >= 2):
                        break
                    lines = "".join(parser.reasoning)[-10000:].splitlines()[-64:]
                    if any(n >= 16 for line, n in Counter(lines).items() if len(line.strip()) > 8):
                        raise RuntimeError("repetitive_reasoning")
                    with self.lock:
                        self.rows[key] = {"state": "running", "progress": {
                            "reasoning_characters": parser.reasoning_characters,
                            "final_characters": parser.final_characters,
                        }}
            result = parser.result()
            with self.lock:
                self.rows[key] = {"state": "completed", "response": result}
        except Exception as exc:
            with self.lock:
                detail = str(exc)
                self.rows[key] = {"state": "failed", "error": detail
                    if detail == "repetitive_reasoning" or detail.startswith("stream.")
                    else type(exc).__name__, "partial_content": "".join(parser.content),
                    "partial_reasoning": "".join(parser.reasoning)}
        finally:
            self.last_activity = time.monotonic()


def main(protocol: dict[str, Any], parser_type: Any) -> None:
    work = Path("/work/fukamiru-proofreading")
    binary, model, env = install(protocol, work)
    command = [str(binary), "-m", str(model), "--host", "127.0.0.1", "--port", "18085",
               "--alias", model.stem, "--ctx-size", "65536", "--parallel", "1",
               "--n-gpu-layers", "99", "--flash-attn", "on", "--jinja", "--no-webui"]
    log = (work / "llama.log").open("w")
    server = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT, env=env)
    tasks = Tasks(parser_type)

    class Gateway(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def reply(self, value, code=200):
            body = json.dumps(value).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            tasks.last_activity = time.monotonic()
            if self.path == "/identity":
                return self.reply(protocol)
            if self.path.startswith("/tasks/"):
                with tasks.lock:
                    value = tasks.rows.get(self.path.removeprefix("/tasks/"))
                return self.reply(value or {}, 200 if value else 404)
            if self.path in {"/health", "/props"}:
                try:
                    with native(self.path) as response:
                        return self.reply(json.load(response))
                except Exception:
                    return self.reply({"status": "loading"}, 503)
            self.reply({}, 404)

        def do_POST(self):
            tasks.last_activity = time.monotonic()
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 4 * 1024 * 1024:
                return self.reply({}, 413)
            payload = json.loads(self.rfile.read(length))
            if self.path == "/tasks":
                try:
                    tasks.submit(payload["request_id"], payload["request"])
                    return self.reply({"task_id": payload["request_id"]})
                except (KeyError, ValueError):
                    return self.reply({"error": "request_identity_conflict"}, 409)
            if self.path in {"/apply-template", "/tokenize"}:
                with native(self.path, payload) as response:
                    return self.reply(json.load(response))
            self.reply({}, 404)

    gateway = ThreadingHTTPServer(("0.0.0.0", 8080), Gateway)
    gateway.timeout = 1
    print("FUKAMIRU_PRIVATE_BACKEND_STARTED", flush=True)
    try:
        while server.poll() is None:
            gateway.handle_request()
            with tasks.lock:
                busy = any(r["state"] in {"queued", "running"} for r in tasks.rows.values())
            if not busy and time.monotonic() - tasks.last_activity > 120:
                break
    finally:
        gateway.server_close()
        server.terminate()
        try:
            server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            server.kill()
        log.close()
