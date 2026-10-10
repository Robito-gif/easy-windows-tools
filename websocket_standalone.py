"""Local-only WebSocket services for Easy Windows Tools."""

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

import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import string
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
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
