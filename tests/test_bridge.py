"""Offline tests for bridge.py: no codex CLI, no model calls, no network."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import bridge  # noqa: E402

CONFIG = """
[bridge]
state_home = "{home}"
codex_cli = "codex-not-really-spawned-in-tests"

[provider]
base_url = "http://localhost:9/v1"
wire_api = "chat"

[models]
fast = "unit-test-model-a"
thinker = "unit-test-model-b"
"""


def write_config(tmp):
    path = Path(tmp) / "team.config.toml"
    path.write_text(CONFIG.format(home=(Path(tmp) / "state").as_posix()),
                    encoding="utf8")
    return path


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cfg_path = write_config(self.tmp)
        bridge.load_config(str(self.cfg_path))

    def test_models_loaded(self):
        self.assertEqual(sorted(bridge.MODELS), ["fast", "thinker"])
        self.assertEqual(bridge.MODELS["fast"], "unit-test-model-a")

    def test_missing_config_exits(self):
        with self.assertRaises(SystemExit):
            bridge.load_config(str(Path(self.tmp) / "nope.toml"))

    def test_worker_toml_uses_config(self):
        toml_text = bridge.build_worker_toml(bridge.CFG)
        self.assertIn('base_url="http://localhost:9/v1"', toml_text)
        self.assertIn('wire_api="chat"', toml_text)
        self.assertIn("requires_openai_auth=false", toml_text)
        self.assertNotIn("experimental_bearer_token", toml_text)

    def test_worker_toml_token_from_env(self):
        bridge.CFG["provider"]["bearer_token_env"] = "CODEX_TEAM_TEST_TOKEN"
        os.environ["CODEX_TEAM_TEST_TOKEN"] = "test-token-123"
        try:
            toml_text = bridge.build_worker_toml(bridge.CFG)
            self.assertIn('experimental_bearer_token="test-token-123"', toml_text)
            # token only lives in generated worker config, not the team config
            self.assertNotIn("test-token-123",
                             self.cfg_path.read_text(encoding="utf8"))
        finally:
            del os.environ["CODEX_TEAM_TEST_TOKEN"]
            del bridge.CFG["provider"]["bearer_token_env"]

    def test_worker_toml_unset_token_env_exits(self):
        bridge.CFG["provider"]["bearer_token_env"] = "CODEX_TEAM_MISSING_TOKEN"
        os.environ.pop("CODEX_TEAM_MISSING_TOKEN", None)
        try:
            with self.assertRaises(SystemExit):
                bridge.build_worker_toml(bridge.CFG)
        finally:
            del bridge.CFG["provider"]["bearer_token_env"]


class DispatchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        bridge.load_config(str(write_config(cls.tmp)))

    def call(self, method, params=None, ident=1):
        return bridge.dispatch({"method": method, "params": params or {},
                                "id": ident})

    def test_initialize(self):
        result = self.call("initialize")
        self.assertEqual(result["serverInfo"]["name"], "codex-team")
        self.assertIn("tools", result["capabilities"])

    def test_tools_list_mentions_aliases(self):
        tools = self.call("tools/list")["tools"]
        self.assertEqual([t["name"] for t in tools],
                         ["team_start", "team_send", "team_status", "team_cancel"])
        start_tool = tools[0]
        self.assertEqual(start_tool["inputSchema"]["properties"]["model"]["enum"],
                         ["fast", "thinker"])

    def test_unknown_alias_rejected_without_spawning(self):
        # must fail BEFORE ensure_host(): no child codex process in tests
        result = self.call("tools/call", {"name": "team_start", "arguments": {
            "name": "w1", "model": "bogus", "task": "x"}})
        self.assertTrue(result["isError"])
        self.assertIn("bogus", result["content"][0]["text"])
        self.assertIsNone(bridge.proc)
        self.assertIsNone(bridge.server)

    def test_status_with_no_workers(self):
        result = self.call("tools/call", {"name": "team_status", "arguments": {}})
        payload = json.loads(result["content"][0]["text"])
        self.assertEqual(payload["workers"], [])
        self.assertIsNone(payload["dashboard"])

    def test_unknown_tool_and_method(self):
        result = self.call("tools/call", {"name": "nope", "arguments": {}})
        self.assertTrue(result["isError"])
        with self.assertRaises(ValueError):
            bridge.dispatch({"method": "nope", "params": {}, "id": 1})


class StdioLoopTests(unittest.TestCase):
    """The bridge as a real MCP stdio subprocess (still offline: the child
    codex app-server is never spawned by these calls)."""

    def test_mcp_handshake_and_error_paths(self):
        tmp = tempfile.mkdtemp()
        cfg = write_config(tmp)
        proc = subprocess.Popen(
            [sys.executable, str(ROOT / "bridge.py"), "--config", str(cfg)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        try:
            def rpc(method, params, ident):
                proc.stdin.write(json.dumps(
                    {"jsonrpc": "2.0", "id": ident, "method": method,
                     "params": params}) + "\n")
                proc.stdin.flush()
                return json.loads(proc.stdout.readline())

            init = rpc("initialize", {"protocolVersion": "2024-11-05"}, 1)
            self.assertEqual(init["result"]["serverInfo"]["name"], "codex-team")
            tools = rpc("tools/list", {}, 2)
            self.assertEqual(len(tools["result"]["tools"]), 4)
            err = rpc("tools/call", {"name": "team_start", "arguments": {
                "name": "w1", "model": "bogus", "task": "x"}}, 3)
            self.assertTrue(err["result"]["isError"])
            status = rpc("tools/call", {"name": "team_status", "arguments": {}}, 4)
            payload = json.loads(status["result"]["content"][0]["text"])
            self.assertEqual(payload["workers"], [])
        finally:
            proc.terminate()
            proc.wait(timeout=10)


if __name__ == "__main__":
    unittest.main()
