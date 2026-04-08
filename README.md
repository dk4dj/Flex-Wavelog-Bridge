# FlexRadio → Wavelog Bridge

Connects a **FlexRadio 6600** (and compatible models) to **Wavelog**, transmitting frequency, mode, and TX power in real time and fully automatically.

Implemented based on the **FlexLib API v4.1.5** (FlexLib_API_v4.1.5.39794).

---

## Features

- **Automatic device discovery** via VITA-49 UDP broadcast (port 4992) with last-used radio pre-selected
- **SmartSDR TCP API** – complete implementation of the command protocol
- **TX slice tracking** – always transmits data from the active transmit slice
- **Manual slice selection** – fixed selection independent of TX status
- **Real-time transmission** to Wavelog `POST /api/radio`, configurable interval (1–120 s)
- **TX power reporting** – reads RF output power from the radio and sends it to Wavelog as `power` (watts)
- **Auto-reconnect for FlexRadio** – automatically reconnects after connection loss, configurable interval
- **Auto-retry for Wavelog** – keeps retrying silently when Wavelog is temporarily unreachable
- **System tray operation** – runs invisibly in the background; double-click reopens the window
- **Quit button** – dedicated button in the UI to fully exit the program (closing the window minimises to tray)
- **Wavelog connection test** – independent of FlexRadio, with detailed diagnostics
- **Persistent configuration** stored in `%APPDATA%\FlexWavelogBridge\config.json`
- **Detailed logging** in `%APPDATA%\FlexWavelogBridge\bridge.log`

---

## Requirements

| Component | Requirement |
|-----------|-------------|
| Operating System | Windows 10/11 (64-bit) |
| Python | 3.11 or newer – from the **Microsoft Store** |
| FlexRadio | 6600 (or any other SmartSDR-compatible device) |
| SmartSDR | Must be running (required for discovery and TCP connection) |
| Wavelog | Any version with an active API key (Read + Write) |

---

## Installation

### Install Python from the Microsoft Store

1. Open **Start → Microsoft Store**
2. Search for **"Python 3.12"** and install it
3. Python is then available as `python` or `python3` in the terminal

### Set up the program

Extract all files into a folder, then double-click `start.bat`.

The script will:

1. Automatically locate the Store Python installation
2. Create a virtual environment in the `venv\` subfolder
3. Install all dependencies (`PyQt6`, `requests`)
4. Launch the program without a console window (`pythonw`)

**Manual alternative:**

```
python -m venv venv
venv\Scripts\pip install -r requirements.txt
venv\Scripts\pythonw flex_wavelog_bridge.py
```

---

## Getting Started

### 1. Configure Wavelog

Open the **Configuration** tab:

- **Wavelog URL** – e.g. `https://log.mycall.com` (no trailing `/`)
- **API Key** – create one in Wavelog under *User Account → API Keys* (Read + Write)
- **Radio Name** – any name; this is how it appears in Wavelog
- **Update Interval** – how often data is sent to Wavelog (default: 5 s)

Click **"Save Settings"**, then **"Test Wavelog Connection"**.

### 2. Connect FlexRadio

Go to the **Control** tab:

1. Click **"Search Device"** – listens for 4 seconds via UDP broadcast
   - The last used radio is automatically pre-selected in the list
2. Select the found device and click **"Connect"**
3. Alternatively: enter the IP address manually (e.g. if UDP port 4992 is already in use)

### 3. Auto-Reconnect

Enable **"Automatically reconnect on connection loss"** in the Control tab.  
Configure the reconnect interval (5–300 s). When SmartSDR is closed or the network drops, the bridge will reconnect automatically as soon as SmartSDR is available again.

### 4. Slice Selection

After a successful connection, all open slices appear in the slice selector:

| Mode | Behaviour |
|------|-----------|
| Auto (TX Slice) | Automatically follows the slice with `tx=1` |
| Manual | Fixed selection of a slice, regardless of TX status |

In Auto mode, the lowest-numbered slice is used as fallback when no TX slice is active.

### 5. TX Power

The bridge reads the `rfpower` value (0–100) from the FlexRadio transmit status and forwards it as `power` (integer, watts) to the Wavelog API. The current value is shown in the orange indicator in the status bar. Power is only sent when the value is greater than 0.

---

## Closing vs. Quitting

| Action | Result |
|--------|--------|
| Click **✕** in the window title bar | Minimises to system tray – bridge keeps running |
| Click **✕ Quit** button in the header | Fully terminates the program |
| Tray menu → **Quit** | Fully terminates the program |

A tray notification reminds you when the window is minimised to the tray.

---

## Autostart

1. Press `Win+R` → type `shell:startup`
2. Place a shortcut to `start.bat` in the startup folder
3. Enable **"Automatically connect on startup"** in the app

---

## Files

```
flex_wavelog_bridge.py   Main program
requirements.txt         Python dependencies (PyQt6, requests)
start.bat                Windows launcher script
README.md                This file
CLAUDE.md                Technical documentation for developers
```

**User data** (created automatically):

```
%APPDATA%\FlexWavelogBridge\
    config.json          Configuration
    bridge.log           Detailed log (DEBUG level)
```

---

## Mode Mapping: FlexRadio → ADIF / Wavelog

| FlexRadio Mode | Wavelog / ADIF |
|----------------|----------------|
| USB, LSB | SSB |
| AM, SAM | AM |
| FM, NFM, DFM | FM |
| CW | CW |
| RTTY | RTTY |
| DIGU, DIGL, FDV | DIGI |

Unknown modes are passed through unchanged.

---

## Troubleshooting

| Problem | Cause | Solution |
|---------|-------|----------|
| Discovery finds no device | SmartSDR not running, UDP/4992 blocked | Start SmartSDR; check firewall; enter IP manually |
| Port not available | Another program is using UDP/4992 | Enter IP manually in the dialog |
| Connection refused | SmartSDR not running / wrong IP | Check SmartSDR status and IP address |
| Timeout | Different subnet / VPN / firewall | Check reachability with `ping` |
| Wavelog 401 | API key invalid or insufficient permissions | Create a new key with Read + Write |
| Wavelog 404 | Wrong URL | Try the URL with `/index.php/` |
| Wavelog 302 | HTTP/HTTPS mismatch | Use `https://` instead of `http://` (or vice versa) |
| No slice visible | No slice opened in SmartSDR | Open at least one slice in SmartSDR |
| Wrong slice | No TX slice active | Activate TX in SmartSDR or use Manual mode |
| Program not visible | `pythonw` starts without a window | Check the system tray (arrow next to the clock) |
| No power value shown | Radio not transmitting or `sub transmit` not supported | Value only appears during active TX |

---

## Changelog

### v2.0 (2025)
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

This project was developed with the assistance of **[Claude](https://claude.ai)**, an AI assistant made by [Anthropic](https://www.anthropic.com). Claude helped design the architecture, implement the FlexLib API communication, write the PyQt6 UI, and iteratively debug and extend the feature set.

---

## License

This project is licensed under the **GNU General Public License v3.0 (GPL-3.0)**.

You are free to use, modify, and distribute this software under the terms of the GPL v3. Any derivative works must also be released under the same license.

See the [LICENSE](LICENSE) file for the full license text, or visit  
https://www.gnu.org/licenses/gpl-3.0.html

---

*73 de DK4DJ*
