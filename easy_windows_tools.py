# -*- coding: utf-8 -*-
"""Easy Windows Tools: Windows power and display controls for the terminal."""

# Copyright (C) 2026 Robin Güneş
#
# This program is free software: you can redistribute it and/or modify it under
# the terms of the GNU General Public License as published by the Free Software
# Foundation, version 3 only.
#
# This program is distributed in the hope that it will be useful, but WITHOUT
# ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS
# FOR A PARTICULAR PURPOSE. See the GNU General Public License for details.
# You should have received a copy of the GNU General Public License along with
# this program. If not, see <https://www.gnu.org/licenses/>.

import ctypes
import csv
import errno
import hashlib
import hmac
import json
import os
import re
import secrets
import shlex
import shutil
import socket
import sqlite3
import string
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from contextlib import ExitStack, contextmanager
from datetime import datetime, timedelta, timezone
from ctypes import wintypes
from pathlib import Path
from typing import Any, Callable

import websockets
from websockets.exceptions import ConnectionClosed, InvalidURI
from websockets.sync.server import ServerConnection, WebSocketServer, serve

APP_DATA_DIRECTORY = Path(
    os.environ.get("APPDATA", Path.home() / ".config")
) / "EasyWindowsTools"
DEFAULT_DATABASE_PATH = APP_DATA_DIRECTORY / "websocket_services.sqlite3"
LOCALHOST_ORIGIN = re.compile(r"https?://(localhost|127\.0\.0\.1)(:\d+)?\Z")
MAX_MESSAGE_BYTES = 1024 * 1024
MAX_PIN_ATTEMPTS = 5
PIN_LENGTH = 11
PIN_SCRYPT_N = 1 << 14


class LocalWebSocketManager:
    """Manage local WebSocket endpoints and authenticated peer relays."""

    CONNECTION_TYPES = {
        "server": "Uygulama sunucusu",
        "relay": "Yerel tünel / aktarıcı",
    }

    def __init__(self, db_path: str | Path | None = None):
        configured_path = db_path or os.environ.get("EWT_WEBSOCKET_DB")
        self.db_path = Path(configured_path) if configured_path else DEFAULT_DATABASE_PATH
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._runtimes: dict[int, dict[str, Any]] = {}
        self._runtime_lock = threading.RLock()
        self._message_handlers: dict[int, Callable[[dict[str, Any]], Any]] = {}
        self._initialize_database()

    @contextmanager
    def _connect(self):
        connection = sqlite3.connect(self.db_path, timeout=10)
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def _initialize_database(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """CREATE TABLE IF NOT EXISTS websocket_services (
                    id INTEGER PRIMARY KEY,
                    name TEXT NOT NULL COLLATE NOCASE UNIQUE,
                    connection_type TEXT NOT NULL CHECK(connection_type IN ('server', 'relay')),
                    host TEXT NOT NULL DEFAULT '127.0.0.1',
                    port INTEGER NOT NULL UNIQUE CHECK(port BETWEEN 1024 AND 65535),
                    pin_salt TEXT NOT NULL,
                    pin_hash TEXT NOT NULL,
                    max_connections INTEGER NOT NULL DEFAULT 10
                        CHECK(max_connections BETWEEN 1 AND 100),
                    created_at TEXT NOT NULL
                )"""
            )

    @staticmethod
    def _hash_pin(pin: str, salt: bytes) -> bytes:
        return hashlib.scrypt(
            pin.encode("ascii"),
            salt=salt,
            n=PIN_SCRYPT_N,
            r=8,
            p=1,
            dklen=32,
        )

    @staticmethod
    def _new_pin() -> str:
        return "".join(secrets.choice(string.digits) for _ in range(PIN_LENGTH))

    @classmethod
    def _validate_configuration(
        cls,
        name: str,
        connection_type: str,
        port: int,
        max_connections: int,
    ) -> tuple[str, str, int, int]:
        name = name.strip()
        if not name or len(name) > 80 or any(ord(char) < 32 for char in name):
            raise ValueError("Bağlantı adı 1-80 karakter olmalı ve kontrol karakteri içermemelidir.")
        if connection_type not in cls.CONNECTION_TYPES:
            raise ValueError("Bağlantı türü 'server' veya 'relay' olmalıdır.")
        if type(port) is not int or not 1024 <= port <= 65535:
            raise ValueError("Port numarası 1024 ile 65535 arasında olmalıdır.")
        if type(max_connections) is not int or not 1 <= max_connections <= 100:
            raise ValueError("Eşzamanlı bağlantı sınırı 1 ile 100 arasında olmalıdır.")
        return name, connection_type, port, max_connections

    def create_service(
        self,
        name: str,
        connection_type: str,
        port: int = 8765,
        max_connections: int = 10,
    ) -> tuple[dict[str, Any], str]:
        name, connection_type, port, max_connections = self._validate_configuration(
            name, connection_type, port, max_connections,
        )
        pin = self._new_pin()
        salt = secrets.token_bytes(16)
        created_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        try:
            with self._connect() as connection:
                cursor = connection.execute(
                    """INSERT INTO websocket_services
                       (name, connection_type, host, port, pin_salt, pin_hash,
                        max_connections, created_at)
                       VALUES (?, ?, '127.0.0.1', ?, ?, ?, ?, ?)""",
                    (
                        name, connection_type, port, salt.hex(),
                        self._hash_pin(pin, salt).hex(), max_connections, created_at,
                    ),
                )
                service_id = cursor.lastrowid
        except sqlite3.IntegrityError as error:
            raise ValueError(
                "Bu ad veya port başka bir WebSocket kaydında kullanılıyor."
            ) from error
        service = self.get_service_by_id(service_id)
        if service is None:
            raise RuntimeError("WebSocket kaydı oluşturuldu ancak okunamadı.")
        return service, pin

    def update_service(
        self,
        service_id: int,
        name: str,
        connection_type: str,
        port: int,
        max_connections: int,
    ) -> dict[str, Any]:
        name, connection_type, port, max_connections = self._validate_configuration(
            name, connection_type, port, max_connections,
        )
        if self.is_running(service_id):
            raise RuntimeError("Çalışan bir bağlantı düzenlenemez; önce durdurun.")
        try:
            with self._connect() as connection:
                cursor = connection.execute(
                    """UPDATE websocket_services
                       SET name = ?, connection_type = ?, host = '127.0.0.1',
                           port = ?, max_connections = ?
                       WHERE id = ?""",
                    (name, connection_type, port, max_connections, service_id),
                )
        except sqlite3.IntegrityError as error:
            raise ValueError(
                "Bu ad veya port başka bir WebSocket kaydında kullanılıyor."
            ) from error
        if not cursor.rowcount:
            raise ValueError("WebSocket kaydı bulunamadı.")
        service = self.get_service_by_id(service_id)
        if service is None:
            raise RuntimeError("Güncellenen WebSocket kaydı tekrar okunamadı.")
        return service

    def rotate_pin(self, service_id: int) -> str:
        pin = self._new_pin()
        salt = secrets.token_bytes(16)
        with self._connect() as connection:
            cursor = connection.execute(
                """UPDATE websocket_services
                   SET pin_salt = ?, pin_hash = ? WHERE id = ?""",
                (salt.hex(), self._hash_pin(pin, salt).hex(), service_id),
            )
        if not cursor.rowcount:
            raise ValueError("WebSocket kaydı bulunamadı.")
        return pin

    def get_service_by_id(self, service_id: int) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                """SELECT id, name, connection_type, host, port, pin_salt,
                          pin_hash, max_connections, created_at
                   FROM websocket_services WHERE id = ?""",
                (service_id,),
            ).fetchone()
        return dict(row) if row else None

    def get_service(self, name: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                """SELECT id, name, connection_type, host, port, pin_salt,
                          pin_hash, max_connections, created_at
                   FROM websocket_services WHERE name = ? COLLATE NOCASE""",
                (name.strip(),),
            ).fetchone()
        return dict(row) if row else None

    def list_services(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """SELECT id, name, connection_type, host, port, pin_salt,
                          pin_hash, max_connections, created_at
                   FROM websocket_services ORDER BY name COLLATE NOCASE"""
            ).fetchall()
        return [dict(row) for row in rows]

    def delete_service(self, service_id: int) -> None:
        if self.is_running(service_id):
            self.stop_service(service_id)
        with self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM websocket_services WHERE id = ?", (service_id,),
            )
        if not cursor.rowcount:
            raise ValueError("WebSocket kaydı bulunamadı.")
        self._message_handlers.pop(service_id, None)

    def register_message_handler(
        self,
        service_name: str,
        handler: Callable[[dict[str, Any]], Any],
    ) -> None:
        """Register a Python callback for authenticated application-server messages."""
        service = self.get_service(service_name)
        if service is None:
            raise ValueError(f"'{service_name}' adında WebSocket kaydı yok.")
        if service["connection_type"] != "server":
            raise ValueError("Mesaj işleyicisi yalnızca uygulama sunucusu türünde kullanılabilir.")
        if not callable(handler):
            raise TypeError("Mesaj işleyicisi çağrılabilir bir Python fonksiyonu olmalıdır.")
        self._message_handlers[service["id"]] = handler

    def _authenticate(self, service: dict[str, Any], pin: str) -> bool:
        if not isinstance(pin, str) or not re.fullmatch(r"\d{11}", pin):
            return False
        salt = bytes.fromhex(service["pin_salt"])
        candidate = self._hash_pin(pin, salt)
        return hmac.compare_digest(candidate.hex(), service["pin_hash"])

    def is_running(self, service_id: int) -> bool:
        with self._runtime_lock:
            runtime = self._runtimes.get(service_id)
        return bool(runtime and runtime["thread"].is_alive() and not runtime["error"])

    def running_names(self) -> set[str]:
        return {
            service["name"]
            for service in self.list_services()
            if self.is_running(service["id"])
        }

    def start_service(self, name: str) -> str:
        service = self.get_service(name)
        if service is None:
            raise ValueError(f"'{name}' adında WebSocket kaydı bulunamadı.")
        service_id = service["id"]
        with self._runtime_lock:
            existing = self._runtimes.get(service_id)
            if existing and existing["thread"].is_alive():
                if existing["error"]:
                    raise RuntimeError(
                        f"WebSocket sunucusu çalışırken hata oluştu: {existing['error']}"
                    )
                return f"{service['name']} zaten çalışıyor: ws://127.0.0.1:{service['port']}"
            runtime: dict[str, Any] = {
                "thread": None,
                "server": None,
                "ready": threading.Event(),
                "error": None,
            }
            thread = threading.Thread(
                target=self._serve_service,
                args=(service, runtime),
                name=f"ewt-websocket-{service_id}",
                daemon=True,
            )
            runtime["thread"] = thread
            self._runtimes[service_id] = runtime
            thread.start()

        if not runtime["ready"].wait(timeout=10):
            self.stop_service(service_id)
            raise RuntimeError("WebSocket dinleyicisi 10 saniye içinde başlamadı.")
        if runtime["error"]:
            raise RuntimeError(f"WebSocket dinleyicisi başlatılamadı: {runtime['error']}")
        return f"{service['name']} çalışıyor: ws://127.0.0.1:{service['port']}"

    def start_all(self) -> list[tuple[str, str | None]]:
        results = []
        for service in self.list_services():
            try:
                results.append((service["name"], self.start_service(service["name"])))
            except (OSError, RuntimeError, ValueError) as error:
                results.append((service["name"], str(error)))
        return results

    def stop_service(self, service: int | str) -> str:
        if isinstance(service, str):
            record = self.get_service(service)
            if record is None:
                raise ValueError(f"'{service}' adında WebSocket kaydı bulunamadı.")
            service_id = record["id"]
        else:
            service_id = service
            record = self.get_service_by_id(service_id)
            if record is None:
                raise ValueError("WebSocket kaydı bulunamadı.")

        with self._runtime_lock:
            runtime = self._runtimes.get(service_id)
        if not runtime:
            return f"{record['name']} çalışmıyor."
        server: WebSocketServer | None = runtime["server"]
        if server is not None:
            server.shutdown()
        thread: threading.Thread = runtime["thread"]
        thread.join(timeout=10)
        if thread.is_alive():
            raise RuntimeError(f"{record['name']} sunucusu 10 saniye içinde durmadı.")
        with self._runtime_lock:
            self._runtimes.pop(service_id, None)
        if runtime["error"]:
            raise RuntimeError(f"{record['name']} durdurulurken hata oluştu: {runtime['error']}")
        return f"{record['name']} durduruldu."

    def stop_all(self) -> list[tuple[str, str | None]]:
        results = []
        with self._runtime_lock:
            service_ids = list(self._runtimes)
        for service_id in service_ids:
            service = self.get_service_by_id(service_id)
            name = service["name"] if service else str(service_id)
            try:
                results.append((name, self.stop_service(service_id)))
            except (OSError, RuntimeError, ValueError) as error:
                results.append((name, str(error)))
        return results

    def _serve_service(self, service: dict[str, Any], runtime: dict[str, Any]) -> None:
        peers: dict[ServerConnection, threading.Lock] = {}
        peers_lock = threading.RLock()

        def handler(connection: ServerConnection) -> None:
            with peers_lock:
                if len(peers) >= service["max_connections"]:
                    connection.close(1013, "Connection capacity reached")
                    return
            try:
                initial_message = connection.recv(timeout=5)
                if not isinstance(initial_message, str) or len(initial_message.encode("utf-8")) > MAX_MESSAGE_BYTES:
                    connection.close(1009, "Authentication message too large")
                    return
                try:
                    credentials = json.loads(initial_message)
                except json.JSONDecodeError:
                    connection.close(4001, "Invalid authentication message")
                    return
                pin = credentials.get("pin") if isinstance(credentials, dict) else None
                if not isinstance(credentials, dict) or credentials.get("type") != "authenticate":
                    connection.close(4001, "Authentication required")
                    return
                if not self._authenticate(service, pin):
                    connection.close(4003, "Invalid PIN")
                    return
                connection.send(json.dumps({
                    "type": "authenticated",
                    "service": service["name"],
                    "connectionType": service["connection_type"],
                    "protocolVersion": "1",
                }))
                peer_lock = threading.Lock()
                with peers_lock:
                    peers[connection] = peer_lock
                while True:
                    raw_message = connection.recv()
                    if not isinstance(raw_message, str) or len(raw_message.encode("utf-8")) > MAX_MESSAGE_BYTES:
                        connection.close(1009, "Message too large")
                        break
                    try:
                        message = json.loads(raw_message)
                    except json.JSONDecodeError:
                        connection.send(json.dumps({"type": "error", "error": "invalid_json"}))
                        continue
                    if not isinstance(message, dict):
                        connection.send(json.dumps({"type": "error", "error": "object_required"}))
                        continue
                    if service["connection_type"] == "relay":
                        self._relay_message(connection, message, peers, peers_lock, service["name"])
                    else:
                        self._handle_application_message(
                            connection, message, peer_lock, service,
                        )
            except TimeoutError:
                connection.close(4001, "Authentication timeout")
            except ConnectionClosed:
                pass
            finally:
                with peers_lock:
                    peers.pop(connection, None)

        try:
            with serve(
                handler,
                host="127.0.0.1",
                port=service["port"],
                origins=[LOCALHOST_ORIGIN, None],
                max_size=MAX_MESSAGE_BYTES,
                open_timeout=5,
                close_timeout=2,
            ) as server:
                runtime["server"] = server
                runtime["ready"].set()
                server.serve_forever()
        except (OSError, RuntimeError) as error:
            runtime["error"] = error
            runtime["ready"].set()
        except Exception as error:
            runtime["error"] = error
            runtime["ready"].set()

    def _handle_application_message(
        self,
        connection: ServerConnection,
        message: dict[str, Any],
        peer_lock: threading.Lock,
        service: dict[str, Any],
    ) -> None:
        if message.get("type") == "ping":
            response: Any = {"type": "pong", "timestamp": time.time()}
        else:
            handler = self._message_handlers.get(service["id"])
            try:
                response = (
                    handler(message)
                    if handler
                    else {
                        "type": "response",
                        "requestId": message.get("requestId"),
                        "payload": message.get("payload", message),
                    }
                )
            except Exception as error:
                response = {
                    "type": "error",
                    "requestId": message.get("requestId"),
                    "error": str(error)[:500],
                }
        try:
            encoded_response = json.dumps(
                response,
                ensure_ascii=False,
                separators=(",", ":"),
                allow_nan=False,
            )
        except (TypeError, ValueError):
            encoded_response = json.dumps({
                "type": "error",
                "requestId": message.get("requestId"),
                "error": "handler_response_must_be_json",
            })
        with peer_lock:
            connection.send(encoded_response)

    @staticmethod
    def _relay_message(
        sender: ServerConnection,
        message: dict[str, Any],
        peers: dict[ServerConnection, threading.Lock],
        peers_lock: threading.RLock,
        service_name: str,
    ) -> None:
        payload = json.dumps(
            {
                "type": "relay",
                "service": service_name,
                "sender": message.get("sender"),
                "payload": message.get("payload", message),
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        with peers_lock:
            recipients = [(peer, lock) for peer, lock in peers.items() if peer is not sender]
        delivered = 0
        failed = []
        for peer, peer_lock in recipients:
            try:
                with peer_lock:
                    peer.send(payload)
                delivered += 1
            except ConnectionClosed:
                failed.append(peer)
        if failed:
            with peers_lock:
                for peer in failed:
                    peers.pop(peer, None)
        sender.send(json.dumps({
            "type": "relay_ack",
            "recipients": delivered,
        }))

    @staticmethod
    def _available_port(services: list[dict[str, Any]]) -> int:
        reserved = {service["port"] for service in services}
        port = 8765
        while port <= 65535 and port in reserved:
            port += 1
        if port > 65535:
            raise RuntimeError("Kullanılabilir bir yerel WebSocket portu kalmadı.")
        return port

    def open_create_gui(self) -> None:
        try:
            import tkinter as tk
            from tkinter import messagebox, ttk
        except ImportError as error:
            raise RuntimeError(f"WebSocket arayüzü açılamadı: {error}") from error

        root = tk.Tk()
        root.title("Yeni yerel WebSocket oluştur")
        root.geometry("560x460")
        root.minsize(500, 420)
        self._build_service_form(root, tk, ttk, messagebox, root, creating=True)
        root.mainloop()

    def open_settings_gui(self) -> None:
        try:
            import tkinter as tk
            from tkinter import messagebox, simpledialog, ttk
        except ImportError as error:
            raise RuntimeError(f"WebSocket ayarları açılamadı: {error}") from error

        root = tk.Tk()
        root.title("Easy Windows Tools · Yerel WebSocket Merkezi")
        root.geometry("1050x640")
        root.minsize(850, 520)
        style = ttk.Style(root)
        if "vista" in style.theme_names():
            style.theme_use("vista")

        heading = ttk.Label(
            root,
            text="Yerel WebSocket bağlantıları",
            font=("Segoe UI", 16, "bold"),
        )
        heading.pack(anchor="w", padx=18, pady=(16, 4))
        ttk.Label(
            root,
            text=(
                "Yalnızca bu bilgisayarda (127.0.0.1) dinler; internet erişimi veya "
                "ağ tüneli oluşturmaz. PIN'ler veritabanında geri döndürülemez biçimde saklanır."
            ),
            wraplength=980,
        ).pack(anchor="w", padx=18, pady=(0, 12))

        columns = ("connection", "address", "status", "clients")
        tree = ttk.Treeview(root, columns=columns, show="tree headings", height=16)
        tree.heading("#0", text="WebSocket")
        tree.heading("connection", text="Bağlantı türü")
        tree.heading("address", text="Yerel adres")
        tree.heading("status", text="Durum")
        tree.heading("clients", text="Bağlantı sınırı")
        tree.column("#0", width=245)
        tree.column("connection", width=210)
        tree.column("address", width=225)
        tree.column("status", width=115, anchor="center")
        tree.column("clients", width=120, anchor="center")
        tree.pack(fill="both", expand=True, padx=18, pady=4)

        def refresh() -> None:
            selected = tree.selection()
            selected_id = selected[0] if selected else None
            tree.delete(*tree.get_children())
            running = self.running_names()
            for service in self.list_services():
                tree.insert(
                    "", "end",
                    iid=str(service["id"]),
                    text=service["name"],
                    values=(
                        self.CONNECTION_TYPES[service["connection_type"]],
                        f"ws://127.0.0.1:{service['port']}",
                        "Çalışıyor" if service["name"] in running else "Durduruldu",
                        service["max_connections"],
                    ),
                )
            if selected_id and tree.exists(selected_id):
                tree.selection_set(selected_id)

        def selected_service() -> dict[str, Any] | None:
            selection = tree.selection()
            return self.get_service_by_id(int(selection[0])) if selection else None

        def add_service() -> None:
            self._build_service_form(
                root, tk, ttk, messagebox, root, creating=True,
                on_saved=refresh,
            )

        def edit_service() -> None:
            service = selected_service()
            if service is None:
                messagebox.showinfo("WebSocket seçin", "Önce düzenlenecek kaydı seçin.", parent=root)
                return
            self._build_service_form(
                root, tk, ttk, messagebox, root, creating=False,
                service=service, on_saved=refresh,
            )

        def start_selected() -> None:
            service = selected_service()
            if service is None:
                messagebox.showinfo("WebSocket seçin", "Önce başlatılacak kaydı seçin.", parent=root)
                return
            try:
                result = self.start_service(service["name"])
            except (OSError, RuntimeError, ValueError) as error:
                messagebox.showerror("Başlatılamadı", str(error), parent=root)
                refresh()
                return
            refresh()
            messagebox.showinfo("WebSocket çalışıyor", result, parent=root)

        def stop_selected() -> None:
            service = selected_service()
            if service is None:
                messagebox.showinfo("WebSocket seçin", "Önce durdurulacak kaydı seçin.", parent=root)
                return
            try:
                result = self.stop_service(service["id"])
            except (OSError, RuntimeError, ValueError) as error:
                messagebox.showerror("Durdurulamadı", str(error), parent=root)
                refresh()
                return
            refresh()
            messagebox.showinfo("WebSocket durduruldu", result, parent=root)

        def rotate_selected_pin() -> None:
            service = selected_service()
            if service is None:
                messagebox.showinfo("WebSocket seçin", "Önce PIN'i yenilenecek kaydı seçin.", parent=root)
                return
            if not messagebox.askyesno(
                "PIN'i yenile",
                f"{service['name']} için mevcut PIN geçersiz kılınacak.\nDevam edilsin mi?",
                parent=root,
            ):
                return
            try:
                pin = self.rotate_pin(service["id"])
            except (OSError, RuntimeError, ValueError) as error:
                messagebox.showerror("PIN yenilenemedi", str(error), parent=root)
                return
            self._show_new_pin(root, tk, ttk, messagebox, service, pin)

        def delete_selected() -> None:
            service = selected_service()
            if service is None:
                messagebox.showinfo("WebSocket seçin", "Önce silinecek kaydı seçin.", parent=root)
                return
            if not messagebox.askyesno(
                "WebSocket kaydını sil",
                f"'{service['name']}' kaydı silinsin ve çalışıyorsa durdurulsun mu?",
                parent=root,
            ):
                return
            try:
                self.delete_service(service["id"])
            except (OSError, RuntimeError, ValueError) as error:
                messagebox.showerror("Silinemedi", str(error), parent=root)
                return
            refresh()

        actions = ttk.Frame(root)
        actions.pack(fill="x", padx=18, pady=12)
        for label, callback in (
            ("Yeni bağlantı oluştur", add_service),
            ("Ayarları düzenle", edit_service),
            ("Başlat", start_selected),
            ("Durdur", stop_selected),
            ("PIN'i yenile", rotate_selected_pin),
            ("Sil", delete_selected),
            ("Yenile", refresh),
        ):
            ttk.Button(actions, text=label, command=callback).pack(
                side="left", padx=(0, 6),
            )
        refresh()
        root.after(1500, lambda: refresh() if root.winfo_exists() else None)

        def update_status() -> None:
            if root.winfo_exists():
                refresh()
                root.after(1500, update_status)

        root.after(1500, update_status)
        root.mainloop()

    def _build_service_form(
        self,
        parent: Any,
        tk: Any,
        ttk: Any,
        messagebox: Any,
        root: Any,
        *,
        creating: bool,
        service: dict[str, Any] | None = None,
        on_saved: Callable[[], None] | None = None,
    ) -> None:
        form = tk.Toplevel(parent)
        form.title("Yeni yerel WebSocket oluştur" if creating else "WebSocket ayarlarını düzenle")
        form.geometry("570x480")
        form.resizable(False, False)
        form.transient(parent)
        form.grab_set()

        body = ttk.Frame(form, padding=18)
        body.pack(fill="both", expand=True)
        ttk.Label(body, text="Bağlantı adı").grid(row=0, column=0, sticky="w", pady=7)
        name_var = tk.StringVar(value=service["name"] if service else "")
        name_entry = ttk.Entry(body, textvariable=name_var, width=42)
        name_entry.grid(row=0, column=1, sticky="ew", pady=7)

        ttk.Label(body, text="Bağlantı türü").grid(row=1, column=0, sticky="w", pady=7)
        type_var = tk.StringVar(
            value=service["connection_type"] if service else "server",
        )
        type_box = ttk.Combobox(
            body,
            textvariable=type_var,
            values=tuple(self.CONNECTION_TYPES),
            state="readonly",
            width=39,
        )
        type_box.grid(row=1, column=1, sticky="ew", pady=7)

        ttk.Label(body, text="Port").grid(row=2, column=0, sticky="w", pady=7)
        port_var = tk.StringVar(
            value=str(
                service["port"] if service
                else self._available_port(self.list_services())
            ),
        )
        ttk.Spinbox(
            body, textvariable=port_var, from_=1024, to=65535, width=12,
        ).grid(row=2, column=1, sticky="w", pady=7)

        ttk.Label(body, text="Eşzamanlı bağlantı sınırı").grid(
            row=3, column=0, sticky="w", pady=7,
        )
        max_var = tk.IntVar(value=service["max_connections"] if service else 10)
        ttk.Spinbox(
            body, textvariable=max_var, from_=1, to=100, width=12,
        ).grid(row=3, column=1, sticky="w", pady=7)

        ttk.Label(body, text="Dinleme adresi").grid(row=4, column=0, sticky="w", pady=7)
        ttk.Label(
            body, text="127.0.0.1 — yalnızca bu bilgisayar",
        ).grid(row=4, column=1, sticky="w", pady=7)

        details = (
            "Uygulama sunucusu: istemci PIN ile doğrulanır; JSON istekleri Python "
            "işleyicisine iletilir ve JSON yanıtı döner.\n\n"
            "Yerel tünel / aktarıcı: aynı PIN ile doğrulanan istemciler arasında "
            "JSON mesajlarını aktarır.\n\n"
            "İlk mesaj: {\"type\":\"authenticate\",\"pin\":\"11 haneli PIN\"}\n"
            "Sunucu yalnızca 127.0.0.1 üzerinde dinler; başka bilgisayarlara veya "
            "internete port açmaz. PIN yalnızca oluşturma/yenileme anında gösterilir."
        )
        ttk.Label(body, text=details, wraplength=520, justify="left").grid(
            row=5, column=0, columnspan=2, sticky="w", pady=(18, 10),
        )
        body.columnconfigure(1, weight=1)

        def save() -> None:
            try:
                port = int(port_var.get())
                max_connections = int(max_var.get())
                if creating:
                    saved, pin = self.create_service(
                        name_var.get(), type_var.get(), port, max_connections,
                    )
                else:
                    assert service is not None
                    saved = self.update_service(
                        service["id"], name_var.get(), type_var.get(),
                        port, max_connections,
                    )
                    pin = ""
            except (OSError, RuntimeError, ValueError, sqlite3.Error) as error:
                messagebox.showerror("Ayarlar kaydedilemedi", str(error), parent=form)
                return
            form.destroy()
            if on_saved:
                on_saved()
            if creating:
                self._show_new_pin(root, tk, ttk, messagebox, saved, pin)

        footer = ttk.Frame(body)
        footer.grid(row=6, column=0, columnspan=2, sticky="e", pady=(14, 0))
        ttk.Button(footer, text="İptal", command=form.destroy).pack(side="right", padx=(8, 0))
        ttk.Button(footer, text="Kaydet", command=save).pack(side="right")
        name_entry.focus_set()

    @staticmethod
    def _show_new_pin(
        parent: Any,
        tk: Any,
        ttk: Any,
        messagebox: Any,
        service: dict[str, Any],
        pin: str,
    ) -> None:
        dialog = tk.Toplevel(parent)
        dialog.title("WebSocket PIN'iniz")
        dialog.geometry("500x260")
        dialog.resizable(False, False)
        dialog.transient(parent)
        dialog.grab_set()
        content = ttk.Frame(dialog, padding=20)
        content.pack(fill="both", expand=True)
        ttk.Label(
            content,
            text=f"{service['name']} için 11 haneli PIN:",
            font=("Segoe UI", 11),
        ).pack(pady=(4, 8))
        pin_var = tk.StringVar(value=pin)
        entry = ttk.Entry(
            content, textvariable=pin_var, justify="center",
            font=("Consolas", 20, "bold"), state="readonly",
            width=18,
        )
        entry.pack(pady=6)
        ttk.Label(
            content,
            text=(
                "PIN güvenli şekilde sadece şimdi gösterilir. Kopyalayıp saklayın; "
                "kaybolursa ayarlardan yeni PIN üretmeniz gerekir."
            ),
            wraplength=440,
            justify="center",
        ).pack(pady=8)

        actions = ttk.Frame(content)
        actions.pack(pady=8)

        def copy_pin() -> None:
            try:
                dialog.clipboard_clear()
                dialog.clipboard_append(pin)
                dialog.update_idletasks()
            except tk.TclError as error:
                messagebox.showerror("Kopyalanamadı", str(error), parent=dialog)
                return
            messagebox.showinfo("PIN kopyalandı", "PIN panoya kopyalandı.", parent=dialog)

        ttk.Button(actions, text="PIN'i kopyala", command=copy_pin).pack(
            side="left", padx=5,
        )
        ttk.Button(actions, text="Kapat", command=dialog.destroy).pack(
            side="left", padx=5,
        )


# Windows Terminal UTF-8 Desteği
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

# Rich Kontrolü
try:
    from rich.console import Console
    from rich.panel import Panel
    from rich.prompt import Prompt
    from rich.syntax import Syntax
    from rich.table import Table
    from rich.text import Text
    from rich.live import Live
    from rich.progress import Progress, BarColumn, TextColumn, SpinnerColumn
    from rich.box import ROUNDED, HEAVY, SIMPLE
    from rich.theme import Theme
    HAS_RICH = True
except ImportError:
    HAS_RICH = False

# Prompt Toolkit Kontrolü (Fallback Korumalı)
HAS_PROMPT_TOOLKIT = False
try:
    from prompt_toolkit import PromptSession
    from prompt_toolkit.completion import WordCompleter
    from prompt_toolkit.styles import Style as PtStyle
    from prompt_toolkit.history import InMemoryHistory
    from prompt_toolkit.shortcuts import radiolist_dialog
    HAS_PROMPT_TOOLKIT = True
except ImportError:
    pass

# Windows API Sabitleri
APP_NAME = "Easy Windows Tools"
APP_VERSION = "2.2.0"
ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001
ES_DISPLAY_REQUIRED = 0x00000002

# Claude Code Özel Renk Paleti
CLAUDE_THEME = {
    "primary": "#E07A5F",      # Claude Terracotta / Orange
    "secondary": "#818CF8",    # Claude Indigo / Purple
    "accent": "#38BDF8",       # Cyber Sky Blue
    "success": "#34D399",      # Mint Green
    "warning": "#FBBF24",      # Amber Yellow
    "error": "#F87171",        # Crimson Red
    "dim": "#64748B",          # Slate Gray
    "bright": "#F8FAFC",       # Cloud White
    "bg_subtle": "#1E293B",    # Dark Slate
}


class KeepAwakeApp:
    def __init__(self):
        self.enabled = False
        self.keep_display_on = False
        self.supported = os.name == "nt"
        self.session_start_time = None
        self.total_awake_seconds = 0
        self.completed_sessions = 0
        self.prompt_session = None

        if HAS_RICH:
            custom_theme = Theme({
                "claude.primary": CLAUDE_THEME["primary"],
                "claude.secondary": CLAUDE_THEME["secondary"],
                "claude.accent": CLAUDE_THEME["accent"],
                "claude.success": CLAUDE_THEME["success"],
                "claude.warning": CLAUDE_THEME["warning"],
                "claude.error": CLAUDE_THEME["error"],
                "claude.dim": CLAUDE_THEME["dim"],
                "claude.bright": CLAUDE_THEME["bright"],
            })
            self.console = Console(theme=custom_theme)
        else:
            self.console = None

        self.mcp_manager = MCPManager(console=self.console)
        self.websocket_manager = LocalWebSocketManager()
        self.recording_settings = ScreenRecorder.load_settings()

        # Prompt toolkit oturumunu güvenle başlat
        if HAS_PROMPT_TOOLKIT and sys.stdin.isatty():
            try:
                commands = [
                    "/start", "başlat", "baslat",
                    "/start 15dk", "başlat 15dk", "başlat 30dk", "başlat 1saat",
                    "/stop", "durdur", "stop",
                    "/pomodoro", "pomodoro",
                    "/meeting", "toplantı", "toplanti",
                    "/display on", "ekran açık", "ekran acik",
                    "/display off", "ekran kapalı", "ekran kapali",
                    "/run", "çalıştır", "calistir",
                    "/record", "/record ayarlar", "/record 30dk",
                    "/record --fps 30", "/record --resolution 1280x720",
                    '/record --microphone "Mikrofon adı"',
                    '/record --system-audio "Stereo Mix"',
                    "/ekran", "/ekran liste", "/ekran 1", "/ekran 2", "ekran kaydet",
                    "/MCP create", "/MCP ayarlar", "/my MCP's",
                    ">start MCP", ">stop MCP",
                    "/websocket başlat", "/websocket start",
                    "/websocket başlat <ad>", "/websocket start <name>",
                    ">websocket ayarlar", "-websocket create",
                    "-websocket yapmak",
                    "/license", "lisans",
                    "/status", "durum",
                    "/help", "yardım", "yardim",
                    "/clear", "temizle",
                    "/exit", "çıkış", "cikis", "quit"
                ]
                self.completer = WordCompleter(commands, ignore_case=True, match_middle=True)
                self.pt_style = PtStyle.from_dict({
                    'prompt': '#E07A5F bold',
                    'symbol': '#38BDF8',
                })
                self.prompt_session = PromptSession(
                    history=InMemoryHistory(),
                    completer=self.completer,
                    style=self.pt_style
                )
            except Exception:
                self.prompt_session = None

        if self.supported:
            try:
                self.set_execution_state = ctypes.WinDLL("kernel32").SetThreadExecutionState
                self.set_execution_state.argtypes = [wintypes.DWORD]
                self.set_execution_state.restype = wintypes.DWORD
            except Exception:
                self.supported = False

    def _set_awake(self, active):
        if not self.supported:
            return
        flags = ES_CONTINUOUS
        if active:
            flags |= ES_SYSTEM_REQUIRED
            if self.keep_display_on:
                flags |= ES_DISPLAY_REQUIRED
        result = self.set_execution_state(flags)
        if result == 0:
            raise ctypes.WinError()

    def set_enabled(self, enabled, silent=False):
        if not self.supported:
            self._print_error("Bu uygulama yalnızca Windows üzerinde çalışmaktadır.")
            return False

        if self.enabled == enabled:
            if not silent:
                state_str = "etkin" if enabled else "devre dışı"
                self._print_info(f"Uyanık tutma modu zaten {state_str}.")
            return True

        try:
            self._set_awake(enabled)
            self.enabled = enabled
            if enabled:
                self.session_start_time = time.time()
            else:
                if self.session_start_time:
                    self.total_awake_seconds += int(time.time() - self.session_start_time)
                    self.session_start_time = None
                    self.completed_sessions += 1
            if not silent:
                self.show_status_badge()
            return True
        except OSError as error:
            self._print_error(f"Windows güç yönetimi API hatası: {error}")
            return False

    def set_display(self, keep_on):
        if self.keep_display_on == keep_on:
            self._print_info(f"Ekran modu zaten {'AÇIK' if keep_on else 'KAPALI'}.")
            return

        self.keep_display_on = keep_on
        if self.enabled:
            try:
                self._set_awake(True)
            except OSError as error:
                self.keep_display_on = not keep_on
                self._print_error(f"Ekran durumu güncellenemedi: {error}")
                return

        state = "[bold #34D399]HER ZAMAN AÇIK[/]" if keep_on else "[dim]SİSTEME BAĞLI (KAPANABİLİR)[/]"
        if HAS_RICH:
            self.console.print(f"  [claude.dim]↳[/] Ekran uyanıklığı: {state}")
            self.show_status_badge()
        else:
            print(f"  Ekran durumu: {'Açık' if keep_on else 'Kapalı'}")

    def parse_duration(self, value):
        cleaned = value.strip().lower()
        match = re.fullmatch(r"(\d+)\s*(dk|dakika|m|min|sn|saniye|s|saat|h|hour)?", cleaned)
        if not match:
            return None
        amount = int(match.group(1))
        unit = match.group(2) or "dk"
        multiplier = 3600 if unit in {"saat", "h", "hour"} else 1 if unit in {"sn", "saniye", "s"} else 60
        seconds = amount * multiplier
        return seconds if seconds > 0 else None

    # UI Bileşenleri
    def print_banner(self):
        if not HAS_RICH:
            print("\n" + "=" * 50)
            print(f"   ✦ {APP_NAME.upper()}")
            print("=" * 50 + "\n")
            self.show_license_notice()
            return

        # Claude Code Stili Minimalist / Şık Banner
        logo_text = (
            f"[bold #E07A5F] ▟█▙ [/][bold #38BDF8]{APP_NAME.upper()}[/] [dim #64748B]v{APP_VERSION} │ Windows Power & Display Tools[/]"
        )

        header_panel = Panel(
            logo_text,
            box=ROUNDED,
            border_style=CLAUDE_THEME["primary"],
            padding=(0, 2),
            subtitle="[dim]Komutlar için '/help' veya 'yardım' yazabilirsiniz[/dim]",
            subtitle_align="right"
        )
        self.console.print()
        self.console.print(header_panel)
        self.show_license_notice()
        self.show_status_badge()

    def show_license_notice(self):
        notice = (
            f"Copyright (C) 2026 Robin Güneş\n"
            "This program comes with ABSOLUTELY NO WARRANTY.\n"
            "This is free software under GNU GPL version 3; redistribution is permitted under its terms.\n"
            "License text: https://www.gnu.org/licenses/gpl-3.0.html"
        )
        if HAS_RICH:
            self.console.print(Panel(notice, title="License Notice", border_style=CLAUDE_THEME["dim"]))
        else:
            print(notice)

    def show_status_badge(self):
        if not HAS_RICH:
            durum = "ETKİN (Uyanık)" if self.enabled else "KAPALI (Normal)"
            ekran = "Açık" if self.keep_display_on else "Sisteme Bağlı"
            print(f"  [Durum: {durum} | Ekran: {ekran}]")
            return

        table = Table(box=ROUNDED, show_header=False, border_style="dim", padding=(0, 1))
        table.add_column("Key", style="dim")
        table.add_column("Value")
        table.add_column("Key2", style="dim")
        table.add_column("Value2")

        if self.enabled:
            status_text = "[bold #34D399]● AKTİF[/] [dim](Uyku Engellendi)[/]"
        else:
            status_text = "[dim #64748B]○ PASİF[/] [dim](Normal Güç Planı)[/]"

        if self.keep_display_on:
            display_text = "[bold #38BDF8]🖥️  AÇIK[/] [dim](Kapanmaz)[/]"
        else:
            display_text = "[dim #64748B]🖥️  KAPALI[/] [dim](Normal Kararma)[/]"

        total_mins = (self.total_awake_seconds + (int(time.time() - self.session_start_time) if self.enabled else 0)) // 60
        stats_text = f"[bold white]{total_mins}[/] [dim]dk[/]"

        table.add_row("Durum:", status_text, "Ekran:", display_text)
        table.add_row("Oturumlar:", f"[bold white]{self.completed_sessions}[/] [dim]tamamlandı[/]", "Toplam Koruma:", stats_text)

        self.console.print(Panel(table, title="[dim]Sistem & Güç Durumu[/dim]", title_align="left", box=ROUNDED, border_style="dim"))

    def show_help(self):
        if not HAS_RICH:
            print("\nKomutlar:\n  başlat [süre] - Uyku engelini başlatır\n  durdur - Uyanık kalmayı kapatır\n  ekran açık/kapalı - Ekran korumasını ayarlar\n  /ekran [numara] - Kayıt için ekranı seçer; /ekran liste ile görüntüle\n  pomodoro - 25 dk odak oturumu\n  toplantı - 60 dk toplantı oturumu\n  /record [süre] - Ekranı MP4 kaydeder; Ctrl+C ile durdurur\n  /record ayarlar - FPS, çözünürlük, mikrofon ve sistem sesi ayarlarını açar\n  /record [süre] --microphone \"Aygıt\" --system-audio \"Stereo Mix\" - İki ses kaynağını kayda ekler\n  /record [süre] --no-audio - Bu kayıt için ses yakalamayı kapatır\n  çalıştır <komut> - Komut bitene kadar uyanık tutar\n  /MCP create - Bir uygulama için MCP sunucusu oluşturur\n  /MCP ayarlar - Uygulama kataloğu ve güvenlik GUI'sini açar\n  >start MCP <ad> - Yerel MCP sunucusunu başlatır\n  >stop MCP <ad> - Yerel MCP sunucusunu durdurur\n  /my MCP's [ad] - MCP sunucularını ve yapılandırmasını gösterir\n  lisans /license - Telif, garanti ve lisans bilgisini gösterir\n  durum - Sistem durumunu gösterir\n  çıkış - Uygulamadan çıkar\n")
            print("  /websocket başlat [ad] - Kayıtlı yerel WebSocket'leri başlatır\n  >websocket ayarlar - WebSocket ayar penceresini açar\n  -websocket create - Yeni WebSocket oluşturur\n")
            return

        table = Table(box=ROUNDED, border_style=CLAUDE_THEME["secondary"], padding=(0, 1))
        table.add_column("Kategori", style="bold #E07A5F", width=16)
        table.add_column("Komut / Kısayol", style="bold #38BDF8", width=28)
        table.add_column("Açıklama", style="white")

        table.add_row(
            "⚡ Oturumlar",
            "başlat [dim]/[/] /start",
            "Süresiz uyanık tutma modunu başlatır"
        )
        table.add_row(
            "",
            "başlat <süre> [dim](örn: 30dk, 1h)[/]",
            "Belirtilen süre boyunca uyanık tutar ve otomatik kapanır"
        )
        table.add_row(
            "",
            "pomodoro [dim]/[/] /pomodoro",
            "25 dakikalık odaklanma oturumu başlatır"
        )
        table.add_row(
            "",
            "toplantı [dim]/[/] /meeting",
            "1 saatlik (60 dk) toplantı uyanıklık modu"
        )
        table.add_row(
            "",
            "durdur [dim]/[/] /stop",
            "Uyanık modunu sonlandırır, normal güç planına döner"
        )
        table.add_section()
        table.add_row(
            "🖥️ Ekran",
            "ekran açık [dim]/[/] /display on",
            "Monitörün kararmasını ve kapanmasını önler"
        )
        table.add_row(
            "",
            "ekran kapalı [dim]/[/] /display off",
            "Bilgisayarı açık tutar ancak ekranın kararmasına izin verir"
        )
        table.add_section()
        table.add_row(
            "⏺ Kayıt",
            "/record [süre]",
            "Kaydediciyi mevcut kalite ayarlarıyla başlatır; Ctrl+C ile durdurabilirsiniz"
        )
        table.add_row(
            "",
            "/ekran [numara]",
            "Kayıt için Windows Tanımla numarasıyla ekranı seçer; /ekran liste ile gör"
        )
        table.add_row(
            "",
            "/record ayarlar",
            "FPS, çözünürlük, mikrofon ve sistem sesi aygıtlarını ayarlar"
        )
        table.add_row(
            "",
            "/record 30dk --fps 30 --resolution 1280x720 --monitor 2",
            "Yalnızca bu kayıt için süreyi, kaliteyi ve ekranı değiştirir"
        )
        table.add_row(
            "",
            '/record --microphone "Aygıt" --system-audio "Stereo Mix"',
            "Mikrofonu ve sistem sesi aygıtını tek MP4 kaydında birleştirir"
        )
        table.add_row(
            "",
            "/record --no-audio",
            "Bu kayıt için mikrofon ve sistem sesi yakalamayı kapatır"
        )
        table.add_section()
        table.add_row(
            "🛠️ Araçlar",
            "çalıştır <komut> [dim]/[/] /run <cmd>",
            "Bir komutu / scripti çalıştırır ve bitene dek sistemi uyanık tutar"
        )
        table.add_row(
            "",
            "temizle [dim]/[/] /clear",
            "Terminal ekranını temizler"
        )
        table.add_section()
        table.add_row(
            "🔌 MCP",
            "/MCP create",
            "Yüklü bir Windows uygulaması seçip MCP sunucusu oluşturur"
        )
        table.add_row(
            "",
            "/my MCP's [ad]",
            "Kayıtları ve VS Code / Antigravity yapılandırmasını gösterir"
        )
        table.add_row(
            "",
            "/MCP ayarlar",
            "Yüklü uygulamaları, 100 uygulamalı kataloğu ve uygulama bazlı güvenlik ayarlarını açar"
        )
        table.add_row(
            "",
            ">start MCP <ad>",
            "Seçilen yerel MCP sunucusunu 127.0.0.1 üzerinde başlatır"
        )
        table.add_section()
        table.add_row(
            "🌐 WebSocket",
            "/websocket başlat [ad] [dim]/[/] /websocket start [name]",
            "Tüm yerel bağlantıları veya ada göre bir bağlantıyı başlatır"
        )
        table.add_row(
            "",
            ">websocket ayarlar",
            "Yerel WebSocket kayıtlarını ve ayrıntılı ayarlarını yönetir"
        )
        table.add_row(
            "",
            "-websocket create [dim]/[/] -websocket yapmak",
            "Yeni bir yerel WebSocket sunucusu veya tünel kaydı oluşturur"
        )
        table.add_section()
        table.add_row(
            "ℹ️ Genel",
            "durum [dim]/[/] /status",
            "Geçerli uyanıklık ve ekran durum panelini gösterir"
        )
        table.add_row(
            "",
            "lisans [dim]/[/] /license",
            "Telif, garanti ve GNU GPL v3 lisans bilgisini gösterir"
        )
        table.add_row(
            "",
            "yardım [dim]/[/] /help [dim]veya[/] ?",
            "Bu komut kılavuzunu görüntüler"
        )
        table.add_row(
            "",
            "çıkış [dim]/[/] /exit [dim]veya[/] quit",
            "Güç ayarlarını sıfırlayarak uygulamadan çıkar"
        )

        help_panel = Panel(
            table,
            title="[bold #E07A5F]✦ Komut Referans Tablosu[/]",
            subtitle="[dim]Tüm komutlar hem Türkçe hem Slash (/) formatında çalışır[/dim]",
            box=ROUNDED,
            border_style=CLAUDE_THEME["secondary"]
        )
        self.console.print(help_panel)

    def start_timed_session(self, seconds, title="Süreli Oturum"):
        if not self.set_enabled(True, silent=True):
            return

        end_time = datetime.now() + timedelta(seconds=seconds)
        end_str = end_time.strftime("%H:%M:%S")

        if not HAS_RICH:
            print(f"\n  [⚡ {title} Başladı] Bitiş: {end_str}")
            print("  Durdurmak için Ctrl+C tuşlayın.\n")
            try:
                while seconds > 0:
                    mins, secs = divmod(seconds, 60)
                    print(f"\r  Kalan Süre: {mins:02d}:{secs:02d} ", end="", flush=True)
                    time.sleep(1)
                    seconds -= 1
                print("\n  ✓ Süre tamamlandı!")
            except KeyboardInterrupt:
                print("\n  Oturum durduruldu.")
            finally:
                self.set_enabled(False, silent=True)
            return

        # Claude Code Canlı İlerleme Çubuğu & Animasyonlu Oturum
        self.console.print()
        session_info = (
            f"[bold #E07A5F]⏱  {title}[/]  "
            f"[dim]• Bitiş Hedefi:[/] [bold #38BDF8]{end_str}[/]  "
            f"[dim]• Ekran:[/] {'[#34D399]Açık[/]' if self.keep_display_on else '[dim]Kapalı[/]'}\n"
            f"[dim]Erken sonlandırmak için [/][bold white]Ctrl + C[/][dim] tuşlarına basabilirsiniz.[/dim]"
        )
        self.console.print(Panel(session_info, box=ROUNDED, border_style=CLAUDE_THEME["primary"]))

        progress = Progress(
            SpinnerColumn(spinner_name="dots", style="bold #E07A5F"),
            TextColumn("[bold white]{task.description}[/]"),
            BarColumn(bar_width=35, style="dim", complete_style="#E07A5F", finished_style="#34D399"),
            TextColumn("[bold #38BDF8]{task.percentage:>3.0f}%[/]"),
            TextColumn("[dim]• Kalan:[/] [bold yellow]{task.fields[remaining_str]}[/]"),
            console=self.console,
            transient=True
        )

        task_id = progress.add_task(
            "Uyanık Tutuluyor",
            total=seconds,
            remaining_str=f"{seconds // 60:02d}:{seconds % 60:02d}"
        )

        try:
            with progress:
                remaining = seconds
                while remaining > 0:
                    mins, secs = divmod(remaining, 60)
                    hrs, mins = divmod(mins, 60)
                    if hrs > 0:
                        rem_str = f"{hrs:02d}:{mins:02d}:{secs:02d}"
                    else:
                        rem_str = f"{mins:02d}:{secs:02d}"

                    progress.update(task_id, completed=seconds - remaining, remaining_str=rem_str)
                    time.sleep(1)
                    remaining -= 1

                progress.update(task_id, completed=seconds, remaining_str="00:00")

            self.console.print(Panel(
                f"[bold #34D399]✓ Süre Tamamlandı![/] [dim]{title} başarıyla tamamlandı. Güç ayarları normale döndürüldü.[/dim]",
                box=ROUNDED,
                border_style="#34D399"
            ))
        except KeyboardInterrupt:
            self.console.print()
            self.console.print(Panel(
                f"[bold #FBBF24]⚠ Oturum Kesildi[/] [dim]Oturum kullanıcı tarafından erken durduruldu.[/dim]",
                box=ROUNDED,
                border_style="#FBBF24"
            ))
        finally:
            self.set_enabled(False, silent=True)
            self.show_status_badge()

    def run_external_command(self, command_str):
        was_enabled = self.enabled
        if not self.set_enabled(True, silent=True):
            return

        if not HAS_RICH:
            print(f"\n  [Çalıştırılıyor]: {command_str}")
            try:
                start_t = time.time()
                res = subprocess.run(command_str, shell=True)
                dur = time.time() - start_t
                print(f"  [Tamamlandı] Kod: {res.returncode} ({dur:.1f} sn)\n")
            except KeyboardInterrupt:
                print("\n  Komut iptal edildi.")
            finally:
                if not was_enabled:
                    self.set_enabled(False, silent=True)
            return

        cmd_panel = Panel(
            f"[bold #38BDF8]❯[/] [bold white]{command_str}[/]\n"
            f"[dim]Komut çalışırken bilgisayarınız uyanık tutulacaktır.[/dim]",
            title="[bold #E07A5F]● Tool Execution (Subprocess)[/]",
            title_align="left",
            box=ROUNDED,
            border_style=CLAUDE_THEME["secondary"]
        )
        self.console.print(cmd_panel)
        start_t = time.time()

        try:
            result = subprocess.run(command_str, shell=True)
            duration = time.time() - start_t
            
            if result.returncode == 0:
                status_box = f"[bold #34D399]✓ Komut başarıyla tamamlandı[/] [dim](Çıkış kodu: 0, Süre: {duration:.2f}s)[/]"
                border_color = "#34D399"
            else:
                status_box = f"[bold #F87171]✕ Komut hata ile bitti[/] [dim](Çıkış kodu: {result.returncode}, Süre: {duration:.2f}s)[/]"
                border_color = "#F87171"

            self.console.print(Panel(status_box, box=ROUNDED, border_style=border_color))
        except KeyboardInterrupt:
            self.console.print("\n[bold #FBBF24]⚠ Komut yürütmesi kullanıcı tarafından durduruldu.[/]")
        finally:
            if not was_enabled:
                self.set_enabled(False, silent=True)
            self.show_status_badge()

    def _choose_recording_option(self, title, options, current):
        if HAS_PROMPT_TOOLKIT and sys.stdin.isatty():
            from prompt_toolkit.shortcuts import radiolist_dialog

            return radiolist_dialog(
                title=APP_NAME,
                text=f"{title} · Oklarla seç, Enter ile onayla, Escape ile iptal et",
                values=options,
                default=current,
                ok_text="Seç",
                cancel_text="İptal",
            ).run()

        if HAS_RICH:
            table = Table(title=title, border_style=CLAUDE_THEME["primary"])
            table.add_column("#", justify="right", style="dim")
            table.add_column("Seçenek", style="bold")
            for index, (value, label) in enumerate(options, start=1):
                marker = "  · mevcut" if value == current else ""
                table.add_row(str(index), f"{label}{marker}")
            self.console.print(table)
            prompt = self.console.input("Seçenek numarası (Enter mevcut, q iptal): ").strip()
        else:
            for index, (_, label) in enumerate(options, start=1):
                print(f"{index}. {label}")
            prompt = input("Seçenek numarası (Enter mevcut, q iptal): ").strip()

        if prompt.lower() in {"q", "iptal", "cancel"}:
            return None
        if not prompt:
            return current
        try:
            return options[int(prompt) - 1][0]
        except (ValueError, IndexError):
            raise ValueError("Listeden geçerli bir seçenek numarası seçin.")

    def configure_recording(self):
        settings = ScreenRecorder.load_settings()
        fps_options = [
            (15, "15 FPS · Daha küçük dosyalar"),
            (24, "24 FPS · Sinematik"),
            (30, "30 FPS · Akıcı"),
            (60, "60 FPS · En akıcı"),
        ]
        if settings["fps"] not in {value for value, _ in fps_options}:
            fps_options.append((settings["fps"], f"{settings['fps']} FPS · Özel"))
        try:
            fps = self._choose_recording_option("Kare hızı", fps_options, settings["fps"])
            if fps is None:
                return

            current_resolution = settings["resolution"]
            resolution_key = (
                f"{current_resolution[0]}x{current_resolution[1]}"
                if current_resolution else "native"
            )
            resolution_options = [
                ("native", "Monitörün doğal çözünürlüğü"),
                ("1920x1080", "1920 × 1080"),
                ("1600x900", "1600 × 900"),
                ("1280x720", "1280 × 720"),
                ("854x480", "854 × 480"),
                ("custom", "Özel çözünürlük gir"),
            ]
            if resolution_key not in {value for value, _ in resolution_options}:
                resolution_options.append((resolution_key, f"{resolution_key} · Özel (mevcut)"))
            resolution = self._choose_recording_option(
                "Kayıt çözünürlüğü", resolution_options, resolution_key
            )
            if resolution is None:
                return
            if resolution == "custom":
                if HAS_RICH:
                    resolution = self.console.input("Çözünürlük (örn. 1280x720): ").strip()
                else:
                    resolution = input("Çözünürlük (örn. 1280x720): ").strip()

            audio_devices = ScreenRecorder.list_audio_devices()
            if not audio_devices:
                self._print_info(
                    "Windows tarafından kullanılabilir DirectShow ses aygıtı sunulmadı; "
                    "ses kaydı kapalı kalacaktır."
                )
            system_audio_devices = ScreenRecorder.find_system_audio_devices(audio_devices)
            if audio_devices and not system_audio_devices:
                self._print_info(
                    "Sistem sesi için Stereo Mix veya loopback aygıtı bulunamadı; "
                    "Windows/Realtek ses ayarlarından etkinleştirebilirsiniz."
                )
            microphone_options = [("off", "Kapalı")]
            microphone_options.extend(
                (device, device) for device in audio_devices
            )
            microphone = self._choose_recording_option(
                "Mikrofon",
                microphone_options,
                settings["microphone"]
                if settings["microphone"] in audio_devices else "off",
            )
            if microphone is None:
                return

            system_audio_options = [("off", "Kapalı")]
            system_audio_options.extend(
                (device, device) for device in system_audio_devices
            )
            system_audio = self._choose_recording_option(
                "Sistem sesi aygıtı (örn. Stereo Mix)",
                system_audio_options,
                settings["system_audio"]
                if settings["system_audio"] in system_audio_devices else "off",
            )
            if system_audio is None:
                return

            saved = ScreenRecorder.save_settings(
                fps, resolution, microphone=microphone, system_audio=system_audio
            )
        except (OSError, RuntimeError, subprocess.SubprocessError, ValueError) as error:
            self._print_error(f"Kayıt ayarları kaydedilemedi: {error}")
            return

        self.recording_settings = saved
        resolution_label = (
            f"{saved['resolution'][0]}x{saved['resolution'][1]}"
            if saved["resolution"] else "Monitörün doğal çözünürlüğü"
        )
        microphone_label = saved["microphone"] or "kapalı"
        system_audio_label = saved["system_audio"] or "kapalı"
        self._print_info(
            f"Varsayılan kayıt: {saved['fps']} FPS · {resolution_label} · "
            f"Mikrofon: {microphone_label} · Sistem sesi: {system_audio_label}"
        )

    def select_recording_screen(self, selection=None):
        try:
            monitors = ScreenRecorder.list_monitors()
            settings = ScreenRecorder.load_settings()
        except (OSError, RuntimeError) as error:
            self._print_error(f"Ekranlar listelenemedi: {error}")
            return

        if not monitors:
            self._print_error("Kayıt yapılabilecek ekran bulunamadı.")
            return

        if selection is None or selection.lower() in {"liste", "list", "?"}:
            if HAS_RICH:
                table = Table(title="Kayıt için ekran seç", border_style=CLAUDE_THEME["primary"])
                table.add_column("No.", justify="right", style="bold #38BDF8")
                table.add_column("Ekran")
                table.add_column("Çözünürlük", justify="right")
                table.add_column("Durum")
                for monitor in monitors:
                    selected = monitor["number"] == settings["monitor"]
                    status = "KAYITTA" if selected else "Ana ekran" if monitor["is_primary"] else ""
                    name = f"Ekran {monitor['number']}"
                    if monitor["name"] and monitor["name"] != name:
                        name += f" · {monitor['name']}"
                    table.add_row(
                        str(monitor["number"]),
                        name,
                        f"{monitor['width']} × {monitor['height']}",
                        status,
                    )
                self.console.print(table)
                self._print_info(
                    f"Seçili ekran: {settings['monitor']}. Değiştirmek için /ekran <numara> yazın."
                )
            else:
                for monitor in monitors:
                    marker = " (seçili)" if monitor["number"] == settings["monitor"] else ""
                    primary = " (ana ekran)" if monitor["is_primary"] else ""
                    print(
                        f"{monitor['number']}. Ekran {monitor['number']} · "
                        f"{monitor['width']}x{monitor['height']}{primary}{marker}"
                    )
                print(f"Seçili ekran: {settings['monitor']}. Değiştirmek için /ekran <numara> yazın.")
            return

        try:
            monitor_number = ScreenRecorder.validate_monitor_number(selection)
        except ValueError as error:
            self._print_error(str(error))
            return
        if monitor_number > len(monitors):
            self._print_error(
                f"Ekran {monitor_number} bulunamadı. /ekran liste komutuyla geçerli numaraları görün."
            )
            return

        try:
            saved = ScreenRecorder.save_settings(
                settings["fps"], settings["resolution"], monitor_number=monitor_number
            )
        except (OSError, ValueError) as error:
            self._print_error(f"Ekran seçimi kaydedilemedi: {error}")
            return
        self.recording_settings = saved
        monitor = monitors[monitor_number - 1]
        self._print_info(
            f"Kayıt ekranı {monitor_number} olarak ayarlandı "
            f"({monitor['width']}x{monitor['height']})."
        )

    def _parse_record_options(self, command):
        arguments = shlex.split(command)[1:]
        options = {
            "duration": None,
            "fps": None,
            "resolution": None,
            "monitor_number": None,
            "microphone": None,
            "system_audio": None,
        }
        index = 0
        while index < len(arguments):
            argument = arguments[index]
            if argument.startswith("--fps="):
                options["fps"] = ScreenRecorder.validate_fps(argument.partition("=")[2])
            elif argument == "--fps":
                index += 1
                if index >= len(arguments):
                    raise ValueError("--fps seçeneğinden sonra 1-60 arasında FPS değeri girin.")
                options["fps"] = ScreenRecorder.validate_fps(arguments[index])
            elif argument.startswith("--resolution="):
                value = argument.partition("=")[2]
                ScreenRecorder.parse_resolution(value)
                options["resolution"] = value
            elif argument == "--resolution":
                index += 1
                if index >= len(arguments):
                    raise ValueError("--resolution seçeneğinden sonra 1280x720 gibi bir değer girin.")
                ScreenRecorder.parse_resolution(arguments[index])
                options["resolution"] = arguments[index]
            elif argument.startswith("--monitor=") or argument.startswith("--screen="):
                options["monitor_number"] = ScreenRecorder.validate_monitor_number(argument.partition("=")[2])
            elif argument in {"--monitor", "--screen"}:
                index += 1
                if index >= len(arguments):
                    raise ValueError("--monitor seçeneğinden sonra ekran numarası girin.")
                options["monitor_number"] = ScreenRecorder.validate_monitor_number(arguments[index])
            elif argument in {"--microphone", "--system-audio"}:
                option = "microphone" if argument == "--microphone" else "system_audio"
                index += 1
                if index >= len(arguments):
                    raise ValueError(f"{argument} seçeneğinden sonra aygıt adı girin.")
                options[option] = arguments[index]
            elif argument.startswith("--microphone="):
                options["microphone"] = argument.partition("=")[2]
            elif argument.startswith("--system-audio="):
                options["system_audio"] = argument.partition("=")[2]
            elif argument == "--no-audio":
                options["microphone"] = "off"
                options["system_audio"] = "off"
            elif argument.startswith("--"):
                raise ValueError(f"Bilinmeyen ekran kaydı seçeneği: {argument}")
            elif options["duration"] is None:
                options["duration"] = self.parse_duration(argument)
                if options["duration"] is None:
                    raise ValueError(f"Geçersiz kayıt süresi: {argument}")
            else:
                raise ValueError(f"Beklenmeyen ekran kaydı değeri: {argument}")
            index += 1
        return options

    def record_screen(
        self, duration=None, fps=None, resolution=None, monitor_number=None,
        microphone=None, system_audio=None,
    ):
        saved_settings = ScreenRecorder.load_settings()
        try:
            selected_fps = ScreenRecorder.validate_fps(
                fps if fps is not None else saved_settings["fps"]
            )
            selected_resolution = ScreenRecorder.parse_resolution(
                resolution if resolution is not None else saved_settings["resolution"]
            )
            selected_monitor = ScreenRecorder.validate_monitor_number(
                monitor_number if monitor_number is not None else saved_settings["monitor"]
            )
            selected_microphone = (
                saved_settings["microphone"] if microphone is None else microphone
            )
            selected_system_audio = (
                saved_settings["system_audio"] if system_audio is None else system_audio
            )
        except ValueError as error:
            self._print_error(str(error))
            return

        was_enabled = self.enabled
        was_display_on = self.keep_display_on
        self.keep_display_on = True

        try:
            if not self.set_enabled(True, silent=True):
                return
            self._set_awake(True)
            resolution_label = (
                f"{selected_resolution[0]}x{selected_resolution[1]}"
                if selected_resolution else "Monitör çözünürlüğü"
            )
            if HAS_RICH:
                duration_text = f"En fazla {duration} saniye" if duration else "Süre sınırı yok"
                audio_labels = [
                    label for label, device in (
                        ("Mikrofon", selected_microphone),
                        ("Sistem sesi", selected_system_audio),
                    ) if device and device != "off"
                ]
                audio_text = " · ".join(audio_labels) if audio_labels else "Sessiz"
                self.console.print(Panel(
                    f"[bold #F87171]● KAYIT[/]  [dim]Ekran {selected_monitor} · {selected_fps} FPS · {resolution_label} · {audio_text} · MP4 / H.264[/]\n"
                    f"[dim]{duration_text} · Durdurmak için Ctrl+C[/]",
                    title="Ekran Kaydedici",
                    border_style=CLAUDE_THEME["primary"],
                ))
            else:
                print("Ekran kaydı başladı. Durdurmak için Ctrl+C kullanın.")

            recorder = ScreenRecorder(
                fps=selected_fps,
                resolution=selected_resolution,
                monitor_number=selected_monitor,
                microphone=selected_microphone,
                system_audio=selected_system_audio,
            )
            output_path, elapsed, stopped_by_user = recorder.record(
                duration, monitor_number=selected_monitor
            )
            if HAS_RICH:
                result = "Kayıt durduruldu." if stopped_by_user else "Kayıt tamamlandı."
                self.console.print(Panel(
                    f"[bold #34D399]✓ {result}[/]\n[white]{output_path}[/]\n"
                    f"[dim]Süre: {elapsed:.1f} saniye[/]",
                    border_style=CLAUDE_THEME["success"],
                ))
            else:
                print(f"Kayıt kaydedildi: {output_path} ({elapsed:.1f} sn)")
        except (OSError, RuntimeError, subprocess.SubprocessError, ValueError) as error:
            self._print_error(f"Ekran kaydı başlatılamadı: {error}")
        except KeyboardInterrupt:
            self._print_info("Ekran kaydı başlatılmadan iptal edildi.")
        finally:
            self.keep_display_on = was_display_on
            if was_enabled:
                try:
                    self._set_awake(True)
                except OSError as error:
                    self._print_error(f"Önceki güç durumu geri yüklenemedi: {error}")
            elif self.enabled:
                self.set_enabled(False, silent=True)
            self.show_status_badge()

    def _print_error(self, msg):
        if HAS_RICH:
            self.console.print(f"  [bold #F87171]✕ Hata:[/] {msg}")
        else:
            print(f"  Hata: {msg}")

    def _print_info(self, msg):
        if HAS_RICH:
            self.console.print(f"  [dim #38BDF8]ℹ[/] [dim]{msg}[/]")
        else:
            print(f"  Bilgi: {msg}")

    def get_user_input(self):
        if self.prompt_session:
            try:
                prompt_symbol = [('class:prompt', '╭─ '), ('class:symbol', f'⚡ {APP_NAME.lower().replace(" ", "-")}\n'), ('class:prompt', '╰─ ❯ ')]
                return self.prompt_session.prompt(prompt_symbol).strip()
            except Exception:
                pass
        
        # Standart input fallback
        if HAS_RICH:
            return self.console.input(f"\n[bold #E07A5F]╭─ ⚡ {APP_NAME.lower().replace(' ', '-')}[/]\n[bold #E07A5F]╰─ ❯ [/]").strip()
        else:
            return input("\nuyanık ❯ ").strip()

    def run(self):
        if HAS_RICH:
            try:
                self.console.clear()
            except Exception:
                pass
        self.print_banner()

        if not self.supported:
            self._print_info(
                "Uyku ve ekran güç kontrolleri Windows gerektirir. "
                "MCP uygulama merkezi ve bu sistemde algılanan paket yöneticileri kullanılabilir."
            )

        while True:
            try:
                command = self.get_user_input()
            except (EOFError, KeyboardInterrupt):
                self.console.print() if HAS_RICH else print()
                break

            if not command:
                continue

            normalized = command.lower()
            mcp_lookup = re.fullmatch(r"/my\s+mcp(?:'s|s)(?:\s+(.+))?", command, flags=re.IGNORECASE)
            mcp_start = re.fullmatch(r">start\s+mcp\s+(.+)", command, flags=re.IGNORECASE)
            mcp_stop = re.fullmatch(r">stop\s+mcp\s+(.+)", command, flags=re.IGNORECASE)
            websocket_start = re.fullmatch(
                r"/websocket\s+(?:başlat|start|başlat/start)(?:\s+(.+))?",
                command.strip(),
                flags=re.IGNORECASE,
            )

            if normalized in {"-websocket create", "-websocket yapmak", "-websocket create/yapmak"}:
                try:
                    self.websocket_manager.open_create_gui()
                except (OSError, RuntimeError, ValueError) as error:
                    self._print_error(f"WebSocket oluşturma penceresi açılamadı: {error}")
            elif normalized in {">websocket ayarlar", ">websocket settings"}:
                try:
                    self.websocket_manager.open_settings_gui()
                except (OSError, RuntimeError, ValueError) as error:
                    self._print_error(f"WebSocket ayar penceresi açılamadı: {error}")
            elif websocket_start:
                service_name = websocket_start.group(1)
                if service_name:
                    try:
                        self._print_info(
                            self.websocket_manager.start_service(service_name.strip())
                        )
                    except (OSError, RuntimeError, ValueError) as error:
                        self._print_error(f"WebSocket başlatılamadı: {error}")
                else:
                    results = self.websocket_manager.start_all()
                    for service_name, result in results:
                        if result is None:
                            self._print_error(
                                f"{service_name} WebSocket bağlantısı başlatılamadı."
                            )
                        else:
                            self._print_info(result)
                    if not results:
                        self._print_info(
                            "Kayıtlı WebSocket yok. Oluşturmak için -websocket create yazın."
                        )
            elif normalized in {"/mcp create", "mcp create"}:
                self.mcp_manager.create_interactive()
            elif normalized in {"/mcp ayarlar", "/mcp settings", "mcp ayarlar"}:
                self.mcp_manager.open_settings_gui()
            elif mcp_start:
                try:
                    self._print_info(self.mcp_manager.start_mcp_server(mcp_start.group(1).strip()))
                except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as error:
                    self._print_error(f"MCP sunucusu başlatılamadı: {error}")
            elif mcp_stop:
                try:
                    self._print_info(self.mcp_manager.stop_mcp_server(mcp_stop.group(1).strip()))
                except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as error:
                    self._print_error(f"MCP sunucusu durdurulamadı: {error}")
            elif mcp_lookup:
                self.mcp_manager.show_servers(mcp_lookup.group(1))

            # 1. Başlatma Komutları
            elif normalized in {"başlat süresiz", "baslat suresiz", "/start infinity", "start infinity"}:
                self.set_enabled(True)
            elif normalized.startswith("başlat ") or normalized.startswith("baslat ") or normalized.startswith("/start "):
                parts = command.split(maxsplit=1)
                if len(parts) > 1:
                    duration_text = parts[1]
                    seconds = self.parse_duration(duration_text)
                    if seconds is None:
                        self.console.print("  [bold #FBBF24]⚠ Geçersiz süre biçimi.[/] [dim]Örnekler: 30dk, 45sn, 1saat, 2h[/]") if HAS_RICH else print("  Geçersiz süre.")
                    else:
                        self.start_timed_session(seconds, title=f"Süreli Oturum ({duration_text})")
            elif normalized in {"pomodoro", "/pomodoro", "pomo"}:
                self.start_timed_session(25 * 60, title="🍅 Pomodoro Odak Oturumu (25 dk)")
            elif normalized in {"toplantı", "toplanti", "/meeting", "/toplantı", "/toplanti"}:
                self.start_timed_session(60 * 60, title="📅 Toplantı Oturumu (60 dk)")
            elif normalized in {"başlat", "baslat", "start", "on", "/start"}:
                self.set_enabled(True)

            # 2. Durdurma Komutları
            elif normalized in {"durdur", "stop", "off", "/stop", "pause"}:
                self.set_enabled(False)

            # 3. Ekran Ayarları
            elif normalized in {"ekran açık", "ekran acik", "display on", "/display on", "/display 1"}:
                self.set_display(True)
            elif normalized in {"ekran kapalı", "ekran kapali", "display off", "/display off", "/display 0"}:
                self.set_display(False)
            elif normalized == "/ekran" or normalized.startswith("/ekran "):
                parts = command.split(maxsplit=1)
                self.select_recording_screen(parts[1] if len(parts) == 2 else None)

            # 4. Ekran Kaydı
            elif normalized in {"/record ayarlar", "/record settings", "kayıt ayarları", "kayit ayarlari"}:
                self.configure_recording()
            elif normalized in {"ekran kaydet", "screen record"}:
                self.record_screen()
            elif normalized == "/record" or normalized.startswith("/record "):
                try:
                    record_options = self._parse_record_options(command)
                except ValueError as error:
                    self._print_error(str(error))
                else:
                    self.record_screen(**record_options)

            # 5. Komut Çalıştırma (Tool Subprocess)
            elif normalized.startswith("çalıştır ") or normalized.startswith("calistir ") or normalized.startswith("/run "):
                cmd_to_run = command.split(maxsplit=1)[1]
                self.run_external_command(cmd_to_run)

            # 5. Yardım & Bilgi
            elif normalized in {"durum", "status", "/status", "info"}:
                self.show_status_badge()
            elif normalized in {"yardım", "yardim", "help", "?", "/help", "h"}:
                self.show_help()
            elif normalized in {"lisans", "license", "/license"}:
                self.show_license_notice()
            elif normalized in {"temizle", "clear", "cls", "/clear"}:
                if HAS_RICH:
                    try:
                        self.console.clear()
                    except Exception:
                        pass
                    self.print_banner()
                else:
                    os.system("cls" if os.name == "nt" else "clear")

            # 6. Çıkış
            elif normalized in {"çıkış", "cikis", "quit", "exit", "q", "/exit", "/quit"}:
                break
            else:
                if HAS_RICH:
                    self.console.print(f"  [bold #F87171]✕ Bilinmeyen komut:[/] [dim]'{command}'[/] [dim]— komut listesi için[/] [bold #38BDF8]/help[/] [dim]yazabilirsiniz.[/dim]")
                else:
                    print(f"  Bilinmeyen komut: '{command}'. Yardım için 'yardım' yazın.")

        # Çıkış Temizliği & Kapanış Özeti
        for service_name, result in self.websocket_manager.stop_all():
            if result is None:
                self._print_error(f"{service_name} WebSocket bağlantısı durdurulamadı.")

        if self.supported:
            try:
                self.set_execution_state(ES_CONTINUOUS)
            except Exception:
                pass

        if self.session_start_time and self.enabled:
            self.total_awake_seconds += int(time.time() - self.session_start_time)

        total_mins = self.total_awake_seconds // 60

        if HAS_RICH:
            summary = (
                f"[bold #34D399]✓ Güç ayarları başarıyla normale döndürüldü.[/]\n"
                f"[dim]• Toplam Korunan Süre:[/] [bold white]{total_mins} dakika[/]\n"
                f"[dim]• Tamamlanan Oturum:[/] [bold white]{self.completed_sessions}[/]\n\n"
                f"[dim #64748B]Görüşmek üzere! ✦ {APP_NAME}[/]"
            )
            self.console.print()
            self.console.print(Panel(summary, title="[dim]Oturum Özeti[/dim]", box=ROUNDED, border_style=CLAUDE_THEME["primary"]))
        else:
            print(f"\nGüç ayarları sıfırlandı. Toplam süre: {total_mins} dk. İyi çalışmalar!")


class _Point(ctypes.Structure):
    _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]


class _CursorInfo(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("hCursor", wintypes.HANDLE),
        ("ptScreenPos", _Point),
    ]


class _IconInfo(ctypes.Structure):
    _fields_ = [
        ("fIcon", wintypes.BOOL),
        ("xHotspot", wintypes.DWORD),
        ("yHotspot", wintypes.DWORD),
        ("hbmMask", wintypes.HANDLE),
        ("hbmColor", wintypes.HANDLE),
    ]


class _BitmapInfoHeader(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD),
        ("biWidth", wintypes.LONG),
        ("biHeight", wintypes.LONG),
        ("biPlanes", wintypes.WORD),
        ("biBitCount", wintypes.WORD),
        ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD),
        ("biXPelsPerMeter", wintypes.LONG),
        ("biYPelsPerMeter", wintypes.LONG),
        ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD),
    ]


class _BitmapInfo(ctypes.Structure):
    _fields_ = [("bmiHeader", _BitmapInfoHeader), ("bmiColors", wintypes.DWORD * 1)]


class _Win32CursorOverlay:
    CURSOR_SHOWING = 0x00000001
    DIB_RGB_COLORS = 0
    BI_RGB = 0
    DI_NORMAL = 0x0003

    def __init__(self, width, height, monitor_left, monitor_top):
        self.width = width
        self.height = height
        self.monitor_left = monitor_left
        self.monitor_top = monitor_top
        self.buffer_size = width * height * 4
        self.user32 = ctypes.WinDLL("user32", use_last_error=True)
        self.gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
        self.user32.GetCursorInfo.argtypes = [ctypes.POINTER(_CursorInfo)]
        self.user32.GetCursorInfo.restype = wintypes.BOOL
        self.user32.GetIconInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(_IconInfo)]
        self.user32.GetIconInfo.restype = wintypes.BOOL
        self.user32.DrawIconEx.argtypes = [
            wintypes.HDC, ctypes.c_int, ctypes.c_int, wintypes.HANDLE,
            ctypes.c_int, ctypes.c_int, wintypes.UINT, wintypes.HANDLE, wintypes.UINT,
        ]
        self.user32.DrawIconEx.restype = wintypes.BOOL
        self.gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
        self.gdi32.CreateCompatibleDC.restype = wintypes.HDC
        self.gdi32.CreateDIBSection.argtypes = [
            wintypes.HDC, ctypes.POINTER(_BitmapInfo), wintypes.UINT,
            ctypes.POINTER(ctypes.c_void_p), wintypes.HANDLE, wintypes.DWORD,
        ]
        self.gdi32.CreateDIBSection.restype = wintypes.HBITMAP
        self.gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
        self.gdi32.SelectObject.restype = wintypes.HGDIOBJ
        self.gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
        self.gdi32.DeleteObject.restype = wintypes.BOOL
        self.gdi32.DeleteDC.argtypes = [wintypes.HDC]
        self.gdi32.DeleteDC.restype = wintypes.BOOL

        self.bitmap_info = _BitmapInfo()
        self.bitmap_info.bmiHeader.biSize = ctypes.sizeof(_BitmapInfoHeader)
        self.bitmap_info.bmiHeader.biWidth = width
        self.bitmap_info.bmiHeader.biHeight = -height
        self.bitmap_info.bmiHeader.biPlanes = 1
        self.bitmap_info.bmiHeader.biBitCount = 32
        self.bitmap_info.bmiHeader.biCompression = self.BI_RGB
        self.bits = ctypes.c_void_p()
        self.bitmap = self.gdi32.CreateDIBSection(
            None, ctypes.byref(self.bitmap_info), self.DIB_RGB_COLORS,
            ctypes.byref(self.bits), None, 0,
        )
        if not self.bitmap:
            raise ctypes.WinError(ctypes.get_last_error())
        self.device_context = self.gdi32.CreateCompatibleDC(None)
        if not self.device_context:
            self.gdi32.DeleteObject(self.bitmap)
            raise ctypes.WinError(ctypes.get_last_error())
        self.previous_bitmap = self.gdi32.SelectObject(self.device_context, self.bitmap)
        if not self.previous_bitmap:
            self.gdi32.DeleteDC(self.device_context)
            self.gdi32.DeleteObject(self.bitmap)
            raise ctypes.WinError(ctypes.get_last_error())
        self.cursor_handle = None
        self.hotspot = (0, 0)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.gdi32.SelectObject(self.device_context, self.previous_bitmap)
        self.gdi32.DeleteObject(self.bitmap)
        self.gdi32.DeleteDC(self.device_context)

    def _cursor_hotspot(self, cursor_handle):
        if cursor_handle == self.cursor_handle:
            return self.hotspot
        icon_info = _IconInfo()
        if not self.user32.GetIconInfo(cursor_handle, ctypes.byref(icon_info)):
            return 0, 0
        self.cursor_handle = cursor_handle
        self.hotspot = (icon_info.xHotspot, icon_info.yHotspot)
        if icon_info.hbmMask:
            self.gdi32.DeleteObject(icon_info.hbmMask)
        if icon_info.hbmColor:
            self.gdi32.DeleteObject(icon_info.hbmColor)
        return self.hotspot

    def apply(self, bgra_frame):
        cursor_info = _CursorInfo()
        cursor_info.cbSize = ctypes.sizeof(_CursorInfo)
        if not self.user32.GetCursorInfo(ctypes.byref(cursor_info)):
            return bgra_frame
        if not cursor_info.flags & self.CURSOR_SHOWING or not cursor_info.hCursor:
            return bgra_frame
        if len(bgra_frame) != self.buffer_size:
            raise ValueError("Ekran karesi boyutu monitör boyutuyla uyuşmuyor.")

        ctypes.memmove(self.bits, bgra_frame, self.buffer_size)
        hotspot_x, hotspot_y = self._cursor_hotspot(cursor_info.hCursor)
        self.user32.DrawIconEx(
            self.device_context,
            cursor_info.ptScreenPos.x - self.monitor_left - hotspot_x,
            cursor_info.ptScreenPos.y - self.monitor_top - hotspot_y,
            cursor_info.hCursor,
            0, 0, 0, None, self.DI_NORMAL,
        )
        return ctypes.string_at(self.bits, self.buffer_size)


class ScreenRecorder:
    def __init__(
        self, output_dir=None, fps=15, resolution=None, monitor_number=1,
        capture_factory=None, ffmpeg_path=None, microphone=None, system_audio=None,
    ):
        videos_dir = Path.home() / "Videos"
        self.output_dir = Path(output_dir) if output_dir else videos_dir / "Easy Windows Tools"
        self.fps = self.validate_fps(fps)
        self.resolution = self.parse_resolution(resolution)
        self.monitor_number = self.validate_monitor_number(monitor_number)
        self.capture_factory = capture_factory
        self.ffmpeg_path = ffmpeg_path
        self.microphone = self.validate_audio_device(microphone)
        self.system_audio = self.validate_audio_device(system_audio)

    @staticmethod
    def validate_fps(value):
        try:
            fps = int(value)
        except (TypeError, ValueError) as error:
            raise ValueError("Kare hızı tam sayı olmalıdır.") from error
        if not 1 <= fps <= 60:
            raise ValueError("Kare hızı 1 ile 60 FPS arasında olmalıdır.")
        return fps

    @staticmethod
    def parse_resolution(value):
        if value is None or str(value).strip().lower() in {"", "native", "original", "monitor"}:
            return None
        if isinstance(value, (list, tuple)) and len(value) == 2:
            value = f"{value[0]}x{value[1]}"
        match = re.fullmatch(r"(\d{3,5})\s*[xX]\s*(\d{3,5})", str(value).strip())
        if not match:
            raise ValueError("Çözünürlük 1280x720 biçiminde yazılmalıdır.")
        width, height = map(int, match.groups())
        if not 320 <= width <= 7680 or not 240 <= height <= 4320:
            raise ValueError("Çözünürlük genişliği 320-7680, yüksekliği 240-4320 arasında olmalıdır.")
        if width % 2 or height % 2:
            raise ValueError("H.264 kayıt için çözünürlük değerleri çift sayı olmalıdır.")
        return width, height

    @staticmethod
    def validate_monitor_number(value):
        try:
            monitor_number = int(value)
        except (TypeError, ValueError) as error:
            raise ValueError("Ekran numarası tam sayı olmalıdır.") from error
        if monitor_number < 1:
            raise ValueError("Ekran numarası 1 veya daha büyük olmalıdır.")
        return monitor_number

    @staticmethod
    def validate_audio_device(value):
        if value is None or str(value).strip().lower() in {"", "off", "kapalı", "kapali"}:
            return None
        device = str(value).strip()
        if "\n" in device or "\r" in device or '"' in device:
            raise ValueError("Ses aygıtı adı geçersiz.")
        return device

    @staticmethod
    def settings_path():
        app_data = Path(os.environ.get("APPDATA", Path.home()))
        return app_data / "EasyWindowsTools" / "recording.json"

    @classmethod
    def load_settings(cls):
        defaults = {
            "fps": 15,
            "resolution": None,
            "monitor": 1,
            "microphone": None,
            "system_audio": None,
        }
        try:
            settings = json.loads(cls.settings_path().read_text(encoding="utf-8"))
            defaults["fps"] = cls.validate_fps(settings.get("fps", defaults["fps"]))
            defaults["resolution"] = cls.parse_resolution(settings.get("resolution"))
            defaults["monitor"] = cls.validate_monitor_number(settings.get("monitor", 1))
            defaults["microphone"] = cls.validate_audio_device(settings.get("microphone"))
            defaults["system_audio"] = cls.validate_audio_device(settings.get("system_audio"))
        except (OSError, ValueError, TypeError, AttributeError):
            pass
        return defaults

    @classmethod
    def save_settings(
        cls, fps, resolution, monitor_number=None, microphone=None, system_audio=None
    ):
        parsed_resolution = cls.parse_resolution(resolution)
        current_settings = cls.load_settings()
        if monitor_number is None:
            monitor_number = current_settings["monitor"]
        if microphone is None:
            microphone = current_settings["microphone"]
        if system_audio is None:
            system_audio = current_settings["system_audio"]
        settings = {
            "fps": cls.validate_fps(fps),
            "resolution": f"{parsed_resolution[0]}x{parsed_resolution[1]}" if parsed_resolution else None,
            "monitor": cls.validate_monitor_number(monitor_number),
            "microphone": cls.validate_audio_device(microphone),
            "system_audio": cls.validate_audio_device(system_audio),
        }
        settings_path = cls.settings_path()
        settings_path.parent.mkdir(parents=True, exist_ok=True)
        settings_path.write_text(json.dumps(settings, indent=2), encoding="utf-8")
        settings["resolution"] = parsed_resolution
        return settings

    @classmethod
    def list_audio_devices(cls, ffmpeg_path=None):
        if os.name != "nt":
            raise RuntimeError("Ses aygıtlarını listeleme yalnızca Windows'ta kullanılabilir.")
        if ffmpeg_path is None:
            _, get_ffmpeg_exe = cls._load_dependencies()
            ffmpeg_path = get_ffmpeg_exe()
        result = subprocess.run(
            [
                ffmpeg_path, "-hide_banner", "-list_devices", "true",
                "-f", "dshow", "-i", "dummy",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        device_names = re.findall(
            r'^\[dshow @ [^\]]+\] "([^"]+)" \(audio\)$',
            result.stderr,
            flags=re.MULTILINE,
        )
        if not device_names and result.returncode == 0:
            raise RuntimeError("FFmpeg ses aygıtlarını listeleyemedi.")
        return device_names

    @staticmethod
    def find_system_audio_devices(device_names):
        loopback_name = re.compile(
            r"stereo\s*mix|wave\s*out\s*mix|what\s*u\s*hear|loopback|monitor|cable",
            flags=re.IGNORECASE,
        )
        return [name for name in device_names if loopback_name.search(name)]

    @classmethod
    def list_monitors(cls):
        capture_class, _ = cls._load_dependencies()
        with capture_class() as capture:
            return [
                {
                    "number": number,
                    "width": monitor["width"],
                    "height": monitor["height"],
                    "left": monitor["left"],
                    "top": monitor["top"],
                    "is_primary": monitor.get("is_primary", False),
                    "name": monitor.get("name", f"Ekran {number}"),
                }
                for number, monitor in enumerate(capture.monitors[1:], start=1)
            ]

    @staticmethod
    def _load_dependencies():
        try:
            from imageio_ffmpeg import get_ffmpeg_exe
            from mss import MSS
        except ImportError as error:
            project_packages = Path(__file__).resolve().parent / ".venv" / "Lib" / "site-packages"
            if not project_packages.is_dir():
                raise RuntimeError(
                    "Ekran kaydı bağımlılıkları bulunamadı. Uygulamayı baslat.bat ile açın "
                    "veya requirements.txt dosyasını kullandığınız Python ortamına kurun."
                ) from error

            sys.path.insert(0, str(project_packages))
            try:
                from imageio_ffmpeg import get_ffmpeg_exe
                from mss import MSS
            except ImportError as fallback_error:
                raise RuntimeError(
                    "Ekran kaydı paketleri proje .venv ortamında da bulunamadı; "
                    "requirements.txt dosyasını kurun."
                ) from fallback_error
        return MSS, get_ffmpeg_exe

    def record(self, duration=None, monitor_number=None):
        if os.name != "nt":
            raise RuntimeError("Ekran kaydı yalnızca Windows'ta kullanılabilir.")
        capture_class, get_ffmpeg_exe = self._load_dependencies()
        capture_factory = self.capture_factory or capture_class
        ffmpeg_path = self.ffmpeg_path or get_ffmpeg_exe()
        audio_inputs = [
            device for device in (self.microphone, self.system_audio) if device is not None
        ]
        if audio_inputs:
            available_audio_devices = set(self.list_audio_devices(ffmpeg_path))
            missing_audio_devices = [
                device for device in audio_inputs if device not in available_audio_devices
            ]
            if missing_audio_devices:
                raise RuntimeError(
                    "Ses aygıtı bulunamadı: "
                    + ", ".join(missing_audio_devices)
                    + ". /record ayarlar komutuyla aygıtları yeniden seçin."
                )
        self.output_dir.mkdir(parents=True, exist_ok=True)
        output_path = self.output_dir / f"Screen Recording {datetime.now():%Y-%m-%d %H-%M-%S-%f}.mp4"

        try:
            with capture_factory() as capture, ExitStack() as resources:
                selected_monitor = self.validate_monitor_number(
                    self.monitor_number if monitor_number is None else monitor_number
                )
                if selected_monitor >= len(capture.monitors):
                    available = max(0, len(capture.monitors) - 1)
                    raise ValueError(
                        f"Ekran {selected_monitor} bulunamadı. Bu sistemde {available} ekran var."
                    )
                monitor = capture.monitors[selected_monitor]
                width = monitor["width"]
                height = monitor["height"]
                cursor_overlay = resources.enter_context(
                    _Win32CursorOverlay(width, height, monitor["left"], monitor["top"])
                )
                video_filter = "pad=ceil(iw/2)*2:ceil(ih/2)*2"
                if self.resolution:
                    output_width, output_height = self.resolution
                    video_filter = (
                        f"scale={output_width}:{output_height}:"
                        "force_original_aspect_ratio=decrease:force_divisible_by=2,"
                        f"pad={output_width}:{output_height}:(ow-iw)/2:(oh-ih)/2"
                    )
                command = [
                    ffmpeg_path,
                    "-hide_banner", "-loglevel", "error", "-y",
                    "-f", "rawvideo", "-pixel_format", "bgra",
                    "-video_size", f"{width}x{height}",
                    "-framerate", str(self.fps), "-i", "pipe:0",
                ]
                for audio_device in audio_inputs:
                    command.extend(["-f", "dshow", "-i", f"audio={audio_device}"])
                command.extend([
                    "-map", "0:v:0",
                    "-vf", video_filter,
                    "-c:v", "libx264", "-preset", "ultrafast", "-crf", "23",
                    "-pix_fmt", "yuv420p",
                ])
                if len(audio_inputs) == 2:
                    command.extend([
                        "-filter_complex",
                        "[1:a:0][2:a:0]amix=inputs=2:duration=longest:dropout_transition=2,apad[aout]",
                        "-map", "[aout]",
                        "-c:a", "aac", "-b:a", "192k", "-shortest",
                    ])
                elif audio_inputs:
                    command.extend([
                        "-filter_complex", "[1:a:0]apad[aout]",
                        "-map", "[aout]", "-c:a", "aac", "-b:a", "192k", "-shortest",
                    ])
                else:
                    command.append("-an")
                command.extend(["-movflags", "+faststart", str(output_path)])
                process = subprocess.Popen(
                    command,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
                started_at = time.perf_counter()
                next_frame_at = started_at
                frame_count = 0
                stopped_by_user = False
                try:
                    while duration is None or time.perf_counter() - started_at < duration:
                        frame = cursor_overlay.apply(capture.grab(monitor).bgra)
                        try:
                            process.stdin.write(frame)
                        except BrokenPipeError:
                            break
                        frame_count += 1
                        frame_interval = 1 / self.fps
                        next_frame_at += frame_interval
                        now = time.perf_counter()
                        while next_frame_at <= now:
                            try:
                                process.stdin.write(frame)
                            except BrokenPipeError:
                                break
                            frame_count += 1
                            next_frame_at += frame_interval
                        delay = next_frame_at - time.perf_counter()
                        if delay > 0:
                            time.sleep(delay)
                except KeyboardInterrupt:
                    stopped_by_user = True
                finally:
                    try:
                        process.stdin.close()
                    except (BrokenPipeError, OSError):
                        pass
                    return_code = process.wait()
                    error_output = process.stderr.read().decode("utf-8", errors="replace")
                if return_code != 0:
                    raise RuntimeError(error_output.strip() or f"FFmpeg çıkış kodu: {return_code}")
                if frame_count == 0:
                    raise RuntimeError("Kayıt için hiç ekran karesi alınamadı.")
                return output_path, time.perf_counter() - started_at, stopped_by_user
        except Exception:
            output_path.unlink(missing_ok=True)
            raise


# Package-manager search is used for the broader catalog so package identifiers
# stay current instead of being hard-coded from a stale package list.
MCP_APPLICATION_CATALOG = tuple(line.strip() for line in """
Godot
Unity Hub
Unreal Engine
Blender
Google Chrome
Visual Studio
Visual Studio Code
MCreator
SQLite
Blockbench
IntelliJ IDEA
PyCharm
Android Studio
Eclipse IDE
JetBrains Rider
WebStorm
CLion
GoLand
PhpStorm
DataGrip
Git
GitHub Desktop
GitKraken
Docker Desktop
Podman Desktop
Postman
Insomnia
DBeaver
DB Browser for SQLite
pgAdmin
MySQL Workbench
MongoDB Compass
Redis Insight
Beekeeper Studio
Figma
GIMP
Krita
Inkscape
Affinity Photo
DaVinci Resolve
OBS Studio
Audacity
Shotcut
Kdenlive
HandBrake
VLC
7-Zip
WinRAR
Everything
PowerToys
ShareX
Notepad++
Sublime Text
Firefox
Brave
Vivaldi
Microsoft Edge
Opera
Discord
Slack
Zoom
Microsoft Teams
Telegram
Signal
Spotify
Steam
Epic Games Launcher
GOG Galaxy
Heroic Games Launcher
Lutris
Minecraft Launcher
Prism Launcher
CurseForge
Java
Python
Node.js
.NET SDK
Rust
Go
CMake
Ninja
LLVM
GCC
OpenJDK
Maven
Gradle
Flutter
Dart
XAMPP
Apache NetBeans
Qt Creator
VSCodium
Zed
Cursor
Windsurf
RStudio
TeXstudio
KeePassXC
qBittorrent
FileZilla
""".splitlines() if line.strip())
assert len(MCP_APPLICATION_CATALOG) == 100

MCP_APP_PACKAGE_IDS = {
    "Godot": {"winget": "GodotEngine.GodotEngine", "apt": "godot3", "snap": "godot", "pacman": "godot", "brew": "godot"},
    "Unity Hub": {"winget": "Unity.UnityHub", "brew": "unity-hub"},
    "Blender": {"winget": "BlenderFoundation.Blender", "apt": "blender", "snap": "blender", "pacman": "blender", "brew": "blender"},
    "Google Chrome": {"winget": "Google.Chrome", "apt": "google-chrome-stable", "brew": "google-chrome"},
    "Visual Studio": {"winget": "Microsoft.VisualStudio.2022.Community"},
    "Visual Studio Code": {"winget": "Microsoft.VisualStudioCode", "apt": "code", "snap": "code", "pacman": "visual-studio-code-bin", "brew": "visual-studio-code"},
    "MCreator": {"winget": "MCreator.MCreator"},
    "SQLite": {"winget": "SQLite.SQLite", "apt": "sqlite3", "pacman": "sqlite", "brew": "sqlite"},
    "Blockbench": {"winget": "Blockbench.Blockbench", "brew": "blockbench"},
    "IntelliJ IDEA": {"winget": "JetBrains.IntelliJIDEA.Community", "snap": "intellij-idea-community", "pacman": "intellij-idea-community-edition", "brew": "intellij-idea-ce"},
    "PyCharm": {"winget": "JetBrains.PyCharm.Community", "snap": "pycharm-community", "pacman": "pycharm-community-edition", "brew": "pycharm-ce"},
    "Android Studio": {"winget": "Google.AndroidStudio", "snap": "android-studio", "pacman": "android-studio", "brew": "android-studio"},
    "Docker Desktop": {"winget": "Docker.DockerDesktop", "brew": "docker"},
    "Git": {"winget": "Git.Git", "apt": "git", "snap": "git", "pacman": "git", "brew": "git"},
    "Firefox": {"winget": "Mozilla.Firefox", "apt": "firefox", "snap": "firefox", "pacman": "firefox", "brew": "firefox"},
    "VLC": {"winget": "VideoLAN.VLC", "apt": "vlc", "snap": "vlc", "pacman": "vlc", "brew": "vlc"},
    "GIMP": {"winget": "GIMP.GIMP", "apt": "gimp", "snap": "gimp", "pacman": "gimp", "brew": "gimp"},
    "Krita": {"winget": "KDE.Krita", "apt": "krita", "snap": "krita", "pacman": "krita", "brew": "krita"},
    "OBS Studio": {"winget": "OBSProject.OBSStudio", "apt": "obs-studio", "snap": "obs-studio", "pacman": "obs-studio", "brew": "obs"},
    "Audacity": {"winget": "Audacity.Audacity", "apt": "audacity", "snap": "audacity", "pacman": "audacity", "brew": "audacity"},
    "Steam": {"winget": "Valve.Steam", "apt": "steam", "pacman": "steam", "brew": "steam"},
    "7-Zip": {"winget": "7zip.7zip", "apt": "7zip", "pacman": "7zip", "brew": "sevenzip"},
    "Notepad++": {"winget": "Notepad++.Notepad++", "snap": "notepad-plus-plus", "brew": "notepad-plus-plus"},
    "Postman": {"winget": "Postman.Postman", "snap": "postman", "pacman": "postman", "brew": "postman"},
    "DBeaver": {"winget": "DBeaver.DBeaver.Community", "apt": "dbeaver-ce", "snap": "dbeaver-ce", "pacman": "dbeaver", "brew": "dbeaver-community"},
    "DB Browser for SQLite": {"winget": "DBBrowserForSQLite.DBBrowserForSQLite", "apt": "sqlitebrowser", "pacman": "sqlitebrowser", "brew": "db-browser-for-sqlite"},
    "Discord": {"winget": "Discord.Discord", "snap": "discord", "pacman": "discord", "brew": "discord"},
    "Telegram": {"winget": "Telegram.TelegramDesktop", "apt": "telegram-desktop", "snap": "telegram-desktop", "pacman": "telegram-desktop", "brew": "telegram"},
    "qBittorrent": {"winget": "qBittorrent.qBittorrent", "apt": "qbittorrent", "snap": "qbittorrent-arnatious", "pacman": "qbittorrent", "brew": "qbittorrent"},
    "KeePassXC": {"winget": "KeePassXCTeam.KeePassXC", "apt": "keepassxc", "snap": "keepassxc", "pacman": "keepassxc", "brew": "keepassxc"},
}

MCP_APP_COMMANDS = {
    "Godot": ["godot", "godot4", "godot3"],
    "Blender": ["blender"],
    "Google Chrome": ["google-chrome", "chrome", "chromium", "chromium-browser"],
    "Visual Studio Code": ["code", "code-insiders"],
    "MCreator": ["mcreator"],
    "SQLite": ["sqlite3"],
    "Blockbench": ["blockbench"],
    "IntelliJ IDEA": ["idea", "idea-community"],
    "PyCharm": ["pycharm", "pycharm-community"],
    "Android Studio": ["studio"],
    "Git": ["git"],
    "Docker Desktop": ["docker"],
    "Firefox": ["firefox"],
    "VLC": ["vlc"],
    "GIMP": ["gimp"],
    "Krita": ["krita"],
    "OBS Studio": ["obs", "obs-studio"],
    "Audacity": ["audacity"],
    "Steam": ["steam"],
}

MCP_APP_DOWNLOAD_PAGES = {
    "Godot": "https://godotengine.org/download/macos/",
    "Unity Hub": "https://unity.com/download",
    "Unreal Engine": "https://www.unrealengine.com/download",
    "Blender": "https://www.blender.org/download/",
    "Google Chrome": "https://www.google.com/chrome/",
    "Visual Studio Code": "https://code.visualstudio.com/download",
    "MCreator": "https://mcreator.net/download",
    "SQLite": "https://www.sqlite.org/download.html",
    "Blockbench": "https://www.blockbench.net/download",
    "IntelliJ IDEA": "https://www.jetbrains.com/idea/download/",
    "PyCharm": "https://www.jetbrains.com/pycharm/download/",
    "Android Studio": "https://developer.android.com/studio",
}


class MCPManager:
    def __init__(self, console=None, db_path=None, script_path=None, python_path=None):
        self.console = console
        default_data_dir = Path(os.environ.get("APPDATA", Path.home())) / "UyanikKal"
        configured_db = db_path or os.environ.get("UYANIK_KAL_MCP_DB")
        self.db_path = Path(configured_db) if configured_db else default_data_dir / "mcps.sqlite3"
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.script_path = Path(script_path or Path(__file__).resolve()).resolve()
        self.python_path = str(Path(python_path or sys.executable).resolve())
        self._initialize_database()

    @contextmanager
    def _connect(self):
        connection = sqlite3.connect(self.db_path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def _initialize_database(self):
        with self._connect() as connection:
            connection.execute(
                """CREATE TABLE IF NOT EXISTS mcp_servers (
                    id INTEGER PRIMARY KEY,
                    name TEXT NOT NULL COLLATE NOCASE UNIQUE,
                    app_name TEXT NOT NULL,
                    executable TEXT NOT NULL,
                    source TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    configuration TEXT NOT NULL
                )"""
            )
            connection.execute(
                """CREATE TABLE IF NOT EXISTS managed_processes (
                    server_id INTEGER NOT NULL REFERENCES mcp_servers(id) ON DELETE CASCADE,
                    pid INTEGER NOT NULL,
                    started_at TEXT NOT NULL,
                    PRIMARY KEY (server_id, pid)
                )"""
            )
            connection.execute(
                """CREATE TABLE IF NOT EXISTS running_mcp_servers (
                    server_id INTEGER PRIMARY KEY REFERENCES mcp_servers(id) ON DELETE CASCADE,
                    pid INTEGER NOT NULL,
                    port INTEGER NOT NULL,
                    started_at TEXT NOT NULL
                )"""
            )
            columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(mcp_servers)")
            }
            if "settings" not in columns:
                connection.execute(
                    "ALTER TABLE mcp_servers ADD COLUMN settings TEXT NOT NULL DEFAULT '{}'"
                )

    @staticmethod
    def _display_icon_executable(value):
        if not value:
            return None
        value = os.path.expandvars(value.strip())
        if value.startswith('"'):
            end = value.find('"', 1)
            candidate = value[1:end] if end > 0 else value.strip('"')
        else:
            candidate = value.rsplit(",", 1)[0].strip()
        path = Path(candidate)
        if path.suffix.lower() != ".exe" or not path.is_file():
            return None
        return str(path.resolve())

    @classmethod
    def discover_applications(cls):
        applications = {}
        if os.name == "nt":
            import winreg

            uninstall_key = r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"
            registry_views = [0]
            if hasattr(winreg, "KEY_WOW64_64KEY"):
                registry_views.extend([winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY])
            registry_roots = [winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE]
            for root in registry_roots:
                for view in dict.fromkeys(registry_views):
                    try:
                        root_key = winreg.OpenKey(root, uninstall_key, 0, winreg.KEY_READ | view)
                    except OSError:
                        continue
                    with root_key:
                        for index in range(winreg.QueryInfoKey(root_key)[0]):
                            try:
                                subkey_name = winreg.EnumKey(root_key, index)
                                with winreg.OpenKey(root_key, subkey_name) as subkey:
                                    name = winreg.QueryValueEx(subkey, "DisplayName")[0]
                                    try:
                                        display_icon = winreg.QueryValueEx(subkey, "DisplayIcon")[0]
                                    except OSError:
                                        display_icon = ""
                                    try:
                                        is_system_component = winreg.QueryValueEx(subkey, "SystemComponent")[0] == 1
                                    except OSError:
                                        is_system_component = False
                            except (OSError, TypeError):
                                continue
                            if is_system_component:
                                continue
                            executable = cls._display_icon_executable(display_icon)
                            if not executable:
                                continue
                            key = os.path.normcase(executable)
                            applications.setdefault(key, {
                                "name": str(name).strip(),
                                "executable": executable,
                                "source": "Windows uygulama kayıt defteri",
                            })
            app_paths_key = r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths"
            for root in registry_roots:
                for view in dict.fromkeys(registry_views):
                    try:
                        root_key = winreg.OpenKey(root, app_paths_key, 0, winreg.KEY_READ | view)
                    except OSError:
                        continue
                    with root_key:
                        for index in range(winreg.QueryInfoKey(root_key)[0]):
                            try:
                                with winreg.OpenKey(root_key, winreg.EnumKey(root_key, index)) as subkey:
                                    executable_value = winreg.QueryValueEx(subkey, None)[0]
                            except OSError:
                                continue
                            executable = cls._display_icon_executable(str(executable_value))
                            if not executable:
                                continue
                            applications.setdefault(os.path.normcase(executable), {
                                "name": Path(executable).stem,
                                "executable": executable,
                                "source": "Windows App Paths kayıt defteri",
                            })
        elif sys.platform == "darwin":
            app_directories = [Path("/Applications"), Path.home() / "Applications"]
            for directory in app_directories:
                if not directory.is_dir():
                    continue
                for app_path in directory.glob("*.app"):
                    applications[str(app_path.resolve()).casefold()] = {
                        "name": app_path.stem,
                        "executable": f"@open:{app_path.resolve()}",
                        "source": "macOS Applications",
                    }
        else:
            desktop_directories = [
                Path("/usr/share/applications"),
                Path("/usr/local/share/applications"),
                Path.home() / ".local/share/applications",
            ]
            for directory in desktop_directories:
                if not directory.is_dir():
                    continue
                for desktop_file in directory.rglob("*.desktop"):
                    try:
                        text = desktop_file.read_text(encoding="utf-8", errors="replace")
                    except OSError:
                        continue
                    if re.search(r"(?m)^NoDisplay=true\s*$", text, flags=re.IGNORECASE):
                        continue
                    name_match = re.search(r"(?m)^Name(?:\[[^]]+\])?=(.+)$", text)
                    exec_match = re.search(r"(?m)^Exec=(.+)$", text)
                    if not name_match or not exec_match:
                        continue
                    name = name_match.group(1).strip()
                    executable = shlex.split(exec_match.group(1).strip())[0]
                    command = shutil.which(executable)
                    if not command:
                        continue
                    applications[name.casefold()] = {
                        "name": name,
                        "executable": command,
                        "source": str(desktop_file),
                    }
            for app_name, commands in MCP_APP_COMMANDS.items():
                for command in commands:
                    path = shutil.which(command)
                    if path:
                        applications.setdefault(app_name.casefold(), {
                            "name": app_name,
                            "executable": path,
                            "source": "PATH",
                        })
                        break
        return sorted(applications.values(), key=lambda item: item["name"].casefold())

    @staticmethod
    def available_package_managers():
        if os.name == "nt":
            return ["winget"] if shutil.which("winget") else []
        if sys.platform == "darwin":
            return ["brew"] if shutil.which("brew") else []
        if not sys.platform.startswith("linux"):
            return []

        os_release = {}
        try:
            for line in Path("/etc/os-release").read_text(encoding="utf-8").splitlines():
                key, separator, value = line.partition("=")
                if separator:
                    os_release[key] = value.strip('"')
        except OSError:
            pass
        distro = (os_release.get("ID", "") + " " + os_release.get("ID_LIKE", "")).lower()
        managers = []
        if shutil.which("apt-get") and ("debian" in distro or "ubuntu" in distro or "linuxmint" in distro):
            managers.append("apt")
        if shutil.which("snap"):
            managers.append("snap")
        if shutil.which("pacman") and ("arch" in distro or "garuda" in distro or not distro):
            managers.append("pacman")
        return managers

    @staticmethod
    def _run_package_search(manager, query):
        if manager == "winget":
            command = ["winget", "search", "--query", query, "--accept-source-agreements"]
        elif manager == "apt":
            command = ["apt-cache", "search", "--names-only", query]
        elif manager == "snap":
            command = ["snap", "find", query]
        elif manager == "pacman":
            command = ["pacman", "-Ss", query]
        elif manager == "brew":
            command = ["brew", "search", "--casks", query]
        else:
            raise ValueError(f"Desteklenmeyen paket yöneticisi: {manager}")
        result = subprocess.run(
            command, capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=45, check=False,
        )
        if result.returncode and not result.stdout.strip():
            detail = result.stderr.strip() or f"çıkış kodu {result.returncode}"
            raise RuntimeError(f"{manager} paket araması başarısız: {detail}")
        candidates = []
        if manager == "winget":
            for line in result.stdout.splitlines():
                match = re.match(r"^\s*(.+?)\s{2,}([A-Za-z0-9][A-Za-z0-9._+-]+)\s{2,}", line)
                if match and match.group(2).lower() not in {"id", "version", "unknown"}:
                    candidates.append({"name": match.group(1).strip(), "id": match.group(2)})
        elif manager == "apt":
            for line in result.stdout.splitlines():
                package, separator, description = line.partition(" - ")
                if separator and package:
                    candidates.append({"name": description.strip(), "id": package.strip()})
        elif manager == "snap":
            for line in result.stdout.splitlines():
                match = re.match(r"^\s*([A-Za-z0-9][A-Za-z0-9._+-]+)\s{2,}(.+?)\s{2,}", line)
                if match and match.group(1).lower() not in {"name", "snap"}:
                    candidates.append({"name": match.group(2).strip(), "id": match.group(1)})
        elif manager == "pacman":
            for line in result.stdout.splitlines():
                match = re.match(r"^[^/\s]+/([A-Za-z0-9@._+-]+)\s+(.+)$", line.strip())
                if match:
                    candidates.append({"name": match.group(2).strip(), "id": match.group(1)})
        elif manager == "brew":
            for line in result.stdout.splitlines():
                package = line.strip()
                if package and not package.startswith(("==>", "Warning:")):
                    candidates.append({"name": package, "id": package})
        unique = {}
        for candidate in candidates:
            unique.setdefault(candidate["id"].casefold(), candidate)
        return list(unique.values())[:100]

    @classmethod
    def search_catalog_packages(cls, app_name, manager):
        candidates = cls._run_package_search(manager, app_name)
        package_id = MCP_APP_PACKAGE_IDS.get(app_name, {}).get(manager)
        if package_id:
            verified = [
                item for item in candidates
                if item["id"].casefold() == package_id.casefold()
            ]
            if verified:
                return verified
        return candidates

    @staticmethod
    def install_package(manager, package_id):
        if manager == "winget":
            command = [
                "winget", "install", "--id", package_id, "--exact",
                "--accept-source-agreements", "--accept-package-agreements",
            ]
        elif manager == "apt":
            command = ["pkexec", "apt-get", "install", "-y", package_id] if shutil.which("pkexec") else ["sudo", "apt-get", "install", "-y", package_id]
        elif manager == "snap":
            command = ["pkexec", "snap", "install", package_id] if shutil.which("pkexec") else ["sudo", "snap", "install", package_id]
        elif manager == "pacman":
            command = ["pkexec", "pacman", "-S", "--needed", "--noconfirm", package_id] if shutil.which("pkexec") else ["sudo", "pacman", "-S", "--needed", "--noconfirm", package_id]
        elif manager == "brew":
            command = ["brew", "install", "--cask", package_id]
        else:
            raise ValueError(f"Desteklenmeyen paket yöneticisi: {manager}")
        return subprocess.run(
            command, capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=1800, check=False,
        )

    @staticmethod
    def _server_key(name):
        key = re.sub(r"[^a-zA-Z0-9_-]+", "-", name.strip().lower()).strip("-_")
        return key or "desktop-app"

    def _configuration_for(self, server_id, name):
        key = self._server_key(name)
        command = {
            "command": self.python_path,
            "args": [str(self.script_path), "--mcp-server", str(server_id)],
        }
        port = 8800 + ((server_id - 1) % (65535 - 8800))
        url = f"http://127.0.0.1:{port}/mcp"
        return {
            "vscode": {"servers": {key: {"type": "stdio", **command}}},
            "antigravity": {"mcpServers": {key: command}},
            "http": {"url": url},
        }

    def create_server(self, name, app):
        name = name.strip()
        executable = str(app["executable"])
        if not name:
            raise ValueError("MCP sunucusu için bir ad girin.")
        if executable.startswith("@open:"):
            if not Path(executable.removeprefix("@open:")).exists():
                raise ValueError("Uygulama dosyası bulunamadı.")
        elif not Path(executable).is_file() and not shutil.which(executable):
            raise ValueError("Uygulamanın çalıştırılabilir dosyası bulunamadı.")
        created_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with self._connect() as connection:
            cursor = connection.execute(
                """INSERT INTO mcp_servers
                   (name, app_name, executable, source, created_at, configuration)
                   VALUES (?, ?, ?, ?, ?, '{}')""",
                (name, app["name"], executable, app.get("source", "Manuel"), created_at),
            )
            server_id = cursor.lastrowid
            configuration = self._configuration_for(server_id, name)
            connection.execute(
                "UPDATE mcp_servers SET configuration = ? WHERE id = ?",
                (json.dumps(configuration, ensure_ascii=False, indent=2), server_id),
            )
        return self.get_server(name)

    @staticmethod
    def default_server_settings():
        return {
            "allow_app_status": False,
            "allow_launch_application": False,
            "allow_close_application": False,
            "allow_screen_capture": False,
            "allow_input_control": False,
        }

    def server_settings(self, server):
        latest_server = self.get_server_by_id(server["id"])
        if latest_server:
            server = latest_server
        try:
            stored = json.loads(server.get("settings") or "{}")
        except (TypeError, json.JSONDecodeError):
            stored = {}
        settings = self.default_server_settings()
        if not isinstance(stored, dict):
            stored = {}
        for key in settings:
            value = stored.get(key, False)
            settings[key] = value if type(value) is bool else False
        return settings

    def update_server_settings(self, server_id, **settings):
        allowed_keys = set(self.default_server_settings())
        if (
            not settings
            or set(settings) - allowed_keys
            or any(type(value) is not bool for value in settings.values())
        ):
            raise ValueError("Geçersiz MCP güvenlik ayarı.")
        server = self.get_server_by_id(server_id)
        if not server:
            raise ValueError("MCP sunucusu bulunamadı.")
        current = self.server_settings(server)
        current.update(settings)
        with self._connect() as connection:
            connection.execute(
                "UPDATE mcp_servers SET settings = ? WHERE id = ?",
                (json.dumps(current), server_id),
            )
        return current

    def start_mcp_server(self, name):
        server = self.get_server(name)
        if not server:
            raise ValueError(f"'{name}' adında yerel MCP sunucusu kayıtlı değil.")
        with self._connect() as connection:
            running = connection.execute(
                "SELECT pid, port FROM running_mcp_servers WHERE server_id = ?",
                (server["id"],),
            ).fetchone()
        if running:
            if not self._pid_exists(running["pid"]):
                with self._connect() as connection:
                    connection.execute(
                        "DELETE FROM running_mcp_servers WHERE server_id = ?",
                        (server["id"],),
                    )
            else:
                url = f"http://127.0.0.1:{running['port']}/mcp"
                if self._mcp_http_handshake(url, server["name"]):
                    return f"{server['name']} MCP sunucusu zaten çalışıyor: {url}"
                raise RuntimeError(
                    f"Kayıtlı MCP süreci çalışıyor ancak {url} MCP olarak yanıt vermiyor."
                )

        port = 8800 + ((server["id"] - 1) % (65535 - 8800))
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
                listener.bind(("127.0.0.1", port))
        except OSError as error:
            raise RuntimeError(
                f"127.0.0.1:{port} bağlantı noktası kullanımda; MCP sunucusu başlatılmadı."
            ) from error
        command = [
            self.python_path, str(self.script_path), "--mcp-server", str(server["id"]),
            "--transport", "streamable-http", "--port", str(port),
        ]
        creationflags = 0
        popen_options = {}
        if os.name == "nt":
            creationflags = (
                getattr(subprocess, "DETACHED_PROCESS", 0)
                | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                | getattr(subprocess, "CREATE_NO_WINDOW", 0)
            )
        else:
            popen_options["start_new_session"] = True
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            creationflags=creationflags,
            **popen_options,
        )
        deadline = time.monotonic() + 20
        url = f"http://127.0.0.1:{port}/mcp"
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError(
                    f"MCP süreci başlatılamadı (çıkış kodu {process.returncode})."
                )
            if self._mcp_http_handshake(url, server["name"]):
                break
            time.sleep(0.1)
        else:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
            raise RuntimeError(
                f"MCP sunucusu {url} adresinde zamanında MCP yanıtı vermedi."
            )
        with self._connect() as connection:
            connection.execute(
                """INSERT OR REPLACE INTO running_mcp_servers
                   (server_id, pid, port, started_at) VALUES (?, ?, ?, ?)""",
                (server["id"], process.pid, port, datetime.now(timezone.utc).isoformat(timespec="seconds")),
            )
        return f"{server['name']} MCP sunucusu çalışıyor: {url}"

    @staticmethod
    def _mcp_http_handshake(url, expected_name):
        protocol_version = "2025-03-26"
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": protocol_version,
                "capabilities": {},
                "clientInfo": {"name": "easy-windows-tools-healthcheck", "version": APP_VERSION},
            },
        }
        request = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Accept": "application/json, text/event-stream",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=1) as response:
                body = response.read(65536).decode("utf-8", errors="replace")
                session_id = response.headers.get("Mcp-Session-Id")
        except (OSError, urllib.error.URLError, urllib.error.HTTPError):
            return False
        try:
            message = json.loads(body)
        except json.JSONDecodeError:
            message = None
            for line in body.splitlines():
                if line.startswith("data:"):
                    try:
                        message = json.loads(line.partition(":")[2].strip())
                    except json.JSONDecodeError:
                        continue
                    if message:
                        break
        if not isinstance(message, dict):
            return False
        result = message.get("result")
        if not isinstance(result, dict):
            return False
        server_info = result.get("serverInfo")
        if not isinstance(server_info, dict) or server_info.get("name") != expected_name:
            return False
        negotiated_version = result.get("protocolVersion")
        if not isinstance(negotiated_version, str):
            return False

        notification_headers = {
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
            "MCP-Protocol-Version": negotiated_version,
        }
        if isinstance(session_id, str) and session_id:
            notification_headers["Mcp-Session-Id"] = session_id
        initialized_notification = urllib.request.Request(
            url,
            data=json.dumps({
                "jsonrpc": "2.0",
                "method": "notifications/initialized",
            }).encode("utf-8"),
            headers=notification_headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(initialized_notification, timeout=1):
                pass
        except (OSError, urllib.error.URLError, urllib.error.HTTPError):
            return False
        if isinstance(session_id, str) and session_id:
            close_session = urllib.request.Request(
                url,
                headers={
                    "Mcp-Session-Id": session_id,
                    "MCP-Protocol-Version": negotiated_version,
                },
                method="DELETE",
            )
            try:
                with urllib.request.urlopen(close_session, timeout=1):
                    pass
            except urllib.error.HTTPError as error:
                if error.code not in {404, 405}:
                    return False
            except (OSError, urllib.error.URLError):
                return False
        return True

    @staticmethod
    def _pid_exists(pid):
        try:
            os.kill(pid, 0)
        except OSError as error:
            if error.errno == errno.ESRCH or getattr(error, "winerror", None) == 87:
                return False
            return True
        return True

    def stop_mcp_server(self, name):
        server = self.get_server(name)
        if not server:
            raise ValueError(f"'{name}' adında yerel MCP sunucusu kayıtlı değil.")
        with self._connect() as connection:
            running = connection.execute(
                "SELECT pid, port FROM running_mcp_servers WHERE server_id = ?",
                (server["id"],),
            ).fetchone()
        if not running:
            return f"{server['name']} MCP sunucusu bu uygulama oturumunda çalışmıyor."
        url = f"http://127.0.0.1:{running['port']}/mcp"
        if not self._pid_exists(running["pid"]):
            with self._connect() as connection:
                connection.execute(
                    "DELETE FROM running_mcp_servers WHERE server_id = ?",
                    (server["id"],),
                )
            return f"{server['name']} MCP süreci artık çalışmıyor; eski süreç kaydı temizlendi."
        if not self._mcp_http_handshake(url, server["name"]):
            with self._connect() as connection:
                connection.execute(
                    "DELETE FROM running_mcp_servers WHERE server_id = ?",
                    (server["id"],),
                )
            raise RuntimeError(
                f"{url} beklenen MCP sunucusu olarak doğrulanamadı; "
                "başka bir süreç sonlandırılmadı ve eski süreç kaydı temizlendi."
            )
        try:
            if os.name == "nt":
                result = subprocess.run(
                    ["taskkill", "/PID", str(running["pid"]), "/T", "/F"],
                    capture_output=True, text=True, encoding="mbcs", errors="replace", check=False,
                )
                if result.returncode:
                    if (
                        self._pid_exists(running["pid"])
                        or self._mcp_http_handshake(url, server["name"])
                    ):
                        raise RuntimeError(result.stderr.strip() or "taskkill başarısız oldu.")
            else:
                os.kill(running["pid"], 15)
        except OSError as error:
            raise RuntimeError(f"MCP sunucusu durdurulamadı: {error}") from error
        with self._connect() as connection:
            connection.execute(
                "DELETE FROM running_mcp_servers WHERE server_id = ?",
                (server["id"],),
            )
        return f"{server['name']} MCP sunucusu durduruldu."

    def get_server(self, name):
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM mcp_servers WHERE name = ? COLLATE NOCASE", (name.strip(),)
            ).fetchone()
        return dict(row) if row else None

    def get_server_by_id(self, server_id):
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM mcp_servers WHERE id = ?", (server_id,)
            ).fetchone()
        return dict(row) if row else None

    def list_servers(self):
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM mcp_servers ORDER BY name COLLATE NOCASE"
            ).fetchall()
        return [dict(row) for row in rows]

    def create_interactive(self):
        applications = self.discover_applications()
        if not applications:
            self._print_error(
                "Kayıt defterinde çalıştırılabilir dosyası bulunan uygulama bulunamadı. "
                "Şimdilik yalnızca Windows'a kurulu .exe uygulamaları listeleniyor."
            )
            return
        selected = self._select_application(applications)
        if selected is None:
            return
        default_name = selected["name"]
        name = self._ask(f"MCP sunucusu adı [{default_name}]", default=default_name).strip()
        if not name:
            name = default_name
        try:
            server = self.create_server(name, selected)
        except sqlite3.IntegrityError:
            self._print_error(f"'{name}' adında bir MCP sunucusu zaten kayıtlı.")
            return
        except (OSError, ValueError) as error:
            self._print_error(str(error))
            return
        self._print_success(f"'{server['name']}' MCP sunucusu SQLite'a kaydedildi.")
        self._show_server(server)

    def open_settings_gui(self):
        try:
            import tkinter as tk
            from tkinter import messagebox, simpledialog, ttk
        except ImportError as error:
            self._print_error(f"MCP grafik arayüzü açılamadı: {error}")
            return

        root = tk.Tk()
        root.title("Easy Windows Tools · MCP uygulama merkezi")
        root.geometry("1120x720")
        root.minsize(900, 560)
        style = ttk.Style(root)
        if "vista" in style.theme_names():
            style.theme_use("vista")
        notebook = ttk.Notebook(root)
        notebook.pack(fill="both", expand=True, padx=12, pady=12)

        installed_tab = ttk.Frame(notebook, padding=12)
        catalog_tab = ttk.Frame(notebook, padding=12)
        security_tab = ttk.Frame(notebook, padding=12)
        notebook.add(installed_tab, text="Yüklü uygulamalar")
        notebook.add(catalog_tab, text="100 uygulamalı katalog")
        notebook.add(security_tab, text="MCP güvenlik ayarları")

        installed_tree = ttk.Treeview(
            installed_tab, columns=("source", "path"), show="tree headings", height=20,
        )
        installed_tree.heading("source", text="Kaynak")
        installed_tree.heading("path", text="Başlatma yolu")
        installed_tree.column("source", width=230, stretch=False)
        installed_tree.column("path", width=660)
        installed_tree.pack(fill="both", expand=True)
        installed_apps = self.discover_applications()
        for app in installed_apps:
            installed_tree.insert(
                "", "end", iid=app["executable"],
                values=(app["source"], app["executable"]), text=app["name"],
            )
        installed_tree["displaycolumns"] = ("source", "path")
        installed_tree.heading("#0", text="Uygulama")
        installed_tree.column("#0", width=240, stretch=False)

        def refresh_installed_apps():
            nonlocal installed_apps
            installed_apps = self.discover_applications()
            installed_tree.delete(*installed_tree.get_children())
            for app in installed_apps:
                installed_tree.insert(
                    "", "end", iid=app["executable"],
                    values=(app["source"], app["executable"]), text=app["name"],
                )
            installed_count.configure(text=f"Bulunan uygulama: {len(installed_apps)}")

        installed_actions = ttk.Frame(installed_tab)
        installed_actions.pack(fill="x", pady=(10, 0))
        ttk.Button(
            installed_actions,
            text="Seçili uygulama için MCP oluştur",
            command=lambda: create_server_for_selected(),
        ).pack(side="left")
        ttk.Button(
            installed_actions, text="Yüklü uygulama listesini yenile",
            command=refresh_installed_apps,
        ).pack(side="left", padx=6)
        installed_count = ttk.Label(
            installed_actions,
            text=f"Bulunan uygulama: {len(installed_apps)}",
        )
        installed_count.pack(side="right")

        catalog_frame = ttk.Frame(catalog_tab)
        catalog_frame.pack(fill="both", expand=True)
        catalog_tree = ttk.Treeview(
            catalog_frame, columns=("platform",), show="tree headings", height=20,
        )
        catalog_tree.heading("#0", text="Uygulama")
        catalog_tree.heading("platform", text="Paket kimliği / durum")
        catalog_tree.column("#0", width=270, stretch=False)
        catalog_tree.column("platform", width=650)
        catalog_scroll = ttk.Scrollbar(catalog_frame, orient="vertical", command=catalog_tree.yview)
        catalog_tree.configure(yscrollcommand=catalog_scroll.set)
        catalog_tree.pack(side="left", fill="both", expand=True)
        catalog_scroll.pack(side="right", fill="y")
        available_managers = self.available_package_managers()
        for name in MCP_APPLICATION_CATALOG:
            manager_labels = ", ".join(available_managers) or "Paket yöneticisi bulunamadı"
            package = MCP_APP_PACKAGE_IDS.get(name, {}).get(available_managers[0], "arama ile bulunur") if available_managers else "kurulum mevcut değil"
            catalog_tree.insert("", "end", iid=name, text=name, values=(f"{manager_labels} · {package}",))
        catalog_actions = ttk.Frame(catalog_tab)
        catalog_actions.pack(fill="x", pady=(10, 0))
        manager_var = tk.StringVar(value=available_managers[0] if available_managers else "")
        ttk.Label(catalog_actions, text="Paket yöneticisi:").pack(side="left")
        manager_box = ttk.Combobox(
            catalog_actions, textvariable=manager_var, values=available_managers,
            state="readonly", width=16,
        )
        manager_box.pack(side="left", padx=6)
        ttk.Button(
            catalog_actions, text="Seçili uygulamayı kur",
            command=lambda: install_selected_app(),
        ).pack(side="left", padx=6)
        ttk.Button(
            catalog_actions,
            text="7-Zip ile hafif kurulum testi",
            command=lambda: select_lightweight_test_app(),
        ).pack(side="left", padx=6)
        ttk.Button(
            catalog_actions, text="Resmi .dmg/.pkg indirme sayfası",
            command=lambda: open_official_download(),
        ).pack(side="left", padx=6)
        ttk.Label(
            catalog_actions, text=f"{len(MCP_APPLICATION_CATALOG)} katalog girdisi · kurulumdan önce onay alınır",
        ).pack(side="right")

        server_tree = ttk.Treeview(security_tab, columns=("application",), show="tree headings", height=12)
        server_tree.heading("#0", text="MCP sunucusu")
        server_tree.heading("application", text="Bağlı uygulama")
        server_tree.column("#0", width=300)
        server_tree.column("application", width=380)
        server_tree.pack(fill="x", expand=False)
        servers = self.list_servers()
        for server in servers:
            server_tree.insert(
                "", "end", iid=str(server["id"]), text=server["name"],
                values=(server["app_name"],),
            )
        permission_vars = {
            "allow_app_status": tk.BooleanVar(value=False),
            "allow_launch_application": tk.BooleanVar(value=False),
            "allow_close_application": tk.BooleanVar(value=False),
            "allow_screen_capture": tk.BooleanVar(value=False),
            "allow_input_control": tk.BooleanVar(value=False),
        }
        permission_labels = {
            "allow_app_status": "AI uygulama/süreç durumunu okuyabilsin",
            "allow_launch_application": "AI uygulamayı başlatabilsin",
            "allow_close_application": "AI uygulamayı kapatabilsin",
            "allow_screen_capture": "AI ekran görüntüsü alabilsin",
            "allow_input_control": "AI fare/klavye ile işlem yapabilsin",
        }
        permission_frame = ttk.LabelFrame(
            security_tab, text="Seçili MCP için izinler (varsayılan: kapalı)", padding=12,
        )
        permission_frame.pack(fill="x", pady=(12, 0))
        for key, variable in permission_vars.items():
            ttk.Checkbutton(
                permission_frame, text=permission_labels[key], variable=variable,
            ).pack(anchor="w", pady=3)
        security_actions = ttk.Frame(security_tab)
        security_actions.pack(fill="x", pady=10)

        def selected_server():
            selection = server_tree.selection()
            return self.get_server_by_id(int(selection[0])) if selection else None

        def load_server_permissions(_event=None):
            server = selected_server()
            current = self.server_settings(server) if server else self.default_server_settings()
            for key, variable in permission_vars.items():
                variable.set(current[key])

        def save_server_permissions():
            server = selected_server()
            if not server:
                messagebox.showinfo("MCP seçimi", "Önce kayıtlı bir MCP seçin.", parent=root)
                return
            updated = self.update_server_settings(
                server["id"], **{key: variable.get() for key, variable in permission_vars.items()}
            )
            messagebox.showinfo(
                "Ayarlar kaydedildi",
                f"{server['name']} izinleri güncellendi.\n"
                f"Ekran: {'açık' if updated['allow_screen_capture'] else 'kapalı'} · "
                f"Fare/klavye: {'açık' if updated['allow_input_control'] else 'kapalı'}",
                parent=root,
            )

        server_tree.bind("<<TreeviewSelect>>", load_server_permissions)
        ttk.Button(
            security_actions, text="İzinleri kaydet", command=save_server_permissions,
        ).pack(side="left")
        ttk.Label(
            security_tab,
            text="Uygulama başlatma/kapatma, ekran ve fare/klavye izinleri her MCP için ayrı saklanır. "
            "Ekran/klavye erişimi için işletim sisteminin gizlilik izinleri de gerekebilir.",
            wraplength=900,
        ).pack(anchor="w", pady=(4, 0))

        def create_server_for_selected():
            selection = installed_tree.selection()
            if not selection:
                messagebox.showinfo("Uygulama seçin", "Önce yüklü uygulamalardan birini seçin.", parent=root)
                return
            app = next((item for item in installed_apps if item["executable"] == selection[0]), None)
            if not app:
                return
            name = simpledialog.askstring(
                "MCP oluştur", "MCP sunucusu adı:", initialvalue=app["name"], parent=root,
            )
            if not name:
                return
            try:
                server = self.create_server(name, app)
            except (OSError, ValueError, sqlite3.IntegrityError) as error:
                messagebox.showerror("MCP oluşturulamadı", str(error), parent=root)
                return
            server_tree.insert(
                "", "end", iid=str(server["id"]), text=server["name"],
                values=(server["app_name"],),
            )
            server_tree.selection_set(str(server["id"]))
            notebook.select(security_tab)
            load_server_permissions()
            messagebox.showinfo("MCP oluşturuldu", f"{server['name']} MCP'si kaydedildi.", parent=root)

        def selected_catalog_name():
            selection = catalog_tree.selection()
            return selection[0] if selection else None

        def install_selected_app():
            app_name = selected_catalog_name()
            manager = manager_var.get()
            if not app_name:
                messagebox.showinfo("Uygulama seçin", "Önce katalogdan uygulama seçin.", parent=root)
                return
            if not manager:
                messagebox.showerror("Paket yöneticisi yok", "Bu sistemde desteklenen paket yöneticisi bulunamadı.", parent=root)
                return
            try:
                candidates = self.search_catalog_packages(app_name, manager)
            except (OSError, RuntimeError, subprocess.SubprocessError, ValueError) as error:
                messagebox.showerror("Paket araması başarısız", str(error), parent=root)
                return
            if not candidates:
                messagebox.showerror(
                    "Paket bulunamadı",
                    f"{app_name} için {manager} deposunda eşleşme bulunamadı. "
                    "macOS'ta resmi indirme sayfasını açıp .dmg/.pkg yükleyicisini kullanabilirsiniz.",
                    parent=root,
                )
                return
            if len(candidates) > 1:
                package_id = simpledialog.askstring(
                    "Paket kimliği",
                    "Listeden kurulacak paket kimliğini yazın:\n"
                    + "\n".join(
                        f"{item['name']} — {item['id']}" for item in candidates[:20]
                    ),
                    initialvalue=candidates[0]["id"], parent=root,
                )
                if not package_id:
                    return
                if package_id not in {item["id"] for item in candidates}:
                    messagebox.showerror("Geçersiz paket", "Arama sonuçlarından bir paket kimliği seçin.", parent=root)
                    return
            selected_package = next(
                item for item in candidates if item["id"] == package_id
            ) if len(candidates) > 1 else candidates[0]
            package_id = selected_package["id"]
            package_name = selected_package["name"]
            if not messagebox.askyesno(
                "Kurulum onayı",
                f"{app_name} için bulunan '{package_name}' paketini {manager} üzerinden "
                "kurmak istiyor musunuz?\n\n"
                f"Paket kimliği: {package_id}\n"
                "Paket yöneticisi sistemde yönetici izni isteyebilir.",
                parent=root,
            ):
                return

            def perform_install():
                try:
                    result = self.install_package(manager, package_id)
                    output = (result.stdout + "\n" + result.stderr).strip()
                    root.after(
                        0,
                        lambda: messagebox.showinfo(
                            "Kurulum tamamlandı" if result.returncode == 0 else "Kurulum başarısız",
                            f"{package_name} ({package_id})\n\n"
                            f"{output[-5000:] or f'Çıkış kodu: {result.returncode}'}",
                            parent=root,
                        ),
                    )
                except (OSError, RuntimeError, subprocess.SubprocessError) as error:
                    error_message = str(error)
                    root.after(
                        0,
                        lambda message=error_message: messagebox.showerror(
                            "Kurulum başarısız", message, parent=root
                        ),
                    )

            threading.Thread(target=perform_install, daemon=True).start()

        def select_lightweight_test_app():
            if "7-Zip" not in catalog_tree.get_children(""):
                messagebox.showerror(
                    "Test uygulaması bulunamadı",
                    "7-Zip katalog girdisi bulunamadı.",
                    parent=root,
                )
                return
            if not available_managers:
                messagebox.showerror(
                    "Paket yöneticisi yok",
                    "Bu sistemde desteklenen paket yöneticisi bulunamadı.",
                    parent=root,
                )
                return
            manager_var.set(
                "winget" if "winget" in available_managers else available_managers[0]
            )
            catalog_tree.selection_set("7-Zip")
            catalog_tree.focus("7-Zip")
            catalog_tree.see("7-Zip")
            notebook.select(catalog_tab)
            install_selected_app()

        def open_official_download():
            app_name = selected_catalog_name()
            url = MCP_APP_DOWNLOAD_PAGES.get(app_name)
            if sys.platform != "darwin":
                messagebox.showinfo(
                    "macOS yükleyicisi",
                    ".dmg/.pkg doğrudan yükleyici bağlantıları macOS için tasarlanmıştır.",
                    parent=root,
                )
            elif not url:
                messagebox.showinfo(
                    "Resmi indirme sayfası yok",
                    "Bu katalog uygulaması için doğrulanmış indirme sayfası tanımlanmamış.",
                    parent=root,
                )
            else:
                webbrowser.open(url)

        root.mainloop()

    def _select_application(self, applications):
        if HAS_PROMPT_TOOLKIT and sys.stdin.isatty():
            values = [
                (app, f"{app['name']}  ·  {Path(app['executable']).parent}")
                for app in applications
            ]
            try:
                return radiolist_dialog(
                    title="MCP sunucusu için uygulama seç",
                    text="Ok tuşlarıyla gezin · Enter ile seç · Escape ile iptal",
                    values=values, ok_text="Seç", cancel_text="İptal",
                ).run()
            except (EOFError, KeyboardInterrupt):
                return None
        self._show_applications(applications)
        selection = self._ask("MCP için uygulama numarası (q ile iptal)")
        if selection.lower() in {"q", "quit", "iptal"}:
            return None
        try:
            return applications[int(selection) - 1]
        except (ValueError, IndexError):
            self._print_error("Geçerli bir uygulama numarası girin.")
            return None

    def show_servers(self, name=None):
        if name:
            server = self.get_server(name)
            if not server:
                matches = [item for item in self.list_servers() if name.casefold() in item["name"].casefold()]
                if len(matches) == 1:
                    server = matches[0]
                elif len(matches) > 1:
                    self._show_server_list(matches)
                    self._print_info("Birden fazla eşleşme var; tam sunucu adını yazın.")
                    return
                else:
                    self._print_error(f"'{name}' adında kayıtlı bir MCP sunucusu yok.")
                    return
            self._show_server(server)
            return
        servers = self.list_servers()
        if not servers:
            self._print_info("Henüz kayıtlı MCP sunucusu yok. Oluşturmak için /MCP create yazın.")
            return
        self._show_server_list(servers)
        self._print_info("Ayrıntı ve bağlantı JSON'u için /my MCP's <sunucu adı> yazın.")

    def _show_applications(self, applications):
        if HAS_RICH and self.console:
            table = Table(title="Uygulama Seç", border_style="cyan", header_style="bold cyan")
            table.add_column("#", justify="right", style="dim", width=4)
            table.add_column("Uygulama", min_width=18)
            table.add_column("Çalıştırılabilir dosya", overflow="fold")
            for index, app in enumerate(applications, start=1):
                table.add_row(str(index), app["name"], app["executable"])
            self.console.print(table)
            return
        for index, app in enumerate(applications, start=1):
            print(f"{index:>3}. {app['name']} | {app['executable']}")

    def _show_server_list(self, servers):
        if HAS_RICH and self.console:
            table = Table(title="Kayıtlı MCP Sunucuları", border_style="cyan", header_style="bold cyan")
            table.add_column("Ad", style="bold")
            table.add_column("Uygulama")
            table.add_column("Çalıştırılabilir dosya", overflow="fold")
            table.add_column("Oluşturulma")
            for server in servers:
                table.add_row(server["name"], server["app_name"], server["executable"], server["created_at"])
            self.console.print(table)
            return
        for server in servers:
            print(f"{server['name']} -> {server['app_name']} ({server['executable']})")

    def _show_server(self, server):
        configuration = server["configuration"]
        details = (
            f"Ad: {server['name']}\nUygulama: {server['app_name']}\n"
            f"EXE: {server['executable']}\nKaynak: {server['source']}\n"
            f"Oluşturulma: {server['created_at']}\n"
            "Araçlar: launch_application, app_status, close_application"
        )
        if HAS_RICH and self.console:
            self.console.print(Panel(details, title="MCP Sunucusu", border_style="cyan"))
            configuration_data = json.loads(configuration)
            vscode_config = json.dumps(configuration_data["vscode"], ensure_ascii=False, indent=2)
            antigravity_config = json.dumps(configuration_data["antigravity"], ensure_ascii=False, indent=2)
            self.console.print(Panel(Syntax(vscode_config, "json", theme="monokai", word_wrap=True), title="VS Code mcp.json", border_style="green"))
            self.console.print(Panel(Syntax(antigravity_config, "json", theme="monokai", word_wrap=True), title="Antigravity MCP config", border_style="green"))
            http_url = configuration_data.get("http", {}).get("url", "")
            if http_url:
                self.console.print(Panel(http_url, title="Yerel Streamable HTTP MCP uç noktası", border_style="green"))
            self._print_info(
                "VS Code / Antigravity stdio yapılandırmasını kendi MCP ayarlarına ekler. "
                "Yerel HTTP uç noktası yalnızca aynı bilgisayardaki istemcilere açıktır; "
                "bulut MCP istemcileri bu adrese erişemez. "
                "Ekran ve fare/klavye izinleri /MCP ayarlar içinde varsayılan olarak kapalıdır."
            )
        else:
            print(details)
            print(configuration)
            print("Yerel sunucuyu >start MCP <ad> ile başlatın; güvenlik izinleri /MCP ayarlar içinden yönetilir.")

    def _ask(self, message, default=None):
        if HAS_RICH and self.console:
            return Prompt.ask(message, default=default, console=self.console)
        suffix = f" [{default}]" if default else ""
        return input(f"{message}{suffix}: ")

    def _print_info(self, message):
        if HAS_RICH and self.console:
            self.console.print(f"[cyan]ℹ[/] {message}")
        else:
            print(message)

    def _print_success(self, message):
        if HAS_RICH and self.console:
            self.console.print(f"[green]✓[/] {message}")
        else:
            print(message)

    def _print_error(self, message):
        if HAS_RICH and self.console:
            self.console.print(f"[bold red]Hata:[/] {message}")
        else:
            print(f"Hata: {message}")

    def _tracked_process_ids(self, server_id):
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT pid FROM managed_processes WHERE server_id = ?", (server_id,)
            ).fetchall()
        return [row["pid"] for row in rows]

    def _record_process(self, server_id, pid):
        with self._connect() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO managed_processes (server_id, pid, started_at) VALUES (?, ?, ?)",
                (server_id, pid, datetime.now(timezone.utc).isoformat(timespec="seconds")),
            )

    @staticmethod
    def _running_processes(executable):
        if os.name != "nt":
            result = subprocess.run(
                ["ps", "-axo", "pid=,comm="],
                capture_output=True, text=True, encoding="utf-8", errors="replace", check=False,
            )
            image_name = Path(executable.removeprefix("@open:")).name.casefold()
            return [
                {"image": Path(line.strip().split(maxsplit=1)[1]).name, "pid": int(line.strip().split(maxsplit=1)[0])}
                for line in result.stdout.splitlines()
                if len(line.strip().split(maxsplit=1)) == 2
                and Path(line.strip().split(maxsplit=1)[1]).name.casefold() == image_name
                and line.strip().split(maxsplit=1)[0].isdigit()
            ]
        image_name = Path(executable).name
        result = subprocess.run(
            ["tasklist", "/FI", f"IMAGENAME eq {image_name}", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, encoding="mbcs", errors="replace", check=False,
        )
        running = []
        for row in csv.reader(result.stdout.splitlines()):
            if len(row) >= 2 and row[1].isdigit():
                running.append({"image": row[0], "pid": int(row[1])})
        return running

    def launch_application(self, server):
        if not self.server_settings(server)["allow_launch_application"]:
            raise PermissionError(
                "Uygulama başlatma izni kapalı. /MCP ayarlar penceresinden bu MCP için açın."
            )
        executable = server["executable"]
        if executable.startswith("@open:"):
            command = ["open", "-a", executable.removeprefix("@open:")]
        elif os.name == "nt":
            command = [executable]
        else:
            command = shlex.split(executable)
        if not executable.startswith("@open:") and not (Path(command[0]).is_file() or shutil.which(command[0])):
            return f"Uygulama bulunamadı: {executable}"
        try:
            process = subprocess.Popen(command, close_fds=True)
        except OSError as error:
            return f"Uygulama başlatılamadı: {error}"
        self._record_process(server["id"], process.pid)
        return f"{server['app_name']} başlatıldı (PID {process.pid})."

    def application_status(self, server):
        if not self.server_settings(server)["allow_app_status"]:
            raise PermissionError(
                "Uygulama durumu izni kapalı. /MCP ayarlar penceresinden bu MCP için açın."
            )
        try:
            running = self._running_processes(server["executable"])
        except OSError as error:
            return {"application": server["app_name"], "error": str(error), "processes": []}
        managed_ids = set(self._tracked_process_ids(server["id"]))
        return {
            "application": server["app_name"],
            "executable": server["executable"],
            "running": bool(running),
            "processes": running,
            "started_by_this_mcp": [item for item in running if item["pid"] in managed_ids],
        }

    def capture_screen(self, server):
        if not self.server_settings(server)["allow_screen_capture"]:
            raise PermissionError("Ekran görüntüsü izni kapalı; /MCP ayarlar penceresinden açın.")
        try:
            from mss import MSS, tools
            with MSS() as capture:
                screenshot = capture.grab(capture.monitors[0])
                image_data = tools.to_png(screenshot.rgb, screenshot.size)
        except (ImportError, OSError, RuntimeError) as error:
            raise RuntimeError(f"Ekran görüntüsü alınamadı: {error}") from error
        return image_data

    def click_screen(self, server, x, y, button="left", clicks=1):
        if not self.server_settings(server)["allow_input_control"]:
            raise PermissionError("Fare/klavye izni kapalı; /MCP ayarlar penceresinden açın.")
        if button not in {"left", "right", "middle"} or not 1 <= clicks <= 2:
            raise ValueError("Fare düğmesi left/right/middle, tıklama sayısı 1 veya 2 olmalıdır.")
        try:
            import pyautogui
            width, height = pyautogui.size()
            if not 0 <= x < width or not 0 <= y < height:
                raise ValueError(f"Koordinatlar ekran sınırları dışında (0-{width - 1}, 0-{height - 1}).")
            pyautogui.FAILSAFE = True
            pyautogui.click(x=x, y=y, clicks=clicks, button=button)
        except ImportError as error:
            raise RuntimeError("Fare/klavye kontrol paketi eksik; uygulama bağımlılıklarını yükleyin.") from error
        return f"{x},{y} koordinatına {button} tıklama gönderildi."

    def type_into_screen(self, server, text):
        if not self.server_settings(server)["allow_input_control"]:
            raise PermissionError("Fare/klavye izni kapalı; /MCP ayarlar penceresinden açın.")
        if not isinstance(text, str) or not text or len(text) > 2000:
            raise ValueError("Metin 1-2000 karakter arasında olmalıdır.")
        try:
            import pyautogui
            import pyperclip
        except ImportError as error:
            raise RuntimeError("Fare/klavye kontrol paketleri yüklenmemiş.") from error
        old_clipboard = pyperclip.paste()
        try:
            pyperclip.copy(text)
            pyautogui.FAILSAFE = True
            pyautogui.hotkey("command", "v") if sys.platform == "darwin" else pyautogui.hotkey("ctrl", "v")
        except (OSError, RuntimeError) as error:
            raise RuntimeError(f"Metin yapıştırılamadı: {error}") from error
        finally:
            pyperclip.copy(old_clipboard)
        return f"{len(text)} karakter etkin pencereye yazıldı."

    def press_screen_key(self, server, key):
        if not self.server_settings(server)["allow_input_control"]:
            raise PermissionError("Fare/klavye izni kapalı; /MCP ayarlar penceresinden açın.")
        if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9_]{1,20}", key):
            raise ValueError("Tuş adı geçersiz.")
        try:
            import pyautogui
            if key.lower() not in pyautogui.KEYBOARD_KEYS:
                raise ValueError(f"Desteklenmeyen tuş: {key}")
            pyautogui.FAILSAFE = True
            pyautogui.press(key.lower())
        except ImportError as error:
            raise RuntimeError("Fare/klavye kontrol paketi yüklenmemiş.") from error
        return f"{key} tuşu gönderildi."

    def close_application(self, server):
        if not self.server_settings(server)["allow_close_application"]:
            raise PermissionError(
                "Uygulama kapatma izni kapalı. /MCP ayarlar penceresinden bu MCP için açın."
            )
        try:
            running_ids = {item["pid"] for item in self._running_processes(server["executable"])}
        except OSError as error:
            return f"Süreç listesi alınamadı: {error}"
        tracked_ids = set(self._tracked_process_ids(server["id"]))
        targets = sorted(running_ids & tracked_ids)
        if not targets:
            return "Bu MCP sunucusunun başlattığı çalışan bir süreç yok; diğer uygulama örnekleri kapatılmadı."
        closed = []
        for pid in targets:
            if os.name == "nt":
                result = subprocess.run(
                    ["taskkill", "/PID", str(pid), "/T", "/F"],
                    capture_output=True, text=True, encoding="mbcs", errors="replace", check=False,
                )
                succeeded = result.returncode == 0
            else:
                try:
                    os.kill(pid, 15)
                    succeeded = True
                except OSError:
                    succeeded = False
            if succeeded:
                closed.append(pid)
        if closed:
            with self._connect() as connection:
                connection.executemany(
                    "DELETE FROM managed_processes WHERE server_id = ? AND pid = ?",
                    [(server["id"], pid) for pid in closed],
                )
        if not closed:
            return "MCP tarafından başlatılan süreçler kapatılamadı."
        return f"{server['app_name']} uygulamasının MCP tarafından başlatılan süreçleri kapatıldı: {closed}."


def run_mcp_server(server_id, transport="stdio", port=8800):
    manager = MCPManager()
    try:
        server = manager.get_server_by_id(int(server_id))
    except (TypeError, ValueError):
        server = None
    if not server:
        print(f"MCP server kaydı bulunamadı: {server_id}", file=sys.stderr)
        raise SystemExit(2)
    from mcp.server.fastmcp import FastMCP
    mcp = FastMCP(
        name=server["name"],
        instructions=(
            f"{server['app_name']} MCP'si. Ekran ve fare/klavye araçları varsayılan olarak kapalıdır; "
            "yalnızca bu sunucuya kullanıcının açıkça verdiği izinleri kullan. "
            "Uygulama kapatmayı yalnızca bu MCP'nin başlattığı süreçlerle sınırla. "
            "Keyfi kabuk komutu çalıştırma aracı sunulmaz."
        ),
        host="127.0.0.1",
        port=port,
    )

    @mcp.tool()
    def launch_application() -> str:
        """Start the registered desktop application."""
        return manager.launch_application(server)

    @mcp.tool()
    def app_status() -> dict:
        """Return running processes for the registered application."""
        return manager.application_status(server)

    @mcp.tool()
    def close_application() -> str:
        """Close only application processes started by this MCP server."""
        return manager.close_application(server)

    @mcp.tool()
    def screen_capture():
        """Return a screenshot when screen capture is enabled in this server's settings."""
        from mcp.server.fastmcp import Image
        return Image(data=manager.capture_screen(server), format="png")

    @mcp.tool()
    def click_at(x: int, y: int, button: str = "left", clicks: int = 1) -> str:
        """Click the current desktop at a screen coordinate when input control is enabled."""
        return manager.click_screen(server, x, y, button, clicks)

    @mcp.tool()
    def type_text(text: str) -> str:
        """Paste text into the focused application when input control is enabled."""
        return manager.type_into_screen(server, text)

    @mcp.tool()
    def press_key(key: str) -> str:
        """Press one key by name when input control is enabled."""
        return manager.press_screen_key(server, key)

    if transport == "streamable-http" and not 1024 <= port <= 65535:
        raise ValueError("MCP HTTP portu 1024-65535 arasında olmalıdır.")
    mcp.run(transport=transport)


def main():
    if len(sys.argv) >= 3 and sys.argv[1] == "--mcp-server":
        import argparse
        parser = argparse.ArgumentParser(add_help=False)
        parser.add_argument("--mcp-server", required=True)
        parser.add_argument("--transport", choices=("stdio", "streamable-http"), default="stdio")
        parser.add_argument("--port", type=int, default=8800)
        args = parser.parse_args(sys.argv[1:])
        run_mcp_server(args.mcp_server, transport=args.transport, port=args.port)
    else:
        KeepAwakeApp().run()


if __name__ == "__main__":
    main()