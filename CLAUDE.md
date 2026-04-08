# CLAUDE.md — Technical Documentation

**FlexRadio → Wavelog Bridge**
Developer documentation for Claude and future maintainers.

---

## Project Overview

A cross-platform desktop application (Python / PyQt6) that:

1. Connects to a FlexRadio transceiver via the SmartSDR TCP command API (port 4992)
2. Receives frequency, operating mode, and TX power from the active TX slice
3. Periodically forwards this data to the Wavelog REST API
4. Runs as a system tray application in the background
5. Works with **SmartSDR** (Windows) and **AetherSDR** (Linux / macOS / Windows) as the SDR client

---

## Architecture

### Thread Model

```
GUI Thread (Qt main)
│
├── FlexRadioClient (QThread)
│     TCP connection to SmartSDR / AetherSDR on port 4992
│     Signals (all str to avoid cross-thread dict serialisation issues):
│       status_changed(str)    "connected" | "disconnected"
│       radio_data(str)        JSON payload: frequency, mode, power_w, slice, tx
│       log_message(str)       Plain text for GUI log
│       slices_changed(str)    JSON snapshot of all known slices
│
├── UpdateWorker (QThread)
│     Periodically sends radio_data to Wavelog API
│     Signals:
│       log_message(str)
│       wavelog_status(bool, str)
│
├── FlexReconnectManager (QThread)
│     Monitors connection state; emits request_connect(str, int) after timeout
│     Signals:
│       request_connect(str, int)   host, port
│       log_message(str)
│
└── WavelogTester (QThread)
      One-shot connection test; result via signal
      Signals:
        result_ready(bool, str, list)
```

### Why JSON strings instead of `pyqtSignal(dict)`?

PyQt6 cannot reliably serialise nested dicts for cross-thread signal delivery — it silently swallows the resulting exceptions, breaking the entire signal delivery chain. All complex data is therefore sent as `json.dumps(...)` and parsed with `json.loads(...)` in the receiving slot.

### Why `sys.stdout is not None` guard?

Under `pythonw.exe` (the windowless launcher on Windows), `sys.stdout = None`. A `StreamHandler` on `None` raises `AttributeError` on the first log call. Because this happens inside a Qt signal slot, Qt aborts the entire delivery chain without any visible error message. The handler is only created when `sys.stdout is not None`.

### Atomic socket swap in `stop()` and `finally`

```python
sock, self._sock = self._sock, None   # atomic ownership transfer
```

`stop()` is called from the GUI thread; `run()` / `finally` run on the worker thread. Without the atomic swap `finally` would close the socket a second time, producing `WSAENOTSOCK` / `EBADF`. With the swap exactly one party owns the socket at any time.

---

## SmartSDR / AetherSDR Protocol

Both clients implement the same FlexLib SmartSDR protocol. The differences that affect the bridge are documented below.

### Message Types

| Prefix | Direction | Meaning |
|--------|-----------|---------|
| `V`    | Radio → Client | Firmware version string |
| `H`    | Radio → Client | Hex client handle (`H<handle>\|...`) |
| `C`    | Client → Radio | Command: `C<seq>\|<command>\n` |
| `R`    | Radio → Client | Response: `R<seq>\|<hex_code>\|<msg>` (ignored by bridge) |
| `S`    | Radio → Client | Status update: `S<handle>\|<topic> <k>=<v> ...` |
| `M`    | Radio → Client | Informational message (logged) |

### Connection / Init Sequence

Sent immediately after TCP connect, in this order:

```
keepalive enable                  ← prevents radio-side timeout (required by AetherSDR)
client program FlexWavelogBridge
client gui                        ← registers as GUI client (required for multi-client)
client station FlexWavelogBridge  ← identifies station to other connected clients
client start_persistence off
sub client all                    ← receive client connect/disconnect events
sub tx all
sub atu all
sub slice all
sub gps all
sub transmit all                  ← receive rfpower= updates
```

After `sub client all` the radio sends `H<handle>|...` which contains our client handle. This handle is stored in `_own_handle` and used for multi-client filtering.

### Keepalive

The bridge sends `keepalive enable` on connect. AetherSDR additionally sends `ping ms_timestamp=<t>` every second; the bridge uses `ping` on `socket.timeout` which is sufficient for SmartSDR. `keepalive enable` alone prevents the radio from dropping the connection.

### Slice Status Format

```
S<handle>|slice <idx> <k>=<v> <k>=<v> ...
```

Relevant keys parsed by the bridge:

| Key | Type | Meaning |
|-----|------|---------|
| `rf_frequency` | double | Frequency in MHz, e.g. `14.225000` |
| `mode` | string | Demodulation mode (case-insensitive, normalised to upper) |
| `tx` | `0` or `1` | This slice is the active TX slice |
| `in_use` | `0` or `1` | Slice exists (`0` = removed) |
| `client_handle` | hex string | Which connected client owns this slice |

### Transmit Status Format

```
S<handle>|transmit rfpower=<0–100> tune_power=<0–100> mox=<0|1> ...
```

`rfpower` is a percentage of the radio's maximum output power. The bridge forwards it as an integer watt value to Wavelog (1:1 mapping is exact for a 100 W radio).

### Mode Mapping

| FlexRadio / AetherSDR Mode | Wavelog / ADIF |
|---------------------------|----------------|
| USB, LSB | SSB |
| AM, SAM | AM |
| FM, NFM, DFM | FM |
| CW | CW |
| RTTY | RTTY |
| DIGU, DIGL | DIGI |
| FDV (SmartSDR) | DIGI |
| FDVU, FDVM (AetherSDR) | DIGI |

Unknown modes are passed through unchanged.

---

## Multi-Client Filtering (AetherSDR / Maestro compatibility)

When multiple clients are connected simultaneously (e.g. AetherSDR + bridge, or SmartSDR + Maestro + bridge), each client gets its own `client_handle` from the radio. Slice status updates include `client_handle=` in certain messages to identify ownership.

### Implementation (`FlexRadioClient`)

Three new fields control multi-client behaviour:

```python
self._own_handle: str = ""             # our handle, set from the H message
self._owned_slice_ids: set[str] = set() # slice IDs confirmed as ours
self.filter_by_handle: bool = True      # can be disabled via the GUI checkbox
```

### Filtering Logic in `_parse_status`

For each incoming `slice <idx>` update:

1. Extract `client_handle=` from the update string if present
2. If it matches `_own_handle` → add `idx` to `_owned_slice_ids`, process normally
3. If it does not match → remove the slice if we have it, discard the update
4. If no `client_handle` present and `idx` is already in `_owned_slice_ids` → process normally (subsequent updates for owned slices rarely repeat the handle)
5. If no `client_handle` present and `idx` is unknown but we already have confirmed owned slices → defer (wait for a message with handle confirmation)

This matches the approach used in AetherSDR (`m_ownedSliceIds`).

**Important timing note:** The radio sends early slice updates *without* `client_handle`. The bridge creates no slice model for those; it waits until a handle-confirmed update arrives. This is why `filter_by_handle` can be disabled for single-client SmartSDR setups where `client_handle` may never appear.

### Config Key

`client_handle_filter` (bool, default `True`) — stored in `config.json`, exposed as a checkbox in the Configuration tab.

---

## Class Reference

### `FlexDiscovery`

Listens for VITA-49 binary UDP broadcast packets from the radio on port 4992.

**Packet format** (FlexLib: `VitaDiscovery.cs`, `VitaFlex.cs`):

```
Offset  Bytes  Content
  0       4    VITA header (big-endian uint32)
                 bits 31–28: pkt_type  must be 0x7 (ExtDataWithStream)
                 bit  27:    C-flag    must be 1 (Class-ID present)
                 bit  26:    T-flag    (Trailer present)
                 bits 25–24: tsi       (Timestamp Integer type)
                 bits 23–22: tsf       (Timestamp Fractional type)
                 bits 15–0:  packet_size (in 32-bit words)
  4       4    Stream-ID (uint32)
  8       4    OUI word  (bits 23–0 = OUI, must be 0x001C2D)
 12       4    ClassCode word (bits 15–0 = PCC, must be 0xFFFF)
 16+      N    UTF-8 payload: space-separated key=value pairs
 end-4    4    Trailer (optional, when T-flag=1)
```

Relevant payload keys: `ip`, `model`, `nickname`, `version`, `status`, `serial`, `callsign`, `port`, `available_slices`, `max_slices`

**Socket options:** `SO_REUSEPORT` is preferred on macOS / Linux; falls back to `SO_REUSEADDR` on Windows (`AttributeError` on missing `SO_REUSEPORT`).

---

### `FlexRadioClient(QThread)`

TCP connection to the SmartSDR command API.

**TX-slice logic:**

- `tx=1` on any slice sets all other slices to `tx=False`
- `_active_slice_idx()` returns:
  - `selected_slice` if manually set and present in `_slices`
  - The slice with `tx=True` in Auto mode
  - The numerically lowest slice as fallback

**Signals:**

| Signal | Type | Content |
|--------|------|---------|
| `status_changed` | `str` | `"connected"` / `"disconnected"` |
| `radio_data` | `str` | JSON: `{frequency, mode, raw_mode, freq_mhz, slice, tx, power_w}` |
| `log_message` | `str` | Plain text for GUI log |
| `slices_changed` | `str` | JSON: `{idx: {rf_frequency, mode, tx}, ...}` |

**Error handling:**

| Error | Handling |
|-------|----------|
| `ConnectionRefusedError` | User-friendly message, no stack trace |
| `socket.timeout` (on connect) | User-friendly message |
| `OSError` with errno in `_CLOSED_ERRNOS` or `_run=False` | Expected shutdown, silently ignored |
| Other `OSError` | Logged as a real error |

**Portable error codes (`_CLOSED_ERRNOS`):**

Uses `errno.EBADF`, `errno.ECONNRESET`, `errno.ECONNABORTED`, `errno.ENOTSOCK` from the stdlib `errno` module — platform-neutral values. Windows-specific `winerror` codes (10038, 10053, 10054) are checked additionally via `getattr(e, "winerror", None)`, only populated on Windows.

---

### `WavelogClient`

REST client for the Wavelog API.

**`POST /api/radio` payload:**

```json
{
  "key":       "API_KEY",
  "radio":     "FlexRadio 6600",
  "frequency": 14225000,
  "mode":      "SSB",
  "power":     100,
  "timestamp": "2026/04/09 14:30"
}
```

`power` is only included when `power_w > 0`.

**`POST /api/version` payload** (connection test):

```json
{ "key": "API_KEY" }
```

---

### `UpdateWorker(QThread)`

Periodic Wavelog sender.

1. Sleeps `update_interval` seconds
2. Checks `_current_data is not None`
3. Checks URL and API key are configured
4. Calls `WavelogClient.send_radio_data(frequency, mode, power_w)`
5. Counts consecutive Wavelog failures; logs a warning after the 1st failure and every 10th thereafter

**Thread-safe data update:** `update_radio_data(data: dict)` and `clear_radio_data()` use `threading.Lock`.

**`clear_radio_data()`** is called by `MainWindow._on_flex_status("disconnected")` to stop sending stale data after the SDR client closes.

---

### `FlexReconnectManager(QThread)`

Polls `_connected` every second. When disconnected and `flex_reconnect=True`, waits `flex_reconnect_sec` seconds then emits `request_connect(host, port)`. Respects `set_connected(bool)` called from `MainWindow._on_flex_status`.

---

### `MainWindow(QMainWindow)`

**Control tab:**
- FlexRadio connection group (discovery, connect/disconnect, auto-connect, auto-reconnect)
- Slice selection (Auto TX / Manual, live-updating combo)
- Protocol log (scrolling, timestamped)

**Configuration tab:**
- Wavelog URL, API key (with visibility toggle), radio name, update interval
- Multi-client filtering checkbox (`client_handle_filter`)
- Wavelog connection test button

**Key slots:**

| Slot | Signal source | Function |
|------|---------------|----------|
| `_on_flex_status(str)` | `FlexRadioClient.status_changed` | Badge, tray icon, button style, clear data on disconnect |
| `_on_radio_data(str)` | `FlexRadioClient.radio_data` | Update freq/mode/power display, feed UpdateWorker |
| `_on_slices_changed(str)` | `FlexRadioClient.slices_changed` | Rebuild slice combo |
| `_on_wl_status(bool, str)` | `UpdateWorker.wavelog_status` | Wavelog badge colour / text |
| `_on_reconnect_request(str, int)` | `FlexReconnectManager.request_connect` | Trigger `_connect_to()` |

**Tray availability (PLAT 4):** `QSystemTrayIcon.isSystemTrayAvailable()` is checked at startup. When `False` (Linux without notification daemon), `_tray` and `_tray_status` are set to `None`, all accesses are guarded, and `closeEvent` exits normally instead of hiding to tray.

---

## Configuration File

Stored as `config.json` **in the same directory as `flex_wavelog_bridge.py`** (cross-platform, no OS-specific paths):

```json
{
  "wavelog_url":          "https://log.example.com",
  "wavelog_api_key":      "abc123...",
  "radio_name":           "FlexRadio 6600",
  "update_interval":      5,
  "flex_host":            "192.168.1.100",
  "flex_port":            4992,
  "auto_connect":         false,
  "flex_reconnect":       true,
  "flex_reconnect_sec":   15,
  "last_flex_ip":         "192.168.1.100",
  "client_handle_filter": true
}
```

`config.json` and `bridge.log` are always resolved relative to `Path(__file__).resolve().parent` — they appear next to the script regardless of the working directory or OS.

---

## Platform Notes

| Concern | Solution |
|---------|----------|
| Config / log path | `Path(__file__).resolve().parent` — no `APPDATA`, no `~/.config` |
| Socket error codes | `errno.EBADF/ECONNRESET/ECONNABORTED/ENOTSOCK` + optional `winerror` guard |
| UDP socket options | `SO_REUSEPORT` preferred; `AttributeError` fallback to `SO_REUSEADDR` |
| System tray | `isSystemTrayAvailable()` check; graceful fallback when absent |
| UI fonts | `'Segoe UI','SF Pro Text','Helvetica Neue','Liberation Sans',sans-serif` |
| Monospace font | `'Consolas','Menlo','DejaVu Sans Mono','Courier New',monospace` |
| No-window launch | `pythonw.exe` (Windows); `sys.stdout is None` guard for `StreamHandler` |

---

## Developer How-Tos

### Add a new mode mapping

Edit `FlexRadioClient.MODE_MAP`:

```python
MODE_MAP = {
    ...
    "NEW_MODE": "ADIF_EQUIVALENT",
}
```

### Parse an additional slice key

In `FlexRadioClient._update_slice()`, add a branch inside the `for kv in update.split()` loop:

```python
elif k == "new_key":
    if s.get("new_key") != val:
        s["new_key"] = val
        changed = True
```

Then include the field in `_emit_active_slice_data()` JSON payload and `UpdateWorker.run()` / `WavelogClient.send_radio_data()`.

### Send an additional Wavelog field

In `WavelogClient.send_radio_data()`, extend the `payload` dict:

```python
payload = {
    ...
    "new_field": value,
}
```

### Adjust logging level

Line ~46 in the file:

```python
logging.basicConfig(level=logging.DEBUG, ...)  # DEBUG | INFO | WARNING
```

### Add a new subscription

Append to the init command list in `FlexRadioClient.run()`:

```python
for cmd in (...,
            "sub new_topic all"):
    self._cmd(cmd)
```

Then add a new `elif topic == "new_topic":` branch in `_parse_status`.

---

## Dependencies

| Package | Version | Purpose |
|---------|---------|---------|
| `PyQt6` | ≥ 6.4.0 | GUI, threading, signals |
| `requests` | ≥ 2.28.0 | HTTP client for Wavelog API |

Standard library (no installation needed): `socket`, `struct`, `threading`, `json`, `logging`, `datetime`, `time`, `pathlib`, `os`, `sys`, `errno`

---

## Changelog

### v3.0 (2026) — AetherSDR & Cross-Platform

- `keepalive enable` added to init sequence (AetherSDR compatibility, harmless on SmartSDR)
- `client gui` + `client station` registration for proper multi-client announcement
- `client_handle` filtering in `_parse_status` — tracks only owned slices when multiple SDR clients are connected; controlled by `client_handle_filter` config key and GUI checkbox
- `_own_handle`, `_owned_slice_ids`, `filter_by_handle` fields added to `FlexRadioClient`
- `FDVU` / `FDVM` added to `MODE_MAP` (AetherSDR FreeDV modes → DIGI)
- Cross-platform path handling: `Path(__file__).resolve().parent` replaces `APPDATA`
- Portable socket error codes via `errno` module + optional `winerror` guard
- `SO_REUSEPORT` / `SO_REUSEADDR` fallback for UDP socket
- `QSystemTrayIcon.isSystemTrayAvailable()` check with graceful fallback
- Cross-platform font stacks for UI and monospace log window
- Window title updated to show "SmartSDR & AetherSDR"

### v2.0 (2026)

- Bugfix: `UpdateWorker.clear_radio_data()` called on disconnect — stops stale Wavelog sends
- Last-used radio IP stored in config; pre-selected in Discovery dialog
- `FlexReconnectManager` thread — automatic reconnect with configurable interval
- Wavelog failure counter — periodic warnings instead of per-attempt spam
- Quit button in header and tray menu — fully terminates the process
- TX power: `rfpower` from `sub transmit all` forwarded to Wavelog as `power`

### v1.0 (2025)

- Initial release: VITA-49 discovery, SmartSDR TCP API, slice tracking, Wavelog API, system tray, PyQt6 GUI
