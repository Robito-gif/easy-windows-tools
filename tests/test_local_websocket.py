import json
import tempfile
import unittest
from pathlib import Path

from websockets.sync.client import connect

import easy_windows_tools as tools
from local_websocket import LocalWebSocketManager
from websocket_standalone import LocalWebSocketManager as StandaloneWebSocketManager


class LocalWebSocketManagerTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "websocket.sqlite3"
        self.manager = LocalWebSocketManager(db_path=self.db_path)

    def tearDown(self):
        self.manager.stop_all()
        self.temp_dir.cleanup()

    def test_service_authentication_and_ping_round_trip(self):
        service, pin = self.manager.create_service("demo-ws", "server", port=8765, max_connections=5)
        self.assertEqual(service["name"], "demo-ws")
        self.assertEqual(len(pin), 11)
        self.assertTrue(pin.isdigit())

        result = self.manager.start_service("demo-ws")
        self.assertIn("demo-ws çalışıyor", result)

        with connect("ws://127.0.0.1:8765", timeout=5) as ws:
            ws.send(json.dumps({"type": "authenticate", "pin": pin}))
            auth = json.loads(ws.recv())
            self.assertEqual(auth["type"], "authenticated")
            self.assertEqual(auth["service"], "demo-ws")

            ws.send(json.dumps({"type": "ping"}))
            pong = json.loads(ws.recv())
            self.assertEqual(pong["type"], "pong")

        stop_result = self.manager.stop_service("demo-ws")
        self.assertIn("demo-ws durduruldu", stop_result)

    def test_create_service_rejects_duplicate_name_or_port(self):
        self.manager.create_service("demo-a", "server", port=8766)
        with self.assertRaises(ValueError):
            self.manager.create_service("demo-a", "server", port=8767)
        with self.assertRaises(ValueError):
            self.manager.create_service("demo-b", "server", port=8766)

    def test_embedded_manager_authenticates_and_handles_ping(self):
        manager = tools.LocalWebSocketManager(db_path=self.db_path)
        service, pin = manager.create_service("embedded-ws", "server", port=8768)
        self.assertEqual(service["name"], "embedded-ws")

        try:
            manager.start_service("embedded-ws")
            with connect("ws://127.0.0.1:8768", timeout=5) as ws:
                ws.send(json.dumps({"type": "authenticate", "pin": pin}))
                self.assertEqual(json.loads(ws.recv())["type"], "authenticated")
                ws.send(json.dumps({"type": "ping"}))
                self.assertEqual(json.loads(ws.recv())["type"], "pong")
        finally:
            manager.stop_all()

    def test_standalone_copy_can_create_a_service(self):
        manager = StandaloneWebSocketManager(
            db_path=Path(self.temp_dir.name) / "standalone.sqlite3",
        )
        service, pin = manager.create_service(
            "future-app", "server", port=8769,
        )
        self.assertEqual(service["name"], "future-app")
        self.assertEqual(len(pin), 11)


if __name__ == "__main__":
    unittest.main()
