"""Run-scoped, lazy HF Jobs with durable receipts and bounded cleanup."""

from __future__ import annotations

import base64
import hashlib
import json
import re
import threading
import time
import uuid
import zlib
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any

from fukamiru.config import ModelSettings, Settings
from fukamiru.errors import NonRetryableFukamiruError
from fukamiru.local_llm.hf_api import HFJobsAPI, token_available
from fukamiru.local_llm.hf_protocol import MODEL
from fukamiru.local_llm.llama_server import LlamaServerAdapter
from fukamiru.storage.database import DatabasePair

TERMINAL = {"COMPLETED", "CANCELED", "CANCELLED", "ERROR", "FAILED", "DELETED", "TIMEOUT"}
STOPPED = {"stopped", "rejected"}


def worker_command() -> tuple[list[str], str]:
    directory = Path(__file__).parent
    sources = [(directory / name).read_text(encoding="utf-8")
               for name in ("openai_stream.py", "hf_worker.py")]
    bundle = json.dumps({"sources": sources, "protocol": MODEL}).encode()
    encoded = base64.b64encode(zlib.compress(bundle)).decode()
    code = (
        "import base64,json,zlib; "
        f"b=json.loads(zlib.decompress(base64.b64decode('{encoded}'))); "
        "exec(compile(b['sources'][0],'<stream>','exec')); "
        "exec(compile(b['sources'][1],'<hf-worker>','exec')); "
        "main(b['protocol'],CompletionStream)"
    )
    return ["python", "-u", "-c", code], hashlib.sha256(bundle).hexdigest()


class WorkerView:
    def __init__(self, worker: Any, adapter: Any):
        self._worker = worker
        self.llm_jp_proofreading = adapter

    def __getattr__(self, name):
        return getattr(self._worker, name)


class HFProofreadingBackend:
    model_name = "llm-jp-4.1-33b-thinking-BF16"

    def __init__(self, settings: Settings, databases: DatabasePair, *, api=None):
        self.config = settings.hf_proofreading
        self.runtime_root = settings.paths.runtime_root
        self.databases = databases
        self.api = api or HFJobsAPI()
        self.owner = str(uuid.uuid4())
        self.instance = hashlib.sha256(
            str(settings.paths.state_db.resolve()).encode(),
        ).hexdigest()[:24]
        self.command, self.identity = worker_command()
        self.identity = hashlib.sha256((self.identity + self.config.image).encode()).hexdigest()
        self.active: dict[str, ManagedAdapter] = {}
        self.lock = threading.RLock()
        self.stopping = threading.Event()
        self.monitor: threading.Thread | None = None
        self.last_error: str | None = None

    @property
    def configured(self):
        return bool(self.config.enabled and self.config.namespace and token_available())

    def status(self):
        with self.lock:
            active = list(self.active.values())
        return {"provider": "hf_jobs", "configured": self.configured,
                "status": "error" if self.last_error else "active"
                if any(a.job_id for a in active) else "ready_on_demand"
                if self.configured else "not_configured", "model": self.model_name,
                "reasoning_effort": "high", "job_ids": [a.job_id for a in active if a.job_id],
                "max_minutes": self.config.max_minutes, "max_cost_usd": self.config.max_cost_usd,
                "last_error": self.last_error}

    def start_monitor(self):
        if self.monitor and self.monitor.is_alive():
            return
        self.stopping.clear()
        self.monitor = threading.Thread(target=self._monitor, daemon=True, name="hf-job-cleanup")
        self.monitor.start()

    def _monitor(self):
        while not self.stopping.is_set():
            try:
                with self.lock, self.databases.state.connect() as connection:
                    for session in self.active:
                        connection.execute("UPDATE hf_proofreading_jobs SET heartbeat_at=? "
                                           "WHERE session_id=?", (time.time(), session))
                self.recover()
            except Exception as exc:
                self.last_error = self.last_error or type(exc).__name__
            self.stopping.wait(5)

    def _rows(self):
        with self.databases.state.connect() as connection:
            return [dict(r) for r in connection.execute(
                "SELECT * FROM hf_proofreading_jobs WHERE instance_id=? "
                "AND status NOT IN ('stopped','rejected')", (self.instance,))]

    def recover(self):
        for row in self._rows():
            with self.lock:
                active = row["session_id"] in self.active
            if not active and (row["owner_id"] == self.owner
                               or row["heartbeat_at"] < time.time() - 90):
                self.cleanup(row["session_id"])

    @contextmanager
    def proofreading_session(self, worker, claimed, context):
        adapter = ManagedAdapter(self, worker, claimed)
        with self.lock:
            self.active[adapter.session] = adapter
        try:
            yield WorkerView(worker, adapter)
        finally:
            try:
                self.cleanup(adapter.session)
            finally:
                with self.lock:
                    self.active.pop(adapter.session, None)

    def stop(self):
        self.stopping.set()
        with self.lock:
            sessions = list(self.active)
        for session in sessions:
            self.cleanup(session)
        if self.monitor:
            self.monitor.join(5)

    stop_managed_jobs = stop

    def _labels(self, session):
        return {"fukamiru": "proofreading-v1", "instance": self.instance, "session": session,
                "name": "fukamiru-jp-" + session[:12]}

    def start_job(self, adapter):
        adapter.check_cancelled()
        if not self.configured:
            raise NonRetryableFukamiruError("hf_jobs.authentication_missing")
        hardware = next((h for h in self.api.hardware() if h["name"] == self.config.flavor), None)
        if not hardware or hardware["unitLabel"] != "minute":
            raise NonRetryableFukamiruError("hf_jobs.unsupported_hardware_price")
        # Private port is $0.01/hour (167 micro-USD/minute rounded upward).
        price = int(hardware["unitCostMicroUSD"]) + 167
        if price * 60 > round(self.config.max_hourly_usd * 1_000_000):
            raise NonRetryableFukamiruError("hf_jobs.hourly_price_exceeds_limit")
        with self.databases.state.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if connection.execute("SELECT 1 FROM hf_proofreading_jobs WHERE instance_id=? "
                    "AND status NOT IN ('stopped','rejected')", (self.instance,)).fetchone():
                raise NonRetryableFukamiruError("hf_jobs.previous_job_cleanup_pending")
            reserved = connection.execute(
                "SELECT COALESCE(SUM(reserved_micro_usd),0) FROM hf_proofreading_jobs "
                "WHERE run_id=? AND status!='rejected'", (adapter.claimed.run_id,),
            ).fetchone()[0]
            available = int(self.config.max_cost_usd * 1_000_000) - reserved
            minutes = min(self.config.max_minutes, available // price)
            if minutes < 2:
                raise NonRetryableFukamiruError("hf_jobs.run_budget_already_reserved")
            # Reserve a rounding/stop minute in addition to the provider's hard timeout.
            timeout = (minutes - 1) * 60
            connection.execute(
                "INSERT INTO hf_proofreading_jobs(session_id,run_id,instance_id,owner_id,namespace,"
                "status,reserved_micro_usd,timeout_seconds,created_at,heartbeat_at) "
                "VALUES(?,?,?,?,?,'creating',?,?,?,?)",
                (adapter.session, adapter.claimed.run_id, self.instance, self.owner,
                 self.config.namespace, minutes * price, timeout, time.time(), time.time()),
            )
        spec = {"dockerImage": self.config.image, "command": self.command, "arguments": [],
                "environment": {}, "flavor": self.config.flavor, "timeoutSeconds": timeout,
                "labels": self._labels(adapter.session), "expose": {"ports": [8080]}}
        try:
            result = self.api.launch(self.config.namespace, spec)
            ident = str(result["id"])
            if not re.fullmatch(r"[0-9a-f]{24}", ident):
                raise NonRetryableFukamiruError("hf_jobs.invalid_job_identity")
            adapter.job_id = ident
            with self.databases.state.connect() as connection:
                connection.execute("UPDATE hf_proofreading_jobs SET job_id=?,status='starting' "
                                   "WHERE session_id=?", (ident, adapter.session))
            adapter.settings = replace(adapter.settings, endpoint=f"https://{ident}--8080.hf.jobs")
            deadline = time.monotonic() + min(timeout, self.config.startup_seconds)
            while time.monotonic() < deadline:
                adapter.check_cancelled()
                if adapter._health(timeout=3):
                    proof = self.api.request(adapter.base_url + "/identity")
                    if proof != json.loads(json.dumps(MODEL)):
                        raise NonRetryableFukamiruError("hf_jobs.backend_identity_mismatch")
                    with self.databases.state.connect() as connection:
                        connection.execute("UPDATE hf_proofreading_jobs SET status='ready',"
                            "backend_json=? WHERE session_id=?",
                            (json.dumps(proof), adapter.session))
                    return
                if self.api.inspect(self.config.namespace, ident)["status"]["stage"] in TERMINAL:
                    raise NonRetryableFukamiruError("hf_jobs.backend_exited_before_ready")
                adapter.wait(3)
            raise NonRetryableFukamiruError("hf_jobs.startup_timeout")
        except Exception as exc:
            if (not adapter.job_id and re.fullmatch(r"hf_jobs.http_4\d\d", str(exc))
                    and str(exc) != "hf_jobs.http_429"):
                with self.databases.state.connect() as connection:
                    connection.execute("UPDATE hf_proofreading_jobs SET status='rejected',"
                                       "error_code=? WHERE session_id=?",
                                       (str(exc), adapter.session))
            raise

    def cleanup(self, session):
        with self.databases.state.connect() as connection:
            row = connection.execute("SELECT * FROM hf_proofreading_jobs WHERE session_id=?",
                                     (session,)).fetchone()
        if row is None or row["status"] in STOPPED:
            return
        try:
            ident = row["job_id"]
            if not ident:
                found = self.api.find(row["namespace"], self._labels(session))
                if not found and time.time() > row["created_at"] + row["timeout_seconds"] + 900:
                    with self.databases.state.connect() as connection:
                        connection.execute("UPDATE hf_proofreading_jobs SET status='stopped',"
                            "stopped_at=? WHERE session_id=?", (time.time(), session))
                    return
                if len(found) != 1:
                    raise NonRetryableFukamiruError("hf_jobs.launch_outcome_unknown")
                ident = found[0]["id"]
                with self.databases.state.connect() as connection:
                    connection.execute("UPDATE hf_proofreading_jobs SET job_id=? "
                                       "WHERE session_id=?", (ident, session))
            deadline = time.monotonic() + 30
            cancelled = False
            while True:
                info = self.api.inspect(row["namespace"], ident)
                if any((info.get("labels") or {}).get(k) != v
                       for k, v in self._labels(session).items()):
                    raise NonRetryableFukamiruError("hf_jobs.refuse_unowned_job_cleanup")
                if info["status"]["stage"] in TERMINAL:
                    break
                if not cancelled:
                    self.api.cancel(row["namespace"], ident)
                    cancelled = True
                if time.monotonic() >= deadline:
                    raise NonRetryableFukamiruError("hf_jobs.stop_not_confirmed")
                time.sleep(1)
            with self.databases.state.connect() as connection:
                connection.execute("UPDATE hf_proofreading_jobs SET status='stopped',stopped_at=?,"
                                   "error_code=NULL WHERE session_id=?", (time.time(), session))
            if not self._rows():
                self.last_error = None
        except Exception as exc:
            with self.databases.state.connect() as connection:
                connection.execute("UPDATE hf_proofreading_jobs SET status='stop_failed',"
                                   "error_code=? WHERE session_id=?", (type(exc).__name__, session))
            self.last_error = "hf_jobs.cleanup_pending"
            raise


class ManagedAdapter(LlamaServerAdapter):
    def __init__(self, manager, worker, claimed):
        super().__init__(ModelSettings(path=None, server_command="unused", context_size=65536,
            reasoning_effort="high", request_timeout_seconds=3600),
            runtime_root=manager.runtime_root, model_name=manager.model_name)
        self.manager, self.worker, self.claimed = manager, worker, claimed
        self.session = str(uuid.uuid4())
        self.job_id = None
        self.stage = self.logical_key = ""

    @property
    def configured(self):
        # LlamaServerAdapter.__init__ calls this before manager is assigned.
        return True

    @property
    def model_identity(self):
        return self.manager.identity

    def check_cancelled(self):
        if self.manager.stopping.is_set():
            raise NonRetryableFukamiruError("hf_jobs.service_stopping")
        self.worker._raise_if_run_cancelled(self.claimed, "HF proofreading cancelled")

    def wait(self, seconds):
        self.manager.stopping.wait(seconds)
        self.check_cancelled()

    def ensure_started(self):
        if not self.job_id:
            self.manager.start_job(self)

    def _health(self, *, timeout):
        try:
            return self.manager.api.request(self.base_url + "/health").get("status") == "ok"
        except (OSError, NonRetryableFukamiruError):
            return False

    def cached_result(self, stage, key):
        self.check_cancelled()
        self.stage, self.logical_key = stage, key
        with self.manager.databases.state.connect() as connection:
            row = connection.execute("SELECT * FROM hf_proofreading_requests WHERE run_id=? "
                "AND stage=? AND fingerprint=?", (self.claimed.run_id, stage, key)).fetchone()
        if row is None:
            return None
        if row["result_json"]:
            return json.loads(row["result_json"])
        if row["raw_response_json"] and row["status"] == "received":
            raw = json.loads(row["raw_response_json"])
            choice = raw["choices"][0]
            if choice["finish_reason"] != "stop":
                raise NonRetryableFukamiruError("hf_jobs.saved_completion_unfinished")
            result = json.loads(choice["message"]["content"])
            self.remember(result)
            return result
        raise NonRetryableFukamiruError("hf_jobs.previous_inference_unresolved_no_resubmission")

    def remember(self, result):
        with self.manager.databases.state.connect() as connection:
            connection.execute("UPDATE hf_proofreading_requests SET status='completed',"
                "result_json=?,"
                "updated_at=? WHERE run_id=? AND stage=? AND fingerprint=?",
                (json.dumps(result, ensure_ascii=False), time.time(), self.claimed.run_id,
                 self.stage, self.logical_key))

    def _request_json(self, path, payload, *, timeout):
        if path != "/v1/chat/completions":
            return self.manager.api.request(self.base_url + path, payload)
        self.check_cancelled()
        with self.manager.databases.state.connect() as connection:
            connection.execute("INSERT INTO hf_proofreading_requests(run_id,stage,fingerprint,"
                "session_id,status,request_json,created_at,updated_at) "
                "VALUES(?,?,?,?,'submitting',?,?,?)",
                (self.claimed.run_id, self.stage, self.logical_key, self.session,
                 json.dumps(payload, ensure_ascii=False), time.time(), time.time()))
        self.manager.api.request(self.base_url + "/tasks", {
            "request_id": self.logical_key, "request": payload,
        })
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.check_cancelled()
            value = self.manager.api.request(self.base_url + "/tasks/" + self.logical_key)
            if value["state"] in {"completed", "failed"}:
                with self.manager.databases.state.connect() as connection:
                    connection.execute("UPDATE hf_proofreading_requests SET status=?,"
                        "raw_response_json=?,updated_at=? WHERE run_id=? AND stage=? "
                        "AND fingerprint=?",
                        ("received" if value["state"] == "completed" else "failed",
                         json.dumps(value.get("response", value)), time.time(),
                         self.claimed.run_id, self.stage, self.logical_key))
                if value["state"] == "failed":
                    raise NonRetryableFukamiruError("hf_jobs.native_inference_failed")
                return value["response"]
            with self.manager.databases.state.connect() as connection:
                connection.execute("UPDATE hf_proofreading_requests SET progress_json=?,"
                    "updated_at=? WHERE run_id=? AND stage=? AND fingerprint=?",
                    (json.dumps(value), time.time(), self.claimed.run_id,
                     self.stage, self.logical_key))
            self.wait(2)
        raise NonRetryableFukamiruError("hf_jobs.inference_timeout_no_resubmission")
