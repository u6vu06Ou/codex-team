"""Offline tests for team_bus.py: a mock dashboard endpoint stands in for
the bridge's HTTP side; no model calls, no codex."""
import json
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import team_bus  # noqa: E402


class MockDashboard(BaseHTTPRequestHandler):
    received = []

    def log_message(self, *args):
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length))
        MockDashboard.received.append(
            (body, self.headers.get("Origin"), self.path))
        data = json.dumps({"ok": True}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(data)


class TeamBusTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), MockDashboard)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}/sess123/"
        cls.origin = f"http://127.0.0.1:{cls.server.server_port}"
        MockDashboard.received.clear()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def test_initialize_and_tools(self):
        init = team_bus.handle({"id": 1, "method": "initialize", "params": {}},
                               self.base, self.origin)
        self.assertEqual(init["result"]["serverInfo"]["name"], "codex-team-bus")
        tools = team_bus.handle({"id": 2, "method": "tools/list", "params": {}},
                                self.base, self.origin)
        self.assertEqual(tools["result"]["tools"][0]["name"], "team_message")

    def test_team_message_posts_to_bridge(self):
        reply = team_bus.handle({"id": 3, "method": "tools/call", "params": {
            "name": "team_message",
            "arguments": {"name": "worker-b", "message": "status?"}}},
            self.base, self.origin)
        self.assertNotIn("error", reply)
        self.assertIn('"ok": true', reply["result"]["content"][0]["text"])
        body, origin, path = MockDashboard.received[-1]
        self.assertEqual(body, {"op": "send", "name": "worker-b",
                                "message": "status?"})
        self.assertEqual(origin, self.origin)
        self.assertEqual(path, "/sess123/action")

    def test_unknown_tool_is_error_not_crash(self):
        reply = team_bus.handle({"id": 4, "method": "tools/call", "params": {
            "name": "nope", "arguments": {}}}, self.base, self.origin)
        self.assertTrue(reply["result"]["isError"])

    def test_unknown_method_is_jsonrpc_error(self):
        reply = team_bus.handle({"id": 5, "method": "nope", "params": {}},
                                self.base, self.origin)
        self.assertEqual(reply["error"]["code"], -32603)

    def test_notifications_are_ignored(self):
        self.assertIsNone(team_bus.handle({"method": "initialized"},
                                          self.base, self.origin))


if __name__ == "__main__":
    unittest.main()
