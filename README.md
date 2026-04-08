# FlexRadio → Wavelog Bridge

Connects a **FlexRadio** transceiver to **[Wavelog](https://wavelog.org)**, transmitting frequency, operating mode, and TX power in real time — fully automatically and without any manual intervention.

Works with **SmartSDR** (Windows) and **[AetherSDR](https://github.com/ten9876/AetherSDR)** (Linux, macOS, Windows) as the SDR client. Runs on **Windows, macOS, and Linux**.

Implemented against the **FlexLib API v4.1.5** (FlexLib_API_v4.1.5.39794).

---

## Features

- **Automatic device discovery** via VITA-49 UDP broadcast (port 4992) — last-used radio pre-selected
- **SmartSDR & AetherSDR compatible** — works with either client running on any platform
- **Multi-client filtering** — correctly handles simultaneous connections (SmartSDR + AetherSDR + Maestro)
- **TX slice tracking** — always reports the active transmit slice; manual slice selection available
- **Real-time transmission** to Wavelog `POST /api/radio`, configurable interval (1–120 s)
- **TX power reporting** — reads `rfpower` from the radio and sends it to Wavelog as `power` (watts)
- **Auto-reconnect** — automatically reconnects after connection loss, configurable interval
- **Auto-retry for Wavelog** — retries silently when Wavelog is temporarily unreachable
- **System tray operation** — runs in the background; double-click tray icon to reopen
- **Dedicated Quit button** — closes the program completely (window close minimises to tray)
- **Wavelog connection test** — independent of radio connection, with detailed diagnostics
- **Cross-platform** — Windows, macOS, Linux; config and log files stored next to the script

---

## SDR Client Compatibility

The bridge connects to the FlexRadio TCP command API on port 4992. Both SmartSDR and AetherSDR use the same SmartSDR protocol, so the bridge works transparently with either.

| Feature | SmartSDR | AetherSDR |
|---------|----------|-----------|
| UDP Discovery | ✓ | ✓ |
| TCP command channel | ✓ | ✓ |
| Slice frequency / mode | ✓ | ✓ |
| TX power (`rfpower`) | ✓ | ✓ |
| FreeDV modes (FDV / FDVU / FDVM) | FDV | FDVU, FDVM |
| Multi-client filtering | automatic | automatic |

When AetherSDR and another client (SmartSDR, Maestro) are connected at the same time, the **Multi-client filtering** option in the Configuration tab ensures the bridge only tracks slices belonging to its own connection. This setting is enabled by default and harmless when running with a single client.

---

## Requirements

| Component | Requirement |
|-----------|-------------|
| Operating System | Windows 10/11, macOS 12+, or Linux |
| Python | 3.11 or newer |
| SDR Client | SmartSDR (Windows) **or** AetherSDR (Linux / macOS / Windows) |
| FlexRadio | Any SmartSDR-compatible model (FLEX-6000 / 8000 series) |
| Wavelog | Any version with an active API key (Read + Write) |

---

## Installation

### Windows

1. Install **Python 3.12** from the Microsoft Store (search "Python 3.12")
2. Extract all files into a folder
3. Double-click `start.bat`

The script automatically locates the Python installation, creates a virtual environment in `venv\`, installs `PyQt6` and `requests`, and starts the program without a console window.

**Manual alternative:**
```
python -m venv venv
venv\Scripts\pip install -r requirements.txt
venv\Scripts\pythonw flex_wavelog_bridge.py
```

### macOS / Linux

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
python flex_wavelog_bridge.py
```

Or use `start.sh` (provided for Linux / macOS):
```bash
chmod +x start.sh
./start.sh
```

**Linux system tray note:** The tray icon requires a notification daemon (most desktop environments include one). On minimal setups without a tray daemon the program remains fully functional — closing the window exits the program normally instead of minimising to the tray.

---

## Getting Started

### 1. Configure Wavelog

Open the **Configuration** tab:

- **Wavelog URL** — e.g. `https://log.mycall.com` (no trailing `/`)
- **API Key** — create one in Wavelog under *User Account → API Keys* (Read + Write)
- **Radio Name** — any name; this is how it appears in Wavelog
- **Update Interval** — how often data is sent to Wavelog (default: 5 s)
- **Multi-client filtering** — keep enabled when running alongside AetherSDR or Maestro

Click **"Save Settings"**, then **"Test Wavelog Connection"**.

### 2. Start the SDR client

Start either **SmartSDR** or **AetherSDR** and connect to your radio. The bridge works with whichever client is running — or both simultaneously.

### 3. Connect the bridge

Go to the **Control** tab:

1. Click **"Search Device"** — listens for 4 seconds via UDP broadcast; the last-used radio is automatically pre-selected
2. Select the found device and click **"Connect"**
3. Alternatively: enter the IP address manually (required if UDP port 4992 is occupied by another application)

### 4. Auto-reconnect

Enable **"Automatically reconnect on connection loss"** in the Control tab and set the interval (5–300 s). The bridge reconnects as soon as the SDR client is available again.

### 5. Slice selection

After a successful connection all open slices appear in the slice selector:

| Mode | Behaviour |
|------|-----------|
| Auto (TX Slice) | Follows the slice with `tx=1` automatically |
| Manual | Fixed selection regardless of TX status |

In Auto mode the lowest-numbered slice is used as fallback when no TX slice is active.

### 6. TX power

The bridge reads `rfpower` (0–100) from the radio transmit status and forwards it as `power` (integer, watts) to the Wavelog API. The value appears in the orange indicator in the status bar and is only sent when greater than 0.

---

## Closing vs. Quitting

| Action | Result |
|--------|--------|
| Click **✕** in the window title bar | Minimises to system tray — bridge keeps running |
| Click **✕ Quit** button in the header | Fully terminates the program |
| Tray menu → **Quit** | Fully terminates the program |

On systems without a system tray, closing the window exits the program normally.

---

## Autostart

**Windows:** Press `Win+R`, type `shell:startup`, and place a shortcut to `start.bat` in that folder. Enable **"Automatically connect on startup"** in the app.

**Linux:** Create `~/.config/autostart/flex-wavelog-bridge.desktop`:
```ini
[Desktop Entry]
Type=Application
Name=Flex-Wavelog-Bridge
Exec=/path/to/start.sh
Hidden=false
X-GNOME-Autostart-enabled=true
```

**macOS:** Create a LaunchAgent plist in `~/Library/LaunchAgents/`:
```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
    "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.dk4dj.flex-wavelog-bridge</string>
  <key>ProgramArguments</key>
  <array>
    <string>/path/to/venv/bin/python</string>
    <string>/path/to/flex_wavelog_bridge.py</string>
  </array>
  <key>RunAtLoad</key><true/>
</dict></plist>
```

---

## Files

```
flex_wavelog_bridge.py   Main program
requirements.txt         Python dependencies (PyQt6, requests)
start.bat                Windows launcher script
start.sh                 Linux / macOS launcher script
README.md                This file
CLAUDE.md                Technical documentation for developers
```

**Data files** (created automatically, stored next to `flex_wavelog_bridge.py`):
```
bridge.log               Detailed log (DEBUG level)
config.json              Persistent configuration
```

---

## Mode Mapping: FlexRadio → ADIF / Wavelog

| SDR Client Mode | Wavelog / ADIF |
|-----------------|----------------|
| USB, LSB | SSB |
| AM, SAM | AM |
| FM, NFM, DFM | FM |
| CW | CW |
| RTTY | RTTY |
| DIGU, DIGL, FDV, FDVU, FDVM | DIGI |

`FDVU` and `FDVM` are FreeDV modes specific to AetherSDR. Unknown modes are passed through unchanged.

---

## Troubleshooting

| Problem | Cause | Solution |
|---------|-------|----------|
| Discovery finds no device | SDR client not running, UDP/4992 blocked | Start SmartSDR or AetherSDR; check firewall; enter IP manually |
| Port 4992 not available | Another application holds UDP/4992 | Enter IP manually in the dialog |
| Connection refused | SDR client not running / wrong IP | Check SDR client status and IP address |
| Timeout | Different subnet / VPN / firewall | Check reachability with `ping` |
| Wrong slice reported | Multi-client mode, filter off | Enable "Multi-client filtering" in Configuration tab |
| Wavelog 401 | API key invalid or insufficient permissions | Create a new key with Read + Write |
| Wavelog 404 | Wrong URL | Try the URL with `/index.php/` |
| Wavelog 302 | HTTP/HTTPS mismatch | Use `https://` instead of `http://` (or vice versa) |
| No slice visible | No slice opened in SDR client | Open at least one slice in SmartSDR or AetherSDR |
| Wrong slice | No TX slice active | Activate TX in the SDR client or use Manual mode |
| Program not visible (Windows) | `pythonw` starts without a window | Check the system tray (arrow next to the clock) |
| No power value shown | Radio not transmitting | Value only appears during active TX |
| Tray icon missing (Linux) | No notification daemon installed | Program remains functional; closing the window exits normally |

---

## Changelog

### v3.0 (2026)
- **New:** AetherSDR compatibility — works with SmartSDR and AetherSDR interchangeably
- **New:** Multi-client filtering via `client_handle` — correct behaviour when multiple SDR clients are connected simultaneously
- **New:** `keepalive enable` on connect — prevents radio-side timeout with AetherSDR
- **New:** `FDVU` / `FDVM` mode mapping — FreeDV modes from AetherSDR mapped to DIGI
- **New:** `client gui` / `client station` registration — proper multi-client announcement
- **New:** Cross-platform support — Windows, macOS, Linux
- **New:** Config and log files stored next to the script (no platform-specific paths)
- **New:** Portable socket error codes using `errno` module
- **New:** System tray availability check — graceful fallback on Linux without tray daemon
- **New:** Cross-platform font stacks (Segoe UI / SF Pro / Liberation Sans; Consolas / Menlo / DejaVu Sans Mono)

### v2.0 (2026)
- **New:** Last used radio pre-selected in discovery dialog
- **New:** Automatic FlexRadio reconnect with configurable interval
- **New:** Wavelog retry counter with periodic status messages
- **New:** Dedicated "Quit" button in the UI header and tray menu
- **New:** TX power (RF power in watts) read from radio and sent to Wavelog
- **Bugfix:** Bridge no longer sends stale data to Wavelog after SmartSDR is closed

### v1.0 (2025)
- Initial release

---

## About This Project

This project was developed with the assistance of **[Claude](https://claude.ai)**, an AI assistant made by [Anthropic](https://www.anthropic.com). Claude helped design the architecture, implement the FlexLib API communication, write the PyQt6 UI, and iteratively debug and extend the feature set across all versions.

---

## License

This project is licensed under the **GNU General Public License v3.0 (GPL-3.0)**.

You are free to use, modify, and distribute this software under the terms of the GPL v3. Any derivative works must also be released under the same license.

See the [LICENSE](LICENSE) file for the full license text, or visit
https://www.gnu.org/licenses/gpl-3.0.html

---

*73 de DK4DJ*
