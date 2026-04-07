"""
FlexRadio 6600 → Wavelog Bridge – FlexLib API v4.1.5

Bugfix: UpdateWorker sendet keine Daten mehr an Wavelog, nachdem SmartSDR
        beendet wurde. Bei Verbindungstrennung wird _current_data geleert.
"""

import sys, os, json, socket, struct, threading, time, logging, datetime, requests

from pathlib import Path
from PyQt6.QtWidgets import (
    QApplication, QDialog, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QLineEdit, QPushButton, QComboBox, QGroupBox, QSpinBox,
    QTextEdit, QSystemTrayIcon, QMenu, QMessageBox, QTabWidget,
    QFormLayout, QCheckBox, QFrame,
)
from PyQt6.QtCore import Qt, QThread, pyqtSignal, QTimer, pyqtSlot
from PyQt6.QtGui import QIcon, QPixmap, QColor, QPainter, QPen, QAction

# ─── Logging ──────────────────────────────────────────────────────────────────
# IMPORTANT: pythonw.exe sets sys.stdout = None → never pass sys.stdout to a
# StreamHandler or any log.xxx call that might reach it from a signal slot.

LOG_DIR = Path(os.environ.get("APPDATA", ".")) / "FlexWavelogBridge"
LOG_DIR.mkdir(parents=True, exist_ok=True)
LOG_FILE = LOG_DIR / "bridge.log"

# File-only logger – safe under pythonw.exe (no stdout)
_log_handlers: list = [logging.FileHandler(LOG_FILE, encoding="utf-8")]
if sys.stdout is not None:   # console only when stdout actually exists
    _log_handlers.append(logging.StreamHandler(sys.stdout))

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)-7s] %(message)s",
    handlers=_log_handlers,
)
log = logging.getLogger("FlexWavelog")
log.info(f"=== FlexRadio→Wavelog Bridge start (log: {LOG_FILE}) ===")

CONFIG_FILE = LOG_DIR / "config.json"

# ─── Config ───────────────────────────────────────────────────────────────────

DEFAULT_CONFIG = {
    "wavelog_url":      "",
    "wavelog_api_key":  "",
    "radio_name":       "FlexRadio 6600",
    "update_interval":  5,
    "flex_host":        "",
    "flex_port":        4992,
    "auto_connect":     False,
}

def load_config() -> dict:
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            log.debug(f"Config geladen: {cfg}")
            return {**DEFAULT_CONFIG, **cfg}
        except Exception as e:
            log.warning(f"Config laden fehlgeschlagen: {e}")
    return dict(DEFAULT_CONFIG)

def save_config(cfg: dict) -> None:
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
        log.debug("Config gespeichert")
    except Exception as e:
        log.error(f"Config speichern fehlgeschlagen: {e}")

# ─── VITA-49 Discovery ────────────────────────────────────────────────────────
# VitaDiscovery.cs + VitaFlex.cs (FlexLib v4.1.5)
# UDP port 4992, binary VITA-49 packets.
# OUI=0x001C2D, PacketClassCode=0xFFFF

FLEX_OUI                   = 0x001C2D
VITA_EXT_DATA_WITH_STREAM  = 0x7
SL_VITA_DISCOVERY_CLASS    = 0xFFFF
DISCOVERY_PORT             = 4992

def _parse_vita_discovery(data: bytes, src_ip: str) -> dict | None:
    if len(data) < 16:
        return None
    w0       = struct.unpack_from(">I", data, 0)[0]
    pkt_type = (w0 >> 28) & 0xF
    has_cls  = bool((w0 >> 27) & 1)
    has_trl  = bool((w0 >> 26) & 1)
    tsi      = (w0 >> 22) & 0x3
    tsf      = (w0 >> 20) & 0x3
    pkt_size = w0 & 0xFFFF

    if pkt_type != VITA_EXT_DATA_WITH_STREAM or not has_cls:
        return None

    idx  = 8   # skip stream_id (4) already past header (4)
    oui  = struct.unpack_from(">I", data, idx)[0] & 0x00FFFFFF;  idx += 4
    pcc  = struct.unpack_from(">I", data, idx)[0] & 0xFFFF;      idx += 4

    if oui != FLEX_OUI or pcc != SL_VITA_DISCOVERY_CLASS:
        return None

    if tsi: idx += 4
    if tsf: idx += 8

    total_bytes  = pkt_size * 4
    payload_end  = total_bytes - (4 if has_trl else 0)
    payload_len  = payload_end - idx

    if payload_len <= 0 or idx + payload_len > len(data):
        return None

    payload = data[idx:idx + payload_len].decode("utf-8", errors="replace").strip("\x00 ")
    result  = {"ip": src_ip}
    for kv in payload.split():
        k, _, v = kv.partition("=")
        if k and v:
            result[k.lower()] = v
    result.setdefault("nickname", result.get("model", "FlexRadio"))
    log.debug(f"VITA discovery from {src_ip}: {result.get('nickname')} model={result.get('model')}")
    return result


class FlexDiscovery:
    @staticmethod
    def discover(timeout: float = 4.0) -> list[dict]:
        found: dict[str, dict] = {}
        done  = threading.Event()

        def _listen() -> None:
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                sock.settimeout(0.5)
                try:
                    sock.bind(("", DISCOVERY_PORT))
                    log.info(f"Discovery: lauscht auf UDP :{DISCOVERY_PORT}")
                except OSError as e:
                    log.warning(f"Discovery: UDP-Port {DISCOVERY_PORT} nicht verfügbar ({e})")
                    return
                deadline = time.monotonic() + timeout
                while not done.is_set() and time.monotonic() < deadline:
                    try:
                        data, addr = sock.recvfrom(4096)
                        info = _parse_vita_discovery(data, addr[0])
                        if info:
                            key        = info.get("ip", addr[0])
                            found[key] = info
                    except socket.timeout:
                        continue
                    except Exception as e:
                        log.debug(f"Discovery recv: {e}")
                sock.close()
            except Exception as e:
                log.warning(f"Discovery listener: {e}")

        t = threading.Thread(target=_listen, daemon=True)
        t.start()
        t.join(timeout + 1.0)
        done.set()
        log.info(f"Discovery abgeschlossen: {len(found)} Gerät(e)")
        return list(found.values())


# ─── FlexRadio TCP Client ─────────────────────────────────────────────────────
# TcpCommandCommunication.cs + Radio.cs + Slice.cs (FlexLib v4.1.5)
# Port 4992, line-delimited \n.
# Slice status keys: rf_frequency (MHz double), mode (str), tx (0|1)

class FlexRadioClient(QThread):
    # All signals use simple, PyQt6-safe types (str / list, not nested dict)
    status_changed = pyqtSignal(str)   # "connected" | "disconnected" | "error:..."
    radio_data     = pyqtSignal(str)   # JSON string – avoids pyqtSignal(dict) issues
    log_message    = pyqtSignal(str)   # plain text for GUI log
    slices_changed = pyqtSignal(str)   # JSON string of slice snapshot

    MODE_MAP = {
        "USB":"SSB", "LSB":"SSB",
        "AM":"AM",   "SAM":"AM",
        "FM":"FM",   "NFM":"FM",  "DFM":"FM",
        "CW":"CW",   "RTTY":"RTTY",
        "DIGU":"DIGI","DIGL":"DIGI","FDV":"DIGI",
    }

    def __init__(self, host: str, port: int = 4992):
        super().__init__()
        self.host            = host
        self.port            = port
        self._run            = False
        self._sock           = None
        self._seq            = 1
        self._slices: dict[str, dict] = {}   # idx → {rf_frequency, mode, tx}
        self._handle         = ""
        # None = auto (TX slice), str = manual pin
        self.selected_slice: str | None = None

    # ── Main loop ─────────────────────────────────────────────────────────────
    # Windows socket error codes that mean "socket was closed intentionally"
    _CLOSED_ERRNOS = {
        10038,   # WSAENOTSOCK – socket closed externally via stop()
        10054,   # WSAECONNRESET
        10053,   # WSAECONNABORTED
        9,       # EBADF (Linux equivalent)
    }

    def run(self) -> None:
        self._run = True
        log.info(f"FlexRadioClient thread start: {self.host}:{self.port}")
        connect_error: str | None = None   # set only for real, unexpected errors

        try:
            self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self._sock.settimeout(10)
            log.info(f"TCP connect → {self.host}:{self.port}")
            self._sock.connect((self.host, self.port))
            self._sock.settimeout(1.5)
            self.status_changed.emit("connected")
            self.log_message.emit(f"✓ TCP verbunden: {self.host}:{self.port}")
            log.info(f"TCP connected to {self.host}:{self.port}")

            # Init sequence (Radio.cs ~L1914-1965)
            for cmd in ("client program FlexWavelogBridge",
                        "client start_persistence off",
                        "sub client all",
                        "sub tx all",
                        "sub atu all",
                        "sub slice all",
                        "sub gps all"):
                self._cmd(cmd)

            buf = ""
            while self._run:
                try:
                    chunk = self._sock.recv(4096).decode("utf-8", errors="replace")
                    if not chunk:
                        log.info("FlexRadio: Verbindung durch Gegenseite geschlossen")
                        break
                    buf += chunk
                    while "\n" in buf:
                        line, buf = buf.split("\n", 1)
                        line = line.strip("\r\0")
                        if line:
                            log.debug(f"RX: {line[:140]}")
                            self._parse_line(line)
                except socket.timeout:
                    self._cmd("ping")
                except OSError as e:
                    winerr = getattr(e, "winerror", None) or e.errno
                    if winerr in self._CLOSED_ERRNOS or not self._run:
                        # Normal shutdown – socket was closed by stop()
                        log.info(f"FlexRadio: Socket geschlossen (erwartet, winerr={winerr})")
                    else:
                        log.warning(f"FlexRadio recv OSError: {e}")
                        connect_error = str(e)
                    break

        except ConnectionRefusedError:
            connect_error = f"Verbindung abgelehnt – kein FlexRadio unter {self.host}:{self.port}"
            log.warning(f"FlexRadio: {connect_error}")
        except socket.timeout:
            connect_error = f"Timeout – {self.host}:{self.port} nicht erreichbar (10 s)"
            log.warning(f"FlexRadio: {connect_error}")
        except OSError as e:
            winerr = getattr(e, "winerror", None) or e.errno
            if winerr in self._CLOSED_ERRNOS or not self._run:
                # stop() was called before/during connect – completely normal
                log.info(f"FlexRadio: Verbindungsaufbau durch stop() abgebrochen")
            else:
                connect_error = str(e)
                log.error(f"FlexRadio OSError beim Verbinden: {e}", exc_info=True)
        except Exception as e:
            connect_error = str(e)
            log.error(f"FlexRadio unerwarteter Fehler: {e}", exc_info=True)

        finally:
            # Close socket safely – may already be closed by stop()
            sock, self._sock = self._sock, None
            if sock is not None:
                try:
                    sock.close()
                except Exception:
                    pass

        log.info("FlexRadioClient thread ending")
        # Only show an error message if it was a real problem
        if connect_error:
            self.log_message.emit(f"✗ {connect_error}")
        self.status_changed.emit("disconnected")
        self.log_message.emit("FlexRadio Verbindung getrennt")

    def _cmd(self, command: str) -> None:
        if not self._sock:
            return
        try:
            msg = f"C{self._seq}|{command}\n"
            self._sock.sendall(msg.encode("utf-8"))
            self._seq += 1
            log.debug(f"TX: {command}")
        except Exception as e:
            log.debug(f"_cmd error: {e}")

    # ── Protocol parsing ──────────────────────────────────────────────────────

    def _parse_line(self, line: str) -> None:
        if not line:
            return
        ch = line[0]
        if ch == 'H':
            # H<handle>|...
            self._handle = line[1:].split("|")[0]
            msg = f"Client-Handle: 0x{self._handle}"
            log.info(f"FlexRadio: {msg}")
            self.log_message.emit(msg)
        elif ch == 'V':
            ver = line[1:]
            log.info(f"FlexRadio Protokollversion: {ver}")
            self.log_message.emit(f"Protokoll-Version: {ver}")
        elif ch == 'S':
            self._parse_status(line)
        elif ch == 'M':
            parts = line.split("|", 1)
            if len(parts) == 2:
                log.info(f"FlexRadio Meldung: {parts[1]}")

    def _parse_status(self, line: str) -> None:
        # S<handle>|<topic> [<idx>] <k>=<v> ...
        parts = line.split("|", 1)
        if len(parts) < 2:
            return
        body  = parts[1]
        words = body.split()
        if not words:
            return
        topic = words[0].lower()
        if topic == "slice":
            if len(words) < 3:
                return
            idx = words[1]
            if "in_use=0" in body:
                if idx in self._slices:
                    del self._slices[idx]
                    log.info(f"Slice {idx} entfernt")
                    self._emit_slices()
                return
            update_str = body[len("slice ") + len(idx) + 1:]
            self._update_slice(idx, update_str)

    def _update_slice(self, idx: str, update: str) -> None:
        is_new = idx not in self._slices
        if is_new:
            self._slices[idx] = {"tx": False}
            log.info(f"Slice {idx} angelegt")

        s      = self._slices[idx]
        old_tx = s.get("tx", False)
        changed = is_new

        for kv in update.split():
            if "=" not in kv:
                continue
            key, _, val = kv.partition("=")
            k = key.lower()
            if k == "rf_frequency":
                try:
                    new_freq = float(val)
                    if s.get("rf_frequency") != new_freq:
                        s["rf_frequency"] = new_freq
                        changed = True
                except ValueError:
                    log.warning(f"Slice {idx}: ungültige rf_frequency '{val}'")
            elif k == "mode":
                new_mode = val.upper()
                if s.get("mode") != new_mode:
                    s["mode"] = new_mode
                    changed = True
            elif k == "tx":
                new_tx = (val == "1")
                if new_tx != old_tx:
                    s["tx"] = new_tx
                    changed = True
                    log.info(f"Slice {idx}: tx={'JA' if new_tx else 'NEIN'}")
                    if new_tx:
                        for other_idx, other_s in self._slices.items():
                            if other_idx != idx and other_s.get("tx"):
                                other_s["tx"] = False
                                log.debug(f"Slice {other_idx}: tx zurückgesetzt")

        if changed:
            self._emit_slices()

        # Only emit radio_data for the currently active slice
        active = self._active_slice_idx()
        if active != idx:
            return

        freq_mhz = s.get("rf_frequency")
        mode_raw = s.get("mode", "")
        if freq_mhz and mode_raw:
            freq_hz      = int(freq_mhz * 1_000_000)
            wavelog_mode = self.MODE_MAP.get(mode_raw, mode_raw)
            mode_label   = "TX-AUTO" if self.selected_slice is None else "MANUELL"
            log.info(f"Slice {idx} [{mode_label}]: {freq_mhz:.3f} MHz "
                     f"mode={mode_raw}→{wavelog_mode} tx={s.get('tx')}")
            # Emit as JSON string to avoid pyqtSignal(dict) issues
            payload = json.dumps({
                "frequency": freq_hz,
                "mode":      wavelog_mode,
                "raw_mode":  mode_raw,
                "freq_mhz":  freq_mhz,
                "slice":     idx,
                "tx":        s.get("tx", False),
            })
            self.radio_data.emit(payload)

    def _active_slice_idx(self) -> str | None:
        """Which slice index should currently be reported to Wavelog?"""
        if self.selected_slice is not None:
            return self.selected_slice if self.selected_slice in self._slices else None
        # Auto: TX slice
        for idx, s in self._slices.items():
            if s.get("tx"):
                return idx
        # Fallback: numerically lowest
        if self._slices:
            return min(self._slices.keys(), key=lambda x: int(x) if x.isdigit() else 99)
        return None

    def _emit_slices(self) -> None:
        """Emit slice snapshot as JSON string (safe for cross-thread signals)."""
        try:
            self.slices_changed.emit(json.dumps(self._slices))
        except Exception as e:
            log.debug(f"slices_changed emit error: {e}")

    def stop(self) -> None:
        """Signal the run loop to exit and close the socket.

        Closing the socket from the outside thread unblocks recv() immediately.
        The resulting OSError (WinError 10038 / EBADF) is caught and ignored
        inside run() because _run will be False at that point.
        """
        self._run = False
        sock, self._sock = self._sock, None   # take ownership atomically
        if sock is not None:
            try:
                sock.close()
            except Exception:
                pass


# ─── Wavelog API Client ────────────────────────────────────────────────────────

class WavelogClient:
    def __init__(self, base_url: str, api_key: str, radio_name: str):
        self.base_url   = base_url.rstrip("/")
        self.api_key    = api_key
        self.radio_name = radio_name
        self._session   = requests.Session()
        self._session.headers.update({
            "Content-Type": "application/json",
            "Accept":       "application/json",
        })

    def send_radio_data(self, frequency: int, mode: str) -> tuple[bool, str]:
        url     = f"{self.base_url}/api/radio"
        payload = {
            "key":       self.api_key,
            "radio":     self.radio_name,
            "frequency": frequency,
            "mode":      mode,
            "timestamp": datetime.datetime.utcnow().strftime("%Y/%m/%d %H:%M"),
        }
        log.debug(f"Wavelog POST {url} freq={frequency} mode={mode}")
        try:
            t0   = time.monotonic()
            resp = self._session.post(url, json=payload, timeout=10)
            ms   = int((time.monotonic() - t0) * 1000)
            log.debug(f"Wavelog response: {resp.status_code} ({ms}ms) {resp.text[:100]}")
            if resp.status_code == 200:
                return True, f"OK ({ms} ms)"
            return False, f"HTTP {resp.status_code}: {resp.text[:200]}"
        except requests.exceptions.SSLError as e:
            return False, f"SSL-Fehler: {e}"
        except requests.exceptions.ConnectionError:
            return False, "Verbindung fehlgeschlagen"
        except requests.exceptions.Timeout:
            return False, "Request-Timeout"
        except Exception as e:
            return False, str(e)

    def test_connection(self) -> tuple[bool, str, list]:
        url    = f"{self.base_url}/api/version"
        detail = [
            f"  URL:          {url}",
            f"  API-Schlüssel: {self.api_key[:6]}{'*' * max(0, len(self.api_key) - 6)}",
        ]
        log.info(f"Wavelog Verbindungstest: {url}")
        try:
            t0   = time.monotonic()
            resp = self._session.post(url, json={"key": self.api_key}, timeout=8)
            ms   = int((time.monotonic() - t0) * 1000)
            detail += [f"  HTTP-Status: {resp.status_code} ({ms} ms)",
                       f"  Antwort:     {resp.text[:300]}"]
            log.info(f"Wavelog test: {resp.status_code} ({ms}ms)")
            if resp.status_code == 200:
                try:
                    ver = resp.json().get("version", "?")
                    detail.append(f"  Wavelog-Version: {ver}")
                    return True, f"Wavelog v{ver} – Verbindung OK", detail
                except Exception:
                    return True, "Verbindung OK (kein JSON)", detail
            elif resp.status_code == 401:
                detail.append("  → API-Schlüssel ungültig oder abgelaufen")
                return False, "Ungültiger API-Schlüssel (401)", detail
            elif resp.status_code == 404:
                detail.append("  → Endpunkt nicht gefunden – URL korrekt?")
                return False, "Endpunkt nicht gefunden (404)", detail
            elif resp.status_code == 302:
                loc = resp.headers.get("Location", "")
                detail.append(f"  → Weiterleitung nach: {loc}")
                detail.append("  Tipp: http:// statt https:// oder umgekehrt?")
                return False, "Weiterleitung (302)", detail
            else:
                return False, f"HTTP {resp.status_code}", detail
        except requests.exceptions.SSLError as e:
            detail += [f"  SSL-Fehler: {e}",
                       "  Tipp: Zertifikat ungültig? http:// versuchen."]
            return False, "SSL-Fehler", detail
        except requests.exceptions.ConnectionError as e:
            detail += [f"  Verbindungsfehler: {e}",
                       "  Tipp: URL erreichbar? Firewall? VPN?"]
            return False, "Verbindung fehlgeschlagen", detail
        except requests.exceptions.Timeout:
            detail += ["  Timeout nach 8 Sekunden",
                       "  Tipp: Server erreichbar? Port offen?"]
            return False, "Timeout", detail
        except Exception as e:
            detail.append(f"  Fehler: {type(e).__name__}: {e}")
            return False, str(e), detail


# ─── Wavelog Connection Tester (QThread) ──────────────────────────────────────

class WavelogTester(QThread):
    result_ready = pyqtSignal(bool, str, list)

    def __init__(self, url: str, key: str):
        super().__init__()
        self._url = url
        self._key = key

    def run(self) -> None:
        client = WavelogClient(self._url, self._key, "test")
        try:
            ok, summary, detail = client.test_connection()
        except Exception as e:
            log.error(f"WavelogTester exception: {e}", exc_info=True)
            ok, summary, detail = False, str(e), [f"  Ausnahme: {e}"]
        self.result_ready.emit(ok, summary, detail)


# ─── Periodic Update Worker ───────────────────────────────────────────────────

class UpdateWorker(QThread):
    log_message    = pyqtSignal(str)
    wavelog_status = pyqtSignal(bool, str)

    def __init__(self, config: dict):
        super().__init__()
        self._config              = dict(config)
        self._run                 = False
        self._current_data: dict | None = None
        self._lock                = threading.Lock()
        self._client              = self._make_client()

    def _make_client(self) -> WavelogClient:
        return WavelogClient(
            self._config.get("wavelog_url",     ""),
            self._config.get("wavelog_api_key", ""),
            self._config.get("radio_name",      "FlexRadio 6600"),
        )

    def set_config(self, config: dict) -> None:
        self._config = dict(config)
        self._client = self._make_client()

    def update_radio_data(self, data: dict) -> None:
        with self._lock:
            self._current_data = data

    # ── BUGFIX ────────────────────────────────────────────────────────────────
    def clear_radio_data(self) -> None:
        """Löscht die gespeicherten Frequenz-/Modusdaten.

        Wird aufgerufen, wenn SmartSDR / FlexRadio die Verbindung trennt,
        damit der Worker keine veralteten Daten mehr an Wavelog sendet.
        """
        with self._lock:
            self._current_data = None
        log.info("UpdateWorker: Frequenzdaten gelöscht (FlexRadio getrennt)")
    # ── Ende BUGFIX ───────────────────────────────────────────────────────────

    def run(self) -> None:
        self._run = True
        log.info("UpdateWorker thread started")
        while self._run:
            interval = max(1, self._config.get("update_interval", 5))
            time.sleep(interval)
            if not self._run:
                break
            with self._lock:
                data = self._current_data
            if data is None:
                continue
            if not self._config.get("wavelog_url") or not self._config.get("wavelog_api_key"):
                self.log_message.emit("⚠ Wavelog URL oder API-Schlüssel fehlt")
                continue
            ok, msg = self._client.send_radio_data(data["frequency"], data["mode"])
            freq_mhz = data["frequency"] / 1_000_000
            if ok:
                self.log_message.emit(f"✓ {freq_mhz:.3f} MHz / {data['mode']}")
                self.wavelog_status.emit(True,  f"{freq_mhz:.3f} MHz / {data['mode']}")
            else:
                self.log_message.emit(f"✗ Wavelog: {msg}")
                self.wavelog_status.emit(False, msg)

    def stop(self) -> None:
        self._run = False


# ─── Helpers ──────────────────────────────────────────────────────────────────

def make_tray_icon(connected: bool = False) -> QIcon:
    px = QPixmap(32, 32)
    px.fill(Qt.GlobalColor.transparent)
    p = QPainter(px)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setBrush(QColor("#22c55e" if connected else "#64748b"))
    p.setPen(QPen(QColor("#0f172a"), 2))
    p.drawEllipse(4, 4, 24, 24)
    p.setPen(QPen(QColor("#ffffff"), 2))
    p.drawArc(10, 10, 12, 12, 0, 360 * 16)
    p.end()
    return QIcon(px)

BTN_P = ("QPushButton{background:%s;color:white;border-radius:6px;"
         "padding:7px 16px;font-weight:bold;font-size:13px;}"
         "QPushButton:hover{background:%s;}")
BTN_N = ("QPushButton{background:#f1f5f9;color:#475569;border-radius:6px;"
         "padding:7px 14px;font-size:13px;border:1px solid #cbd5e1;}"
         "QPushButton:hover{background:#e2e8f0;}")
EDIT  = ("border:1px solid #cbd5e1;border-radius:6px;"
         "padding:6px 10px;background:white;color:#1e293b;")

MAIN_STYLE = """
QMainWindow,QWidget{background:#f8fafc;font-family:'Segoe UI',sans-serif;}
QGroupBox{border:1px solid #e2e8f0;border-radius:8px;
          margin-top:14px;padding:12px;background:white;}
QGroupBox::title{subcontrol-origin:margin;left:12px;top:-7px;
                 background:white;padding:0 6px;
                 color:#475569;font-size:12px;font-weight:bold;}
QLabel{color:#1e293b;}
QLineEdit{border:1px solid #cbd5e1;border-radius:6px;
          padding:6px 10px;background:white;color:#1e293b;}
QLineEdit:focus{border-color:#3b82f6;}
QSpinBox{border:1px solid #cbd5e1;border-radius:6px;
         padding:4px 8px;background:white;}
QPushButton{border-radius:6px;padding:7px 16px;font-size:13px;}
QTextEdit{border:1px solid #e2e8f0;border-radius:6px;
          background:#0f172a;color:#94a3b8;
          font-family:'Consolas',monospace;font-size:12px;padding:8px;}
QTabWidget::pane{border:none;}
QTabBar::tab{background:#f1f5f9;color:#64748b;
             padding:8px 20px;border:none;font-size:13px;}
QTabBar::tab:selected{background:#f8fafc;color:#1e293b;
                      font-weight:bold;border-bottom:2px solid #3b82f6;}
"""


# ─── Discovery Dialog ─────────────────────────────────────────────────────────

class DiscoveryDialog(QDialog):
    radio_selected = pyqtSignal(str, int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("FlexRadio entdecken")
        self.setMinimumWidth(520)
        self.setModal(True)
        self._radios: list[dict] = []
        self._build()

    def _build(self) -> None:
        lay = QVBoxLayout(self)
        lay.setSpacing(12); lay.setContentsMargins(16, 16, 16, 16)

        title = QLabel("Verfügbare FlexRadio-Geräte")
        title.setStyleSheet("font-size:15px;font-weight:bold;color:#1e293b;")
        lay.addWidget(title)

        hint = QLabel("Sucht via VITA-49 UDP-Broadcast (Port 4992). SmartSDR muss aktiv sein.")
        hint.setStyleSheet("color:#64748b;font-size:12px;"); hint.setWordWrap(True)
        lay.addWidget(hint)

        self.combo = QComboBox()
        self.combo.setMinimumHeight(34)
        self.combo.setStyleSheet(
            "QComboBox{border:1px solid #cbd5e1;border-radius:6px;"
            "padding:5px 10px;font-size:13px;}"
            "QComboBox::drop-down{border:none;}")
        lay.addWidget(self.combo)

        self.status_lbl = QLabel("Bereit zur Suche.")
        self.status_lbl.setStyleSheet("color:#475569;font-size:12px;")
        lay.addWidget(self.status_lbl)

        manual = QGroupBox("Oder manuell eingeben")
        manual.setStyleSheet(
            "QGroupBox{font-size:12px;font-weight:bold;color:#475569;"
            "border:1px solid #e2e8f0;border-radius:6px;margin-top:10px;padding:10px;}")
        ml = QHBoxLayout(manual)
        self.host_edit = QLineEdit(); self.host_edit.setPlaceholderText("IP-Adresse")
        self.host_edit.setStyleSheet(EDIT)
        self.port_edit = QLineEdit("4992"); self.port_edit.setMaximumWidth(65)
        self.port_edit.setStyleSheet(EDIT)
        ml.addWidget(QLabel("Host:")); ml.addWidget(self.host_edit)
        ml.addWidget(QLabel("Port:")); ml.addWidget(self.port_edit)
        lay.addWidget(manual)

        row = QHBoxLayout()
        self.scan_btn = QPushButton("🔍 Suchen")
        self.scan_btn.clicked.connect(self._scan)
        self.scan_btn.setStyleSheet(BTN_P % ("#3b82f6", "#2563eb"))
        ok_btn  = QPushButton("Verbinden"); ok_btn.clicked.connect(self._connect)
        ok_btn.setStyleSheet(BTN_P % ("#22c55e", "#16a34a"))
        cancel  = QPushButton("Abbrechen"); cancel.clicked.connect(self.reject)
        cancel.setStyleSheet(BTN_N)
        row.addWidget(self.scan_btn); row.addStretch()
        row.addWidget(cancel); row.addWidget(ok_btn)
        lay.addLayout(row)

    def _scan(self) -> None:
        self.scan_btn.setEnabled(False); self.scan_btn.setText("Suche läuft…")
        self.status_lbl.setText("Lausche auf VITA-49 Discovery-Pakete…")
        self.combo.clear(); self._radios = []
        QApplication.processEvents()

        def _do():
            found = FlexDiscovery.discover(4.0)
            QTimer.singleShot(0, lambda: self._show_results(found))

        threading.Thread(target=_do, daemon=True).start()

    def _show_results(self, radios: list[dict]) -> None:
        self._radios = radios
        self.combo.clear()
        if radios:
            for r in radios:
                label = (f"{r.get('nickname') or r.get('model', 'FlexRadio')}"
                         f" – {r.get('ip', '?')}"
                         + (f" (v{r['version']})" if "version" in r else "")
                         + (f" [{r['status']}]"   if "status"  in r else ""))
                self.combo.addItem(label)
            self.status_lbl.setText(f"✓ {len(radios)} Gerät(e) gefunden.")
        else:
            self.combo.addItem("Keine Geräte gefunden")
            self.status_lbl.setText(
                "Kein FlexRadio entdeckt. SmartSDR aktiv? IP manuell eingeben.")
        self.scan_btn.setEnabled(True); self.scan_btn.setText("🔍 Suchen")

    def _connect(self) -> None:
        manual = self.host_edit.text().strip()
        if manual:
            try:   port = int(self.port_edit.text().strip() or "4992")
            except ValueError: port = 4992
            self.radio_selected.emit(manual, port); self.accept(); return
        idx = self.combo.currentIndex()
        if 0 <= idx < len(self._radios):
            r = self._radios[idx]
            if r.get("ip"):
                self.radio_selected.emit(r["ip"], int(r.get("port", 4992)))
                self.accept(); return
        QMessageBox.warning(self, "Kein Gerät", "Bitte Gerät auswählen oder IP eingeben.")


# ─── Main Window ──────────────────────────────────────────────────────────────

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.config       = load_config()
        self._flex:   FlexRadioClient | None = None
        self._worker: UpdateWorker    | None = None
        self._tester: WavelogTester   | None = None
        self._conn = False

        self.setWindowTitle("FlexRadio → Wavelog Bridge")
        self.setMinimumSize(760, 640)
        self._build_ui()
        self._build_tray()
        self._start_worker()
        log.info("MainWindow ready")

        if self.config.get("auto_connect") and self.config.get("flex_host"):
            QTimer.singleShot(800, self._auto_connect)

    # ── UI Build ──────────────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        self.setStyleSheet(MAIN_STYLE)
        central = QWidget(); self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setSpacing(0); root.setContentsMargins(0, 0, 0, 0)

        # Header
        hdr = QFrame(); hdr.setStyleSheet("background:#0f172a;border:none;"); hdr.setFixedHeight(64)
        hl  = QHBoxLayout(hdr); hl.setContentsMargins(20, 0, 20, 0)
        hl.addWidget(self._lbl("⚡ FlexRadio → Wavelog Bridge",
                               "color:white;font-size:18px;font-weight:bold;"))
        hl.addStretch()
        self.conn_badge = self._lbl("● Getrennt", "color:#64748b;font-size:13px;font-weight:bold;")
        hl.addWidget(self.conn_badge)
        root.addWidget(hdr)

        # Live status band
        sf = QFrame(); sf.setStyleSheet("background:#1e293b;border:none;"); sf.setFixedHeight(50)
        sl = QHBoxLayout(sf); sl.setContentsMargins(20, 0, 20, 0); sl.setSpacing(28)
        self.freq_lbl = self._lbl("– – –.– – – MHz",
                                  "color:#38bdf8;font-size:22px;font-weight:bold;font-family:'Consolas';")
        self.mode_lbl = self._lbl("–", "color:#a78bfa;font-size:18px;font-weight:bold;")
        sl.addWidget(self.freq_lbl); sl.addWidget(self.mode_lbl); sl.addStretch()
        self.wl_badge = self._lbl("Wavelog: –", "color:#64748b;font-size:12px;")
        sl.addWidget(self.wl_badge)
        root.addWidget(sf)

        # Tabs
        tabs = QTabWidget(); root.addWidget(tabs)
        tabs.addTab(self._build_tab_control(), "Steuerung")
        tabs.addTab(self._build_tab_config(),  "Konfiguration")

    def _lbl(self, text: str, style: str = "") -> QLabel:
        l = QLabel(text)
        if style: l.setStyleSheet(style)
        return l

    def _build_tab_control(self) -> QWidget:
        w  = QWidget(); cl = QVBoxLayout(w)
        cl.setContentsMargins(16, 16, 16, 16); cl.setSpacing(12)

        # FlexRadio group
        fg = QGroupBox("FlexRadio Verbindung"); ff = QFormLayout(fg); ff.setSpacing(10)
        host_txt       = self.config.get("flex_host") or "Nicht konfiguriert"
        self.host_lbl  = self._lbl(host_txt, "color:#475569;font-size:13px;")
        ff.addRow("Gerät:", self.host_lbl)
        br = QHBoxLayout()
        self.discover_btn = QPushButton("🔍 Gerät suchen")
        self.discover_btn.clicked.connect(self._open_discovery)
        self.discover_btn.setStyleSheet(BTN_P % ("#3b82f6", "#2563eb"))
        self.connect_btn  = QPushButton("Verbinden")
        self.connect_btn.clicked.connect(self._toggle_conn)
        self.connect_btn.setStyleSheet(BTN_P % ("#22c55e", "#16a34a"))
        br.addWidget(self.discover_btn); br.addWidget(self.connect_btn); br.addStretch()
        ff.addRow("", br)
        self.auto_cb = QCheckBox("Beim Start automatisch verbinden")
        self.auto_cb.setChecked(bool(self.config.get("auto_connect")))
        self.auto_cb.toggled.connect(lambda v: self._patch("auto_connect", v))
        ff.addRow("", self.auto_cb)
        cl.addWidget(fg)

        # Slice selection group
        sg = QGroupBox("Slice-Auswahl"); sv = QVBoxLayout(sg); sv.setSpacing(8)
        mr = QHBoxLayout()
        self.slice_auto_btn   = QPushButton("🔄 Auto (TX-Slice)")
        self.slice_manual_btn = QPushButton("☑ Manuell wählen")
        self.slice_auto_btn.setCheckable(True); self.slice_manual_btn.setCheckable(True)
        self.slice_auto_btn.setChecked(True)
        _sb = ("QPushButton{background:#f1f5f9;color:#475569;border-radius:6px;"
               "padding:6px 14px;font-size:13px;border:1px solid #cbd5e1;}"
               "QPushButton:checked{background:#3b82f6;color:white;border-color:#3b82f6;}"
               "QPushButton:hover:!checked{background:#e2e8f0;}")
        self.slice_auto_btn.setStyleSheet(_sb); self.slice_manual_btn.setStyleSheet(_sb)
        self.slice_auto_btn.clicked.connect(self._set_slice_auto)
        self.slice_manual_btn.clicked.connect(self._set_slice_manual)
        mr.addWidget(self.slice_auto_btn); mr.addWidget(self.slice_manual_btn); mr.addStretch()
        sv.addLayout(mr)
        cr = QHBoxLayout()
        self.slice_combo = QComboBox(); self.slice_combo.setMinimumHeight(32)
        self.slice_combo.setEnabled(False)
        self.slice_combo.setStyleSheet(
            "QComboBox{border:1px solid #cbd5e1;border-radius:6px;"
            "padding:4px 10px;font-size:13px;background:white;}"
            "QComboBox:disabled{background:#f1f5f9;color:#94a3b8;}"
            "QComboBox::drop-down{border:none;}")
        self.slice_combo.currentIndexChanged.connect(self._on_slice_combo_changed)
        cr.addWidget(self._lbl("Slice:")); cr.addWidget(self.slice_combo, stretch=1)
        sv.addLayout(cr)
        self.slice_status_lbl = self._lbl("Kein FlexRadio verbunden", "color:#64748b;font-size:12px;")
        sv.addWidget(self.slice_status_lbl)
        cl.addWidget(sg)

        # Log group
        lg = QGroupBox("Protokoll"); lv = QVBoxLayout(lg)
        self.log_out = QTextEdit(); self.log_out.setReadOnly(True); self.log_out.setMinimumHeight(180)
        lv.addWidget(self.log_out)
        clr = QPushButton("Löschen"); clr.setMaximumWidth(80)
        clr.setStyleSheet(BTN_N); clr.clicked.connect(self.log_out.clear)
        lv.addWidget(clr, alignment=Qt.AlignmentFlag.AlignRight)
        cl.addWidget(lg)
        return w

    def _build_tab_config(self) -> QWidget:
        w  = QWidget(); cv = QVBoxLayout(w)
        cv.setContentsMargins(16, 16, 16, 16); cv.setSpacing(12)

        wg = QGroupBox("Wavelog Einstellungen"); wf = QFormLayout(wg); wf.setSpacing(10)
        self.url_edit = QLineEdit(self.config.get("wavelog_url", ""))
        self.url_edit.setPlaceholderText("https://log.meineinstanz.de")
        wf.addRow("Wavelog URL:", self.url_edit)

        key_row = QHBoxLayout()
        self.key_edit = QLineEdit(self.config.get("wavelog_api_key", ""))
        self.key_edit.setPlaceholderText("API-Schlüssel (Lesen + Schreiben)")
        self.key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.key_edit.setStyleSheet(EDIT)
        self.show_key_btn = QPushButton("👁")
        self.show_key_btn.setCheckable(True); self.show_key_btn.setFixedWidth(34)
        self.show_key_btn.setToolTip("Schlüssel anzeigen/verbergen")
        self.show_key_btn.setStyleSheet(
            "QPushButton{border:1px solid #cbd5e1;border-radius:6px;"
            "background:white;font-size:14px;padding:2px;}"
            "QPushButton:checked{background:#dbeafe;border-color:#3b82f6;}"
            "QPushButton:hover{background:#f1f5f9;}")
        self.show_key_btn.toggled.connect(
            lambda v: (self.key_edit.setEchoMode(
                QLineEdit.EchoMode.Normal if v else QLineEdit.EchoMode.Password),
                self.show_key_btn.setText("🙈" if v else "👁")))
        key_row.addWidget(self.key_edit); key_row.addWidget(self.show_key_btn)
        wf.addRow("API-Schlüssel:", key_row)

        self.rname = QLineEdit(self.config.get("radio_name", "FlexRadio 6600"))
        wf.addRow("Funkgerät-Name:", self.rname)

        self.interval = QSpinBox()
        self.interval.setRange(1, 120); self.interval.setValue(self.config.get("update_interval", 5))
        self.interval.setSuffix(" Sekunden")
        wf.addRow("Update-Intervall:", self.interval)

        test_btn = QPushButton("🔗 Wavelog-Verbindung testen")
        test_btn.clicked.connect(self._test_wavelog)
        test_btn.setStyleSheet(BTN_P % ("#8b5cf6", "#7c3aed"))
        wf.addRow("", test_btn)
        cv.addWidget(wg)

        save_btn = QPushButton("💾 Einstellungen speichern")
        save_btn.clicked.connect(self._save_config)
        save_btn.setStyleSheet(
            "QPushButton{background:#0f172a;color:white;font-weight:bold;"
            "font-size:14px;padding:10px;border-radius:6px;}"
            "QPushButton:hover{background:#1e293b;}")
        cv.addWidget(save_btn); cv.addStretch()
        return w

    # ── Tray ──────────────────────────────────────────────────────────────────

    def _build_tray(self) -> None:
        self._tray = QSystemTrayIcon(make_tray_icon(False), self)
        self._tray.setToolTip("FlexRadio → Wavelog Bridge")
        menu = QMenu()
        show_a = QAction("Fenster anzeigen", self); show_a.triggered.connect(self.show)
        menu.addAction(show_a); menu.addSeparator()
        self._tray_status = QAction("Status: Getrennt", self)
        self._tray_status.setEnabled(False); menu.addAction(self._tray_status)
        menu.addSeparator()
        quit_a = QAction("Beenden", self); quit_a.triggered.connect(QApplication.quit)
        menu.addAction(quit_a)
        self._tray.setContextMenu(menu)
        self._tray.activated.connect(
            lambda r: (self.show(), self.raise_(), self.activateWindow())
            if r == QSystemTrayIcon.ActivationReason.DoubleClick else None)
        self._tray.show()

    def _start_worker(self) -> None:
        self._worker = UpdateWorker(self.config)
        self._worker.log_message.connect(self._append_log)
        self._worker.wavelog_status.connect(self._on_wl_status)
        self._worker.start()

    # ── Connection ────────────────────────────────────────────────────────────

    def _open_discovery(self) -> None:
        dlg = DiscoveryDialog(self)
        dlg.radio_selected.connect(self._on_radio_selected)
        dlg.exec()

    def _on_radio_selected(self, host: str, port: int) -> None:
        self.config["flex_host"] = host; self.config["flex_port"] = port
        self.host_lbl.setText(f"{host}:{port}"); save_config(self.config)
        self._append_log(f"Gerät ausgewählt: {host}:{port}")
        self._connect_to(host, port)

    def _toggle_conn(self) -> None:
        if self._conn:
            self._disconnect()
        else:
            host = self.config.get("flex_host", "")
            if not host:
                QMessageBox.warning(self, "Kein Gerät",
                                    "Bitte zuerst ein FlexRadio suchen und auswählen.")
                return
            self._connect_to(host, self.config.get("flex_port", 4992))

    def _auto_connect(self) -> None:
        host = self.config.get("flex_host", "")
        if host:
            self._connect_to(host, self.config.get("flex_port", 4992))

    def _connect_to(self, host: str, port: int) -> None:
        if self._flex and self._flex.isRunning():
            self._flex.stop(); self._flex.wait(2000)

        self._append_log(f"Verbinde mit {host}:{port} …")
        self._flex = FlexRadioClient(host, port)
        self._flex.status_changed.connect(self._on_flex_status)
        self._flex.radio_data.connect(self._on_radio_data)
        self._flex.log_message.connect(self._append_log)
        self._flex.slices_changed.connect(self._on_slices_changed)
        self._flex.start()

    def _disconnect(self) -> None:
        if self._flex:
            self._flex.stop()
            self._flex.wait(3000)
            self._flex = None
        self._on_flex_status("disconnected")

    # ── Signal Handlers ───────────────────────────────────────────────────────

    @pyqtSlot(str)
    def _on_flex_status(self, status: str) -> None:
        connected = (status == "connected")
        self._conn = connected
        self.conn_badge.setText(
            "● Verbunden" if connected else "● Getrennt")
        self.conn_badge.setStyleSheet(
            f"color:{'#22c55e' if connected else '#64748b'};"
            "font-size:13px;font-weight:bold;")
        self.connect_btn.setText("Trennen" if connected else "Verbinden")
        self.connect_btn.setStyleSheet(
            BTN_P % (("#ef4444", "#dc2626") if connected else ("#22c55e", "#16a34a")))
        self._tray.setIcon(make_tray_icon(connected))
        self._tray_status.setText(
            f"Status: {'Verbunden' if connected else 'Getrennt'}")

        # ── BUGFIX ────────────────────────────────────────────────────────────
        # Wenn FlexRadio / SmartSDR die Verbindung trennt, gespeicherte
        # Frequenz-/Modusdaten löschen, damit der UpdateWorker keine
        # veralteten Daten mehr an Wavelog sendet.
        if not connected and self._worker:
            self._worker.clear_radio_data()
            self.freq_lbl.setText("– – –.– – – MHz")
            self.mode_lbl.setText("–")
            self.wl_badge.setText("Wavelog: –")
            self.wl_badge.setStyleSheet("color:#64748b;font-size:12px;")
        # ── Ende BUGFIX ───────────────────────────────────────────────────────

        if not connected:
            self.slice_combo.clear()
            self.slice_status_lbl.setText("Kein FlexRadio verbunden")

    @pyqtSlot(str)
    def _on_radio_data(self, payload_json: str) -> None:
        try:
            data = json.loads(payload_json)
        except Exception as e:
            log.warning(f"_on_radio_data JSON parse error: {e}")
            return
        self.freq_lbl.setText(f"{data['freq_mhz']:.3f} MHz")
        self.mode_lbl.setText(data["mode"])
        if self._worker:
            self._worker.update_radio_data(data)

    @pyqtSlot(str)
    def _on_slices_changed(self, slices_json: str) -> None:
        try:
            slices = json.loads(slices_json)
        except Exception as e:
            log.warning(f"_on_slices_changed JSON parse error: {e}")
            return
        self._refresh_slice_combo(slices)

    @pyqtSlot(bool, str)
    def _on_wl_status(self, ok: bool, msg: str) -> None:
        color = "#22c55e" if ok else "#ef4444"
        self.wl_badge.setText(f"Wavelog: {msg}")
        self.wl_badge.setStyleSheet(f"color:{color};font-size:12px;")

    # ── Slice UI ──────────────────────────────────────────────────────────────

    def _refresh_slice_combo(self, slices: dict) -> None:
        self.slice_combo.blockSignals(True)
        prev = self.slice_combo.currentData()
        self.slice_combo.clear()
        for idx, s in sorted(slices.items(), key=lambda x: int(x[0]) if x[0].isdigit() else 99):
            freq = s.get("rf_frequency", 0)
            mode = s.get("mode", "?")
            tx   = " [TX]" if s.get("tx") else ""
            self.slice_combo.addItem(f"Slice {idx}: {freq:.3f} MHz {mode}{tx}", userData=idx)
        # Restore selection if still present
        for i in range(self.slice_combo.count()):
            if self.slice_combo.itemData(i) == prev:
                self.slice_combo.setCurrentIndex(i)
                break
        self.slice_combo.blockSignals(False)
        n = self.slice_combo.count()
        self.slice_status_lbl.setText(
            f"{n} Slice(s) verfügbar" if n else "Keine Slices – öffne einen Slice in SmartSDR")

    def _set_slice_auto(self) -> None:
        self.slice_auto_btn.setChecked(True)
        self.slice_manual_btn.setChecked(False)
        self.slice_combo.setEnabled(False)
        if self._flex:
            self._flex.selected_slice = None
        self._append_log("Slice-Modus: Auto (TX-Slice)")

    def _set_slice_manual(self) -> None:
        self.slice_auto_btn.setChecked(False)
        self.slice_manual_btn.setChecked(True)
        self.slice_combo.setEnabled(True)
        self._on_slice_combo_changed(self.slice_combo.currentIndex())
        self._append_log("Slice-Modus: Manuell")

    def _on_slice_combo_changed(self, idx: int) -> None:
        if not self.slice_manual_btn.isChecked():
            return
        data = self.slice_combo.itemData(idx)
        if data is not None and self._flex:
            self._flex.selected_slice = data
            self._append_log(f"Manuell gewählter Slice: {data}")

    # ── Config ────────────────────────────────────────────────────────────────

    def _save_config(self) -> None:
        self.config.update({
            "wavelog_url":     self.url_edit.text().strip(),
            "wavelog_api_key": self.key_edit.text().strip(),
            "radio_name":      self.rname.text().strip() or "FlexRadio 6600",
            "update_interval": self.interval.value(),
        })
        save_config(self.config)
        if self._worker:
            self._worker.set_config(self.config)
        self._append_log("✓ Einstellungen gespeichert")

    def _patch(self, key: str, value) -> None:
        self.config[key] = value
        save_config(self.config)

    # ── Wavelog Test ──────────────────────────────────────────────────────────

    def _test_wavelog(self) -> None:
        url = self.url_edit.text().strip()
        key = self.key_edit.text().strip()
        if not url or not key:
            QMessageBox.warning(self, "Fehlende Daten",
                                "Bitte Wavelog URL und API-Schlüssel eingeben.")
            return
        self._append_log("Teste Wavelog-Verbindung …")
        self._tester = WavelogTester(url, key)
        self._tester.result_ready.connect(self._on_test_result)
        self._tester.start()

    @pyqtSlot(bool, str, list)
    def _on_test_result(self, ok: bool, summary: str, detail: list) -> None:
        icon = QMessageBox.Icon.Information if ok else QMessageBox.Icon.Warning
        msg  = QMessageBox(icon,
                           "Wavelog Verbindungstest",
                           f"{'✓' if ok else '✗'} {summary}\n\n" + "\n".join(detail),
                           QMessageBox.StandardButton.Ok, self)
        msg.exec()
        self._append_log(f"Wavelog-Test: {'✓' if ok else '✗'} {summary}")

    # ── Log ───────────────────────────────────────────────────────────────────

    def _append_log(self, text: str) -> None:
        ts  = datetime.datetime.now().strftime("%H:%M:%S")
        self.log_out.append(f"[{ts}] {text}")
        log.debug(f"GUI-Log: {text}")

    # ── Window close → tray ───────────────────────────────────────────────────

    def closeEvent(self, event) -> None:
        event.ignore()
        self.hide()
        self._tray.showMessage(
            "FlexRadio → Wavelog Bridge",
            "Läuft weiterhin im System-Tray. Doppelklick zum Öffnen.",
            QSystemTrayIcon.MessageIcon.Information, 3000)


# ─── Entry Point ──────────────────────────────────────────────────────────────

def main() -> None:
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    app.setApplicationName("FlexRadio Wavelog Bridge")
    win = MainWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
