from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from fukamiru.config import ModelSettings
from fukamiru.local_llm import LlamaServerAdapter


class _FixtureHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/health":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"status":"ok"}')
            return
        self.send_error(404)

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        self.rfile.read(length)
        content = json.dumps(
            {
                "edited_text": "修正文",
                "edits": [],
                "reasons": [],
                "confidence": 1.0,
            },
            ensure_ascii=False,
        )
        body = json.dumps({"choices": [{"message": {"content": content}}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_: object) -> None:
        return


class LlamaServerAdapterTests(unittest.TestCase):
    def test_json_schema_is_forwarded_to_llama_server(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            model = root / "model.gguf"
            model.write_bytes(b"fixture")
            adapter = LlamaServerAdapter(
                ModelSettings(
                    path=model,
                    server_command=sys.executable,
                    host="127.0.0.1",
                    port=65529,
                ),
                runtime_root=root,
                model_name="llm-jp-fixture",
            )
            schema = {
                "type": "object",
                "properties": {"edits": {"type": "array"}},
                "required": ["edits"],
            }
            response = {
                "choices": [{"message": {"content": '{"edits":[]}'}}]
            }
            with (
                patch.object(adapter, "ensure_started"),
                patch.object(adapter, "_request_json", return_value=response) as request,
            ):
                payload = adapter.complete_json(
                    system_prompt="system",
                    user_prompt="user",
                    max_tokens=512,
                    json_schema=schema,
                )

            body = request.call_args.args[1]
            self.assertEqual(payload, {"edits": []})
            self.assertEqual(body["max_tokens"], 512)
            self.assertEqual(body["response_format"]["type"], "json_schema")
            self.assertEqual(body["response_format"]["json_schema"]["schema"], schema)

    def test_truncated_structured_output_retries_with_a_larger_budget(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            model = root / "model.gguf"
            model.write_bytes(b"fixture")
            adapter = LlamaServerAdapter(
                ModelSettings(
                    path=model,
                    server_command=sys.executable,
                    host="127.0.0.1",
                    port=65529,
                    context_size=4096,
                ),
                runtime_root=root,
                model_name="llm-jp-fixture",
            )
            truncated = {
                "choices": [
                    {
                        "message": {"content": '{"edits":['},
                        "finish_reason": "length",
                    }
                ],
                "usage": {"completion_tokens": 512},
            }
            completed = {
                "choices": [
                    {
                        "message": {"content": '{"edits":[],"confidence":1.0}'},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"completion_tokens": 12},
            }
            with (
                patch.object(adapter, "ensure_started"),
                patch.object(
                    adapter,
                    "_request_json",
                    side_effect=[truncated, completed],
                ) as request,
            ):
                payload = adapter.complete_json(
                    system_prompt="system",
                    user_prompt="user",
                    max_tokens=512,
                    json_schema={"type": "object"},
                )

            self.assertEqual(payload, {"edits": [], "confidence": 1.0})
            self.assertEqual(request.call_count, 2)
            self.assertEqual(request.call_args_list[0].args[1]["max_tokens"], 512)
            self.assertEqual(request.call_args_list[1].args[1]["max_tokens"], 1024)

    def test_existing_loopback_server_is_used_without_claiming_process_ownership(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            model = root / "model.gguf"
            model.write_bytes(b"fixture")
            server = ThreadingHTTPServer(("127.0.0.1", 0), _FixtureHandler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                adapter = LlamaServerAdapter(
                    ModelSettings(
                        path=model,
                        server_command=sys.executable,
                        host="127.0.0.1",
                        port=server.server_port,
                    ),
                    runtime_root=root,
                    model_name="llm-jp-fixture",
                )
                payload = adapter.complete_json(system_prompt="system", user_prompt="user")
                self.assertEqual(payload["edited_text"], "修正文")
                self.assertEqual(adapter.status()["status"], "ready")
                self.assertFalse(adapter.status()["owned_process"])
                adapter.stop()
            finally:
                server.shutdown()
                server.server_close()
                thread.join(2)

    def test_startup_timeout_remains_visible_as_error_and_stops_owned_process(self) -> None:
        class FakeProcess:
            pid = 12345
            returncode: int | None = None

            def poll(self) -> int | None:
                return self.returncode

            def terminate(self) -> None:
                self.returncode = 0

            def wait(self, timeout: float) -> int:
                del timeout
                return int(self.returncode or 0)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            model = root / "model.gguf"
            model.write_bytes(b"fixture")
            adapter = LlamaServerAdapter(
                ModelSettings(
                    path=model,
                    server_command=sys.executable,
                    host="127.0.0.1",
                    port=65530,
                    startup_timeout_seconds=0.01,
                    fit=False,
                    warmup=False,
                    parallel_slots=1,
                    http_threads=4,
                    webui=False,
                ),
                runtime_root=root,
                model_name="llm-jp-fixture",
            )
            with (
                patch.object(adapter, "_health", return_value=False),
                patch(
                    "fukamiru.local_llm.llama_server.subprocess.Popen",
                    return_value=FakeProcess(),
                ) as popen,
                self.assertRaisesRegex(Exception, "startup timed out"),
            ):
                adapter.ensure_started()
            command = popen.call_args.args[0]
            self.assertIn("--fit", command)
            self.assertEqual(command[command.index("--fit") + 1], "off")
            self.assertIn("--no-warmup", command)
            self.assertEqual(command[command.index("--parallel") + 1], "1")
            self.assertEqual(command[command.index("--threads-http") + 1], "4")
            self.assertIn("--no-webui", command)
            status = adapter.status()
            self.assertEqual(status["status"], "error")
            self.assertIn("startup timed out", status["last_error"])
            self.assertFalse(status["owned_process"])


if __name__ == "__main__":
    unittest.main()
