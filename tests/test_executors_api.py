"""Tests for the API-backed executors (W5): Ollama Qwen + DeepSeek.

Deterministic and offline: a REAL local stub HTTP server (http.server
in a thread on an ephemeral port) implements Ollama's /api/tags and
/api/generate and DeepSeek's OpenAI-shaped /chat/completions. No live
service is contacted and nothing here depends on the host: the negative
availability paths are proven against a closed local port and a cleared
key, and the model-selection variables (OLLAMA_MODEL, DEEPSEEK_MODEL) are
removed from the environment where a test asserts the default model, so
the suite passes identically on a PC that runs Ollama (fix pass Wave 2.0).
"""
from __future__ import annotations

import json
import os
import socket
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from hybrid_critic_loop import executors_api
from hybrid_critic_loop.executors_api import DeepSeekExecutor, OllamaQwenExecutor

CODE = "class SpeedSampler:\n    def average(self):\n        return 0.0\n"
GENERATE_REPLY = f"Here is the implementation:\n\n```python\n{CODE}```\nDone.\n"
JUDGE_REPLY = {
    "A": {"items": {"evidence": {"score": 8, "confidence": 0.9,
                                 "evidence": "packet A", "rationale": "cited"}}},
    "B": {"items": {"evidence": {"score": 3, "confidence": 0.7,
                                 "evidence": "packet B", "rationale": "thin"}}},
}
RUBRIC = {"id": "panel_v2", "items": ["evidence"]}


class _StubState:
    def __init__(self):
        self.models = ["qwen2.5-coder:14b", "llama3.1:8b"]
        self.generate_reply = GENERATE_REPLY
        self.chat_reply = GENERATE_REPLY
        self.requests: list[dict] = []
        self.delay_s = 0.0  # a slow model: the stub sleeps before answering a POST


STATE = _StubState()


def _is_judge_prompt(text: str) -> bool:
    return "RUBRIC:" in text


class _StubHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # keep test output clean
        pass

    def _send_json(self, obj, status=200):
        body = json.dumps(obj).encode("utf-8")
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except OSError:
            pass  # the client gave up (a timeout test): nothing to answer

    def _read_json_body(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            return json.loads(raw.decode("utf-8")) if raw else {}
        except Exception:
            return {}

    def do_GET(self):
        if self.path == "/api/tags":
            self._send_json({"models": [{"name": n} for n in STATE.models]})
        else:
            self._send_json({"error": "not found"}, status=404)

    def do_POST(self):
        body = self._read_json_body()
        STATE.requests.append(
            {"path": self.path, "body": body,
             "authorization": self.headers.get("Authorization")}
        )
        if STATE.delay_s:
            time.sleep(STATE.delay_s)
        if self.path == "/api/generate":
            prompt = body.get("prompt") or ""
            reply = json.dumps(JUDGE_REPLY) if _is_judge_prompt(prompt) else STATE.generate_reply
            self._send_json({
                "model": body.get("model"),
                "response": reply,
                "prompt_eval_count": 123,
                "eval_count": 45,
                "done": True,
            })
        elif self.path == "/chat/completions":
            messages = body.get("messages") or []
            content = messages[0].get("content", "") if messages else ""
            reply = json.dumps(JUDGE_REPLY) if _is_judge_prompt(content) else STATE.chat_reply
            self._send_json({
                "id": "chatcmpl-stub-1",
                "choices": [{"message": {"role": "assistant", "content": reply}}],
                "usage": {"prompt_tokens": 111, "completion_tokens": 22,
                          "total_tokens": 133},
            })
        else:
            self._send_json({"error": "not found"}, status=404)


def _closed_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


class _StubServerMixin(unittest.TestCase):
    def setUp(self):
        STATE.models = ["qwen2.5-coder:14b", "llama3.1:8b"]
        STATE.generate_reply = GENERATE_REPLY
        STATE.chat_reply = GENERATE_REPLY
        STATE.requests = []
        STATE.delay_s = 0.0
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _StubHandler)
        self.port = self.server.server_address[1]
        self.base = f"http://127.0.0.1:{self.port}"
        self.thread = threading.Thread(
            target=lambda: self.server.serve_forever(poll_interval=0.05), daemon=True
        )
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


class TestOllamaQwenExecutor(_StubServerMixin):
    def test_identity(self):
        ex = OllamaQwenExecutor()
        self.assertEqual(ex.expert_id, "qwen-local")
        self.assertEqual(ex.family, "alibaba")

    def test_available_true_with_model_listed(self):
        ex = OllamaQwenExecutor()
        with patch.dict(os.environ, {"OLLAMA_HOST": self.base}):
            ok, detail = ex.available()
        self.assertTrue(ok, detail)
        self.assertIn("qwen2.5-coder:14b", detail)
        self.assertEqual(ex.model_tag, "qwen2.5-coder:14b")

    def test_available_false_when_model_absent(self):
        STATE.models = ["llama3.1:8b"]
        ex = OllamaQwenExecutor()
        with patch.dict(os.environ, {"OLLAMA_HOST": self.base}):
            ok, detail = ex.available()
        self.assertFalse(ok)
        self.assertIn("qwen2.5-coder", detail)

    def test_available_false_no_exception_when_port_closed(self):
        host = f"http://127.0.0.1:{_closed_port()}"
        ex = OllamaQwenExecutor()
        with patch.dict(os.environ, {"OLLAMA_HOST": host}):
            ok, detail = ex.available()  # must not raise
        self.assertFalse(ok)
        self.assertEqual(detail, f"ollama not reachable at {host}")

    def test_generate_writes_fenced_code_and_reports_tokens(self):
        ex = OllamaQwenExecutor()
        contract = {"task": "Implement SpeedSampler", "target_file": "src/speed_sampler.py"}
        with patch.dict(os.environ, {"OLLAMA_HOST": self.base}):
            os.environ.pop("OLLAMA_MODEL", None)  # assert the default tag, not the host's
            with tempfile.TemporaryDirectory() as td:
                result = ex.generate(Path(td), contract, "")
                written = Path(td) / "src" / "speed_sampler.py"
                self.assertTrue(written.exists())
                self.assertEqual(written.read_text(encoding="utf-8"), CODE)
        self.assertEqual(result.status, "completed", result.detail)
        self.assertEqual(result.tokens_in, 123)   # prompt_eval_count
        self.assertEqual(result.tokens_out, 45)   # eval_count
        self.assertIsNone(result.cost_usd)        # local: never invented
        generate_calls = [r for r in STATE.requests if r["path"] == "/api/generate"]
        self.assertEqual(len(generate_calls), 1)
        self.assertEqual(generate_calls[0]["body"]["model"], "qwen2.5-coder:14b")
        self.assertFalse(generate_calls[0]["body"]["stream"])

    def test_generate_error_when_reply_has_no_code_block(self):
        STATE.generate_reply = "I cannot write code, sorry."
        ex = OllamaQwenExecutor()
        contract = {"task": "x", "target_file": "src/speed_sampler.py"}
        with patch.dict(os.environ, {"OLLAMA_HOST": self.base}):
            with tempfile.TemporaryDirectory() as td:
                result = ex.generate(Path(td), contract, "")
                self.assertFalse((Path(td) / "src" / "speed_sampler.py").exists())
        self.assertEqual(result.status, "error")
        self.assertIn("no fenced code block", result.detail)

    def test_generate_error_no_exception_when_unreachable(self):
        host = f"http://127.0.0.1:{_closed_port()}"
        ex = OllamaQwenExecutor()
        with patch.dict(os.environ, {"OLLAMA_HOST": host}):
            with tempfile.TemporaryDirectory() as td:
                result = ex.generate(  # must not raise
                    Path(td), {"task": "x", "target_file": "src/x.py"}, "")
        self.assertEqual(result.status, "error")
        self.assertTrue(result.detail)

    def test_generate_without_target_file_is_declined_before_any_request(self):
        # Fix pass Wave 2.9 (review M8 / 1D-12): an API seat cannot place a
        # reply in the worktree without a target_file — it declines up front
        # instead of spending tokens and reporting completed with nothing
        # written (the behaviour this test used to pin).
        ex = OllamaQwenExecutor()
        with patch.dict(os.environ, {"OLLAMA_HOST": self.base}):
            with tempfile.TemporaryDirectory() as td:
                result = ex.generate(Path(td), {"task": "explain something"}, "")
                self.assertEqual(os.listdir(td), [])
        self.assertEqual(result.status, "unavailable", result.detail)
        self.assertIn("target_file", result.detail)
        self.assertEqual([r for r in STATE.requests if r["path"] == "/api/generate"], [])

    def test_generate_timeout_is_reported_as_timeout(self):
        # Fix pass Wave 2.9 (review M8 / 1D-07): a slow model is a timeout,
        # not an error — the ledger has an outcome for it.
        STATE.delay_s = 1.0
        ex = OllamaQwenExecutor()
        contract = {"task": "x", "target_file": "src/speed_sampler.py"}
        with patch.dict(os.environ, {"OLLAMA_HOST": self.base}), \
                patch.object(executors_api, "_GENERATE_TIMEOUT_S", 0.3):
            with tempfile.TemporaryDirectory() as td:
                result = ex.generate(Path(td), contract, "")
                self.assertFalse((Path(td) / "src" / "speed_sampler.py").exists())
        self.assertEqual(result.status, "timeout", result.detail)
        self.assertIn("timed out", result.detail)

    def test_judge_parses_stub_json_reply(self):
        ex = OllamaQwenExecutor()
        with patch.dict(os.environ, {"OLLAMA_HOST": self.base}):
            result = ex.judge("packet text", RUBRIC)
        self.assertEqual(result, JUDGE_REPLY)

    def test_judge_none_when_unreachable(self):
        host = f"http://127.0.0.1:{_closed_port()}"
        ex = OllamaQwenExecutor()
        with patch.dict(os.environ, {"OLLAMA_HOST": host}):
            self.assertIsNone(ex.judge("packet text", RUBRIC))


class TestDeepSeekExecutor(_StubServerMixin):
    def test_identity(self):
        ex = DeepSeekExecutor()
        self.assertEqual(ex.expert_id, "deepseek")
        self.assertEqual(ex.family, "deepseek")

    def test_available_toggles_on_env_var(self):
        ex = DeepSeekExecutor()
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("DEEPSEEK_API_KEY", None)
            ok, detail = ex.available()
            self.assertFalse(ok)
            self.assertEqual(detail, "DEEPSEEK_API_KEY not set")
            os.environ["DEEPSEEK_API_KEY"] = ""
            ok, _ = ex.available()
            self.assertFalse(ok, "empty key must not count as present")
            os.environ["DEEPSEEK_API_KEY"] = "test-key-not-real"
            ok, detail = ex.available()
            self.assertTrue(ok)
            self.assertEqual(detail, "api key present")

    def _patched(self):
        return patch.object(
            executors_api, "DEEPSEEK_API_URL", f"{self.base}/chat/completions"
        )

    def test_generate_against_stub(self):
        ex = DeepSeekExecutor()
        contract = {"task": "Implement SpeedSampler", "target_file": "src/speed_sampler.py"}
        env = {"DEEPSEEK_API_KEY": "test-key-not-real"}
        with patch.dict(os.environ, env), self._patched():
            os.environ.pop("DEEPSEEK_MODEL", None)  # assert the default model, not the host's
            with tempfile.TemporaryDirectory() as td:
                result = ex.generate(Path(td), contract, "")
                written = Path(td) / "src" / "speed_sampler.py"
                self.assertTrue(written.exists())
                self.assertEqual(written.read_text(encoding="utf-8"), CODE)
        self.assertEqual(result.status, "completed", result.detail)
        self.assertEqual(result.tokens_in, 111)   # usage.prompt_tokens
        self.assertEqual(result.tokens_out, 22)   # usage.completion_tokens
        self.assertIsNone(result.cost_usd)        # not reported: never computed
        self.assertEqual(result.request_ids, ["chatcmpl-stub-1"])
        calls = [r for r in STATE.requests if r["path"] == "/chat/completions"]
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["body"]["model"], "deepseek-chat")
        self.assertEqual(calls[0]["authorization"], "Bearer test-key-not-real")

    def test_generate_unavailable_without_key_makes_no_request(self):
        ex = DeepSeekExecutor()
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("DEEPSEEK_API_KEY", None)
            with tempfile.TemporaryDirectory() as td:
                result = ex.generate(Path(td), {"task": "x"}, "")
        self.assertEqual(result.status, "unavailable")
        self.assertEqual(result.detail, "DEEPSEEK_API_KEY not set")
        self.assertEqual(STATE.requests, [])

    def test_generate_without_target_file_is_declined_before_any_request(self):
        # Fix pass Wave 2.9 (review M8 / 1D-12).
        ex = DeepSeekExecutor()
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test-key-not-real"}), self._patched():
            with tempfile.TemporaryDirectory() as td:
                result = ex.generate(Path(td), {"task": "explain something"}, "")
                self.assertEqual(os.listdir(td), [])
        self.assertEqual(result.status, "unavailable", result.detail)
        self.assertIn("target_file", result.detail)
        self.assertEqual(STATE.requests, [])

    def test_generate_timeout_is_reported_as_timeout(self):
        # Fix pass Wave 2.9 (review M8 / 1D-07).
        STATE.delay_s = 1.0
        ex = DeepSeekExecutor()
        contract = {"task": "x", "target_file": "src/speed_sampler.py"}
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test-key-not-real"}), self._patched(), \
                patch.object(executors_api, "_GENERATE_TIMEOUT_S", 0.3):
            with tempfile.TemporaryDirectory() as td:
                result = ex.generate(Path(td), contract, "")
        self.assertEqual(result.status, "timeout", result.detail)
        self.assertIn("timed out", result.detail)

    def test_generate_error_no_exception_when_endpoint_closed(self):
        ex = DeepSeekExecutor()
        closed = f"http://127.0.0.1:{_closed_port()}/chat/completions"
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test-key-not-real"}), \
                patch.object(executors_api, "DEEPSEEK_API_URL", closed):
            with tempfile.TemporaryDirectory() as td:
                result = ex.generate(  # must not raise
                    Path(td), {"task": "x", "target_file": "src/x.py"}, "")
        self.assertEqual(result.status, "error")
        self.assertTrue(result.detail)

    def test_judge_against_stub(self):
        ex = DeepSeekExecutor()
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "test-key-not-real"}), self._patched():
            result = ex.judge("packet text", RUBRIC)
        self.assertEqual(result, JUDGE_REPLY)

    def test_judge_none_without_key(self):
        ex = DeepSeekExecutor()
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("DEEPSEEK_API_KEY", None)
            self.assertIsNone(ex.judge("packet text", RUBRIC))


class TestAvailabilityNegativesAreDeterministic(unittest.TestCase):
    """The honest 'unavailable' reports, proven without touching the host:
    a cleared key and a port nothing listens on. (The previous version of
    this class asserted that the build sandbox had no Ollama server, which
    made the suite fail on the owner's PC — fix pass Wave 2.0, F-W20-03.)"""

    def test_deepseek_unavailable_without_key(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("DEEPSEEK_API_KEY", None)
            ok, detail = DeepSeekExecutor().available()
        self.assertFalse(ok)
        self.assertEqual(detail, "DEEPSEEK_API_KEY not set")

    def test_ollama_unavailable_when_nothing_listens(self):
        host = f"http://127.0.0.1:{_closed_port()}"
        with patch.dict(os.environ, {"OLLAMA_HOST": host}):
            ok, detail = OllamaQwenExecutor().available()
        self.assertFalse(ok)
        self.assertEqual(detail, f"ollama not reachable at {host}")


if __name__ == "__main__":
    unittest.main()
