# -*- coding: utf-8 -*-
"""Easy Windows Tools: Windows power and display controls for the terminal."""

# Copyright (C) 2026 Robin Güneş
#
# This program is free software: you can redistribute it and/or modify it under
# the terms of the GNU General Public License as published by the Free Software
# Foundation, either version 3 of the License, or (at your option) any later
# version.
#
# This program is distributed in the hope that it will be useful, but WITHOUT
# ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS
# FOR A PARTICULAR PURPOSE. See the GNU General Public License for details.
# You should have received a copy of the GNU General Public License along with
# this program. If not, see <https://www.gnu.org/licenses/>.

import ctypes
import csv
import json
import os
import re
import shlex
import sqlite3
import subprocess
import sys
import time
from contextlib import ExitStack, contextmanager
from datetime import datetime, timedelta, timezone
from ctypes import wintypes
from pathlib import Path

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
APP_VERSION = "2.0.0"
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
                    "/ekran", "/ekran liste", "/ekran 1", "/ekran 2", "ekran kaydet",
                    "/MCP create", "/my MCP's",
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
            f"[bold #E07A5F] ▟█▙ [/][bold #38BDF8]{APP_NAME.upper()}[/]  [dim #64748B]│  Windows Power & Display Tools[/]"
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
            print("\nKomutlar:\n  başlat [süre] - Uyku engelini başlatır\n  durdur - Uyanık kalmayı kapatır\n  ekran açık/kapalı - Ekran korumasını ayarlar\n  /ekran [numara] - Kayıt için ekranı seçer; /ekran liste ile görüntüle\n  pomodoro - 25 dk odak oturumu\n  toplantı - 60 dk toplantı oturumu\n  /record [süre] - Ekranı MP4 kaydeder; Ctrl+C ile durdurur\n  /record ayarlar - Kalıcı FPS ve çözünürlük ayarlarını değiştirir\n  /record [süre] --fps 30 --resolution 1280x720 --monitor 2 - Bu kayıt için kalite/ekranı seçer\n  çalıştır <komut> - Komut bitene kadar uyanık tutar\n  /MCP create - Bir uygulama için MCP sunucusu oluşturur\n  /my MCP's [ad] - MCP sunucularını ve yapılandırmasını gösterir\n  lisans /license - Telif, garanti ve lisans bilgisini gösterir\n  durum - Sistem durumunu gösterir\n  çıkış - Uygulamadan çıkar\n")
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
            "FPS ve çözünürlük seçim panelini açar; seçimler sonraki kayıtlar için saklanır"
        )
        table.add_row(
            "",
            "/record 30dk --fps 30 --resolution 1280x720 --monitor 2",
            "Yalnızca bu kayıt için süreyi, kaliteyi ve ekranı değiştirir"
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

            saved = ScreenRecorder.save_settings(fps, resolution)
        except (OSError, ValueError) as error:
            self._print_error(f"Kayıt ayarları kaydedilemedi: {error}")
            return

        self.recording_settings = saved
        resolution_label = (
            f"{saved['resolution'][0]}x{saved['resolution'][1]}"
            if saved["resolution"] else "Monitörün doğal çözünürlüğü"
        )
        self._print_info(f"Varsayılan kayıt kalitesi: {saved['fps']} FPS · {resolution_label}")

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
        options = {"duration": None, "fps": None, "resolution": None, "monitor_number": None}
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

    def record_screen(self, duration=None, fps=None, resolution=None, monitor_number=None):
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
                self.console.print(Panel(
                    f"[bold #F87171]● KAYIT[/]  [dim]Ekran {selected_monitor} · {selected_fps} FPS · {resolution_label} · MP4 / H.264[/]\n"
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
        except (OSError, RuntimeError, subprocess.SubprocessError) as error:
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
            self._print_error("Bu uygulama yalnızca Windows üzerinde çalışmaktadır.")
            return

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

            if normalized in {"/mcp create", "mcp create"}:
                self.mcp_manager.create_interactive()
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
    def __init__(self, output_dir=None, fps=15, resolution=None, monitor_number=1, capture_factory=None, ffmpeg_path=None):
        videos_dir = Path.home() / "Videos"
        self.output_dir = Path(output_dir) if output_dir else videos_dir / "Easy Windows Tools"
        self.fps = self.validate_fps(fps)
        self.resolution = self.parse_resolution(resolution)
        self.monitor_number = self.validate_monitor_number(monitor_number)
        self.capture_factory = capture_factory
        self.ffmpeg_path = ffmpeg_path

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
    def settings_path():
        app_data = Path(os.environ.get("APPDATA", Path.home()))
        return app_data / "EasyWindowsTools" / "recording.json"

    @classmethod
    def load_settings(cls):
        defaults = {"fps": 15, "resolution": None, "monitor": 1}
        try:
            settings = json.loads(cls.settings_path().read_text(encoding="utf-8"))
            defaults["fps"] = cls.validate_fps(settings.get("fps", defaults["fps"]))
            defaults["resolution"] = cls.parse_resolution(settings.get("resolution"))
            defaults["monitor"] = cls.validate_monitor_number(settings.get("monitor", 1))
        except (OSError, ValueError, TypeError, AttributeError):
            pass
        return defaults

    @classmethod
    def save_settings(cls, fps, resolution, monitor_number=None):
        parsed_resolution = cls.parse_resolution(resolution)
        if monitor_number is None:
            monitor_number = cls.load_settings()["monitor"]
        settings = {
            "fps": cls.validate_fps(fps),
            "resolution": f"{parsed_resolution[0]}x{parsed_resolution[1]}" if parsed_resolution else None,
            "monitor": cls.validate_monitor_number(monitor_number),
        }
        settings_path = cls.settings_path()
        settings_path.parent.mkdir(parents=True, exist_ok=True)
        settings_path.write_text(json.dumps(settings, indent=2), encoding="utf-8")
        settings["resolution"] = parsed_resolution
        return settings

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
                    "-framerate", str(self.fps), "-i", "pipe:0", "-an",
                    "-vf", video_filter,
                    "-c:v", "libx264", "-preset", "ultrafast", "-crf", "23",
                    "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(output_path),
                ]
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
                        process.stdin.write(frame)
                        frame_count += 1
                        next_frame_at += 1 / self.fps
                        delay = next_frame_at - time.perf_counter()
                        if delay > 0:
                            time.sleep(delay)
                        else:
                            next_frame_at = time.perf_counter()
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
        if os.name != "nt":
            return []
        import winreg

        applications = {}
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
                                display_icon = winreg.QueryValueEx(subkey, "DisplayIcon")[0]
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
        return sorted(applications.values(), key=lambda item: item["name"].casefold())

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
        return {
            "vscode": {"servers": {key: {"type": "stdio", **command}}},
            "antigravity": {"mcpServers": {key: command}},
        }

    def create_server(self, name, app):
        name = name.strip()
        executable = str(Path(app["executable"]).resolve())
        if not name:
            raise ValueError("MCP sunucusu için bir ad girin.")
        if not Path(executable).is_file():
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
            vscode_config = json.dumps(json.loads(configuration)["vscode"], ensure_ascii=False, indent=2)
            antigravity_config = json.dumps(json.loads(configuration)["antigravity"], ensure_ascii=False, indent=2)
            self.console.print(Panel(Syntax(vscode_config, "json", theme="monokai", word_wrap=True), title="VS Code mcp.json", border_style="green"))
            self.console.print(Panel(Syntax(antigravity_config, "json", theme="monokai", word_wrap=True), title="Antigravity MCP config", border_style="green"))
            self._print_info("İlgili yapılandırma bloğunu istemcinizin MCP ayarına ekleyin; istemci stdio sunucusunu arka planda başlatır.")
        else:
            print(details)
            print(configuration)
            print("MCP istemcisi yapılandırmayı yükleyince sunucuyu başlatır.")

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
        executable = server["executable"]
        if not Path(executable).is_file():
            return f"Uygulama bulunamadı: {executable}"
        try:
            process = subprocess.Popen([executable], close_fds=True)
        except OSError as error:
            return f"Uygulama başlatılamadı: {error}"
        self._record_process(server["id"], process.pid)
        return f"{server['app_name']} başlatıldı (PID {process.pid})."

    def application_status(self, server):
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

    def close_application(self, server):
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
            result = subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                capture_output=True, text=True, encoding="mbcs", errors="replace", check=False,
            )
            if result.returncode == 0:
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


def run_mcp_server(server_id):
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
            f"{server['app_name']} masaüstü uygulamasını yönet. "
            "Yalnızca bu MCP sunucusunun başlattığı süreçleri kapat."
        ),
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

    mcp.run(transport="stdio")


def main():
    if len(sys.argv) == 3 and sys.argv[1] == "--mcp-server":
        run_mcp_server(sys.argv[2])
    else:
        KeepAwakeApp().run()


if __name__ == "__main__":
    main()
