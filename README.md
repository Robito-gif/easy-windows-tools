# Easy Windows Tools 🛠️

**Easy Windows Tools** is a lightweight, terminal-based Windows utility suite written in Python.

Copyright (C) 2026 Robin Güneş.

**Current version:** 2.2.0

It brings useful Windows functions together in a command-line application, including keeping the computer awake, controlling display behavior, recording the screen, and managing MCP servers. The MCP application center also discovers installed desktop apps and searches a 100-entry software catalog.

> **Platform:** Windows (power and recording features); Windows, macOS, and supported Linux distributions (MCP application center)
> **Language:** Python
> **Interface:** Terminal / CLI, with graphical MCP and WebSocket settings windows
> **License:** GNU General Public License v3 only (GPL-3.0-only)

---

## ✨ Features

### ⚡ Power & Wake Management

Prevent Windows from entering sleep mode while you are working.

* Enable continuous wake mode
* Set a specific wake duration
* Automatically restore normal power behavior when the session ends
* Stop an active session at any time with `Ctrl + C`

Example:

```text
başlat
```

or:

```text
başlat 30dk
```

Supported duration examples include:

```text
30dk
1h
```

---

### 🍅 Pomodoro Mode

Start a **25-minute focus session** that keeps the computer awake.

```text
pomodoro
```

The application automatically restores normal power behavior when the session finishes.

---

### 🤝 Meeting Mode

Start a **60-minute meeting session**.

```text
toplantı
```

This is useful for online meetings, presentations, or other situations where Windows should remain awake.

---

### 🖥️ Display Control

Control whether Windows should allow the display to turn off.

Turn display protection on:

```text
ekran açık
```

Turn it off:

```text
ekran kapalı
```

Slash commands are also supported:

```text
/display on
/display off
```

---

### ⏺️ Screen Recording

Easy Windows Tools includes built-in screen recording functionality.

Start recording:

```text
/record
```

You can also specify a recording duration:

```text
/record 30dk
```

#### Monitor Selection

List available displays:

```text
/ekran liste
```

Select a display for recording:

```text
/ekran 2
```

#### Recording Settings

Open the recording settings:

```text
/record ayarlar
```

You can configure persistent recording settings such as:

* FPS
* Resolution

You can also override settings for a single recording:

```text
/record 30dk --fps 30 --resolution 1280x720 --monitor 2
```

#### Microphone and System Audio (v2.1.0)

Audio capture is off by default. Open the recording settings to choose a
microphone and a Windows audio input device for system sound:

```text
/record ayarlar
```

The selected microphone and system audio are mixed into the MP4 recording.
Audio source selections are saved for future recordings. To override them for a
single recording:

```text
/record 30dk --microphone "Microphone device name" --system-audio "Stereo Mix"
```

Use `--no-audio` to make a single silent recording even when audio sources are
saved in the settings:

```text
/record 30dk --no-audio
```

Windows/FFmpeg can only record system playback when Windows exposes a loopback
input device (for example, **Stereo Mix**). If no such device appears in the
settings list, enable it in Windows sound input settings or use a loopback
device supplied by your audio driver. Microphone recording remains available
independently.

Press `Ctrl + C` to stop an active recording.

---

### 🛠️ Command Runner

Run another command or script while automatically keeping Windows awake until the process finishes.

```text
çalıştır <komut>
```

Example:

```text
çalıştır python test.py
```

Slash syntax is also supported:

```text
/run <command>
```

This is useful for long-running scripts, builds, downloads, and other processes where Windows should not enter sleep mode while the command is running.

---

### 🔌 MCP Support

The MCP application center can discover installed desktop applications and search a
100-entry application catalog. The catalog includes Godot, Unity Hub, Unreal Engine,
Blender, Chrome, Visual Studio, Visual Studio Code, MCreator, SQLite, Blockbench,
IntelliJ IDEA, and other development, design, media, and desktop tools.

```text
/MCP ayarlar
```

The window has tabs for discovered applications, the catalog, and per-server
security permissions. The catalog includes a **“7-Zip ile hafif kurulum testi”**
shortcut that selects the small 7-Zip package and starts the normal package
search and confirmation flow; it does not install anything until you confirm.
A catalog installation always shows the selected package
name and package ID and requires confirmation before invoking the detected package
manager. Package results are searched and verified against the package manager
instead of treating a configured ID as proof that a package exists. On macOS,
the official-download button opens a listed vendor page; it does not download or
install a `.dmg`/`.pkg` automatically.

Create a server for an already installed application:

```text
/MCP create
```

Configure it in a development MCP client using the displayed VS Code or Antigravity
stdio configuration. The server provides generic application launch/status/close,
screenshot, click, text-entry, and key-press tools; it does not provide
application-specific project editing tools.

Each server's permissions are off by default. Application status, application
launch/close, screenshot capture, and mouse/keyboard input permissions can be
changed separately in `/MCP ayarlar`. Permission changes are read from the database
for each tool call, so disabling a permission takes effect without restarting the
MCP server. Mouse/keyboard control also requires the declared `pyautogui` and
`pyperclip` dependencies and may require operating-system privacy permissions.

Start or stop a registered local Streamable HTTP MCP server:

```text
>start MCP <server name>
>stop MCP <server name>
```

The HTTP server binds to `127.0.0.1` and startup performs an MCP initialization
handshake before reporting success. This endpoint is local-only; cloud-hosted MCP
clients need a separately secured, remotely reachable MCP endpoint and cannot
connect to this loopback URL directly.

View saved MCP servers and their configuration:

```text
/my MCP's
```

---

### 🌐 Local WebSocket Connections (v2.2.0)

Easy Windows Tools can host authenticated WebSocket connections on the local
computer without internet access. Every connection binds only to `127.0.0.1`;
remote computers cannot connect, and the app does not create an internet or
public-network tunnel.

Create a connection with `-websocket create` or `-websocket yapmak`, and manage
saved connections with `>websocket ayarlar`. The separate settings window lets
you create, edit, start, stop, delete, and regenerate the PIN for each connection.
The setup form includes a connection name, type, port, and simultaneous-client
limit. Use `/websocket başlat` or `/websocket start` to start every saved
connection; add a name to start just one.

Two local connection types are available:

* **Application server:** a client authenticates and exchanges JSON request and
  response messages with Easy Windows Tools. Other Python code can register a
  request handler using `LocalWebSocketManager.register_message_handler`.
* **Local tunnel / relay:** PIN-authenticated local clients send JSON messages to
  each other through the named relay.

Each connection gets a cryptographically generated 11-digit PIN. It is shown
only once when created or regenerated; the database stores a salted scrypt hash,
not the PIN itself. If it is lost, regenerate it in the settings window and update
the client. Authentication is the first JSON message:

```json
{"type":"authenticate","pin":"01234567890"}
```

On success, the server replies with a JSON `authenticated` message. For application
server connections, send `{"type":"ping"}` for a `pong` response, or send any JSON
object to receive a JSON response (or a response from the registered Python
handler). For relay connections, send
`{"type":"message","sender":"app-name","payload":{"key":"value"}}`;
other authenticated clients receive the payload, and the sender receives a
`relay_ack` with the number of recipients.

The local socket is not encrypted with TLS: loopback traffic remains on this
computer, while the PIN authenticates clients. Do not bind or proxy it to a
network interface or forward the port. Internet connectivity is not used by the
running WebSocket services; installing the `websockets` Python dependency itself
requires the usual package-install source unless already present.

The manager is embedded in `easy_windows_tools.py` and remains available as
`local_websocket.py` for compatibility. `websocket_standalone.py` is the separately
packaged source copy for future applications that want to reuse this implementation:

```python
from websocket_standalone import LocalWebSocketManager

manager = LocalWebSocketManager()
```

All copies remain under this project's **GPL-3.0-only** license. Reuse and
redistribution must retain the license and copyright notices and comply with
the included `LICENSE`; the reusable copy is not offered under a permissive
or proprietary license.

---

### 📊 System Status

Check the current wake and display state:

```text
durum
```

or:

```text
/status
```

---

### 🧹 Terminal Utilities

Clear the terminal:

```text
temizle
```

or:

```text
/clear
```

Display the complete command reference:

```text
yardım
```

or:

```text
/help
```

---

## 📦 Installation

### Requirements

You need:

* Windows
* Python 3.10 or newer
* Git for Windows (needed to install directly from GitHub)
* `pip`

### Install the `EWT` Terminal Command from GitHub

Install `pipx` for your Windows user:

```powershell
python -m pip install --user pipx
python -m pipx ensurepath
```

Close and reopen the terminal so the updated `PATH` takes effect. Install Easy Windows Tools directly from GitHub:

```powershell
python -m pipx install git+https://github.com/Robito-gif/easy-windows-tools.git
```

You can now launch it from CMD or PowerShell:

```text
start EWT
```

To run it in the current terminal instead of opening a new window, use `EWT`.
To install a newer GitHub version later, run `python -m pipx upgrade easy-windows-tools`.

To work on the source code, clone the repository separately:

```bash
git clone https://github.com/Robito-gif/easy-windows-tools.git
cd easy-windows-tools
```

### Create a Virtual Environment

It is recommended to use a virtual environment:

```bash
python -m venv .venv
```

Activate it:

```bash
.venv\Scripts\activate
```

### Install Dependencies

Install the required Python packages:

```bash
pip install -r requirements.txt
```

---

## 🚀 Usage

Start the application with:

```bash
python easy_windows_tools.py
```

Alternatively, on Windows you can use:

```text
baslat.bat
```

Once the application starts, use:

```text
yardım
```

to display the available commands.

---

## 📋 Command Overview

| Command            | Description                               |
| ------------------ | ----------------------------------------- |
| `başlat`           | Start continuous wake mode                |
| `başlat <süre>`    | Start a timed wake session                |
| `durdur`           | Stop wake mode                            |
| `pomodoro`         | Start a 25-minute focus session           |
| `toplantı`         | Start a 60-minute meeting session         |
| `ekran açık`       | Prevent the display from turning off      |
| `ekran kapalı`     | Allow normal display behavior             |
| `/record`          | Start screen recording                    |
| `/record <süre>`   | Record for a specific duration            |
| `/record ayarlar`  | Configure recording settings              |
| `--microphone` / `--system-audio` | Include audio sources in a recording |
| `/ekran liste`     | List available displays                   |
| `/ekran <numara>`  | Select a display for recording            |
| `çalıştır <komut>` | Run a command while keeping Windows awake |
| `/MCP create`      | Create an MCP server                      |
| `/MCP ayarlar`     | Open the application catalog and security settings |
| `>start MCP <name>`| Start a registered local MCP server       |
| `>stop MCP <name>` | Stop a registered local MCP server        |
| `/my MCP's`        | View MCP configurations                   |
| `/websocket başlat [ad]` / `/websocket start [name]` | Start all or one local WebSocket |
| `>websocket ayarlar` | Open local WebSocket settings            |
| `-websocket create` / `-websocket yapmak` | Create a local WebSocket |
| `lisans`           | Show license and warranty details         |
| `durum`            | Show current system status                |
| `temizle`          | Clear the terminal                        |
| `yardım`           | Show the command reference                |
| `çıkış`            | Exit the application                      |

Most commands are also available through English-style slash commands such as `/start`, `/stop`, `/status`, `/help`, `/run`, and `/exit`.

---

## 📁 Project Structure

```text
easy-windows-tools/
│
├── easy_windows_tools.py
├── pyproject.toml
├── baslat.bat
├── requirements.txt
├── README.md
├── LICENSE
└── .gitignore
```

The project does **not** require the `.venv` directory to be included in the repository.

---

## 🔮 Future Features

Easy Windows Tools is still under development. Planned improvements may include:

* 🧰 More Windows system utilities
* 📊 More detailed system monitoring
* 🔋 Advanced power-management controls
* 🎥 More screen-recording options
* 🖥️ Improved multi-monitor management
* ⚙️ More configurable settings
* 🔌 Expanded MCP integration
* 🤖 Additional AI-assisted Windows tools
* 🧩 A modular plugin/tool architecture
* 🎨 Improved terminal interface
* 🌐 Additional localization
* 📦 Easier installation and distribution
* 🪟 Optional graphical interface in the future

More features will be added as the project develops.

---

## 🤝 Contributing

Contributions, suggestions, bug reports, and feature ideas are welcome.

If you find a bug or have an idea for improving Easy Windows Tools, feel free to open an **Issue** or submit a **Pull Request**.

Before submitting a pull request, please make sure that your changes work correctly on Windows and do not introduce unnecessary dependencies.

---

## ⚠️ Notes

Easy Windows Tools interacts with Windows power-management and system functionality.

Some features may behave differently depending on:

* Windows version
* Hardware configuration
* User permissions
* Installed dependencies
* Display configuration

Use system-level features carefully and review commands before executing them.

---

## 📜 License

Easy Windows Tools is licensed under the:

GNU General Public License v3 only (GPL-3.0-only)

Copyright (C) 2026 Robin Güneş.

See the `LICENSE` file for the complete license text.

---

## ⭐ Project

If you find Easy Windows Tools useful, consider giving the repository a ⭐ on GitHub.

**Easy Windows Tools — useful Windows utilities, from the terminal.**
