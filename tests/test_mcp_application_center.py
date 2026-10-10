import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import easy_windows_tools as tools


class MCPApplicationCenterTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.manager = tools.MCPManager(
            db_path=Path(self.temp_dir.name) / "mcps.sqlite3",
        )
        self.server = self.manager.create_server(
            "Test desktop",
            {
                "name": "Test desktop",
                "executable": sys.executable,
                "source": "test",
            },
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_catalog_has_100_unique_entries_and_requested_applications(self):
        self.assertEqual(len(tools.MCP_APPLICATION_CATALOG), 100)
        self.assertEqual(len(set(tools.MCP_APPLICATION_CATALOG)), 100)
        self.assertTrue({
            "Godot",
            "Unity Hub",
            "Unreal Engine",
            "Blender",
            "Google Chrome",
            "Visual Studio",
            "Visual Studio Code",
            "MCreator",
            "SQLite",
            "Blockbench",
            "IntelliJ IDEA",
        }.issubset(tools.MCP_APPLICATION_CATALOG))

    def test_all_permissions_are_off_by_default(self):
        self.assertEqual(
            self.manager.server_settings(self.server),
            {
                "allow_app_status": False,
                "allow_launch_application": False,
                "allow_close_application": False,
                "allow_screen_capture": False,
                "allow_input_control": False,
            },
        )

    def test_screen_and_input_operations_are_denied_without_importing_control_packages(self):
        with patch.dict(sys.modules, {"pyautogui": None, "pyperclip": None}):
            with self.assertRaises(PermissionError):
                self.manager.capture_screen(self.server)
            with self.assertRaises(PermissionError):
                self.manager.click_screen(self.server, 0, 0)
            with self.assertRaises(PermissionError):
                self.manager.type_into_screen(self.server, "blocked")
            with self.assertRaises(PermissionError):
                self.manager.press_screen_key(self.server, "enter")

    def test_application_status_requires_explicit_permission(self):
        with self.assertRaises(PermissionError):
            self.manager.application_status(self.server)

    def test_application_launch_and_close_require_explicit_permission(self):
        with self.assertRaises(PermissionError):
            self.manager.launch_application(self.server)
        with self.assertRaises(PermissionError):
            self.manager.close_application(self.server)

    def test_permission_update_is_read_for_each_tool_call(self):
        self.manager.update_server_settings(
            self.server["id"], allow_app_status=True,
        )
        with patch.object(
            self.manager, "_running_processes", return_value=[{"pid": 12, "image": "test"}],
        ):
            self.assertTrue(self.manager.application_status(self.server)["running"])

        self.manager.update_server_settings(
            self.server["id"], allow_app_status=False,
        )
        with self.assertRaises(PermissionError):
            self.manager.application_status(self.server)

    def test_non_boolean_permission_values_fail_closed(self):
        with self.manager._connect() as connection:
            connection.execute(
                "UPDATE mcp_servers SET settings = ? WHERE id = ?",
                (json.dumps({"allow_input_control": "true"}), self.server["id"]),
            )
        self.assertFalse(self.manager.server_settings(self.server)["allow_input_control"])

    def test_known_package_id_must_exist_in_search_results(self):
        with patch.object(
            tools.MCPManager,
            "_run_package_search",
            return_value=[
                {"name": "Different package", "id": "Other.Package"},
                {"name": "Blender", "id": "BlenderFoundation.Blender"},
            ],
        ):
            self.assertEqual(
                self.manager.search_catalog_packages("Blender", "winget"),
                [{"name": "Blender", "id": "BlenderFoundation.Blender"}],
            )

    def test_winget_search_parser_discards_non_package_unknown_ids(self):
        result = subprocess.CompletedProcess(
            args=["winget", "search"],
            returncode=0,
            stdout=(
                "Name                           Id                                Version    Source\n"
                "-------------------------------------------------------------------------------------\n"
                "Blender                        BlenderFoundation.Blender         5.2.2      winget\n"
                "Unknown product name 9NHT9NJG3849 Unknown    msstore\n"
            ),
            stderr="",
        )
        with patch("easy_windows_tools.subprocess.run", return_value=result):
            candidates = self.manager._run_package_search("winget", "Blender")
        self.assertIn(
            {"name": "Blender", "id": "BlenderFoundation.Blender"},
            candidates,
        )
        self.assertNotIn("Unknown", {item["id"] for item in candidates})

    def test_http_startup_handshake_checks_server_identity(self):
        initialize_response = MagicMock()
        initialize_response.__enter__.return_value = initialize_response
        initialize_response.read.return_value = json.dumps({
            "jsonrpc": "2.0",
            "id": 1,
            "result": {
                "protocolVersion": "2025-03-26",
                "serverInfo": {"name": "Test desktop"},
            },
        }).encode()
        initialize_response.headers.get.return_value = "test-session"
        notification_response = MagicMock()
        notification_response.__enter__.return_value = notification_response
        close_response = MagicMock()
        close_response.__enter__.return_value = close_response
        with patch(
            "easy_windows_tools.urllib.request.urlopen",
            side_effect=[initialize_response, notification_response, close_response],
        ) as urlopen:
            self.assertTrue(
                self.manager._mcp_http_handshake(
                    "http://127.0.0.1:8801/mcp", "Test desktop",
                )
            )
            self.assertEqual(urlopen.call_count, 3)
            self.assertEqual(urlopen.call_args_list[-1].args[0].get_method(), "DELETE")

        initialize_response.read.return_value = json.dumps({
            "jsonrpc": "2.0",
            "id": 1,
            "result": {
                "protocolVersion": "2025-03-26",
                "serverInfo": {"name": "Test desktop"},
            },
        }).encode()
        with patch(
            "easy_windows_tools.urllib.request.urlopen",
            return_value=initialize_response,
        ):
            self.assertFalse(
                self.manager._mcp_http_handshake(
                    "http://127.0.0.1:8801/mcp", "Another server",
                )
            )


if __name__ == "__main__":
    unittest.main()
