"""
FlexRadio 6600 → Wavelog Bridge  –  FlexLib API v4.1.5
"""

import sys, os, json, socket, struct, threading, time, re, logging, datetime, requests
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

LOG_DIR = Path(os.environ.get("APPDATA", ".")) / "FlexWavelogBridge"
LOG_DIR.mkdir(parents=True, exist_ok=True)
LOG_FILE = LOG_DIR / "bridge.log"

_file_handler   = logging.FileHandler(LOG_FILE, encoding="utf-8")
_stdout_handler = logging.StreamHandler(sys.stdout)
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)-7s] %(message)s",
    handlers=[_file_handler, _stdout_handler],
)
log = logging.getLogger("FlexWavelog")

CONFIG_FILE = LOG_DIR / "config.json"

# ─── Config ───────────────────────────────────────────────────────────────────

DEFAULT_CONFIG = {
    "wavelog_url":     "",
    "wavelog_api_key": "",
    "radio_name":      "FlexRadio 6600",
    "update_interval": 5,
    "flex_host":       "",
    "flex_port":       4992,
    "auto_connect":    False,
}

def load_config():
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                return {**DEFAULT_CONFIG, **json.load(f)}
        except Exception as e:
            log.warning(f"Config laden fehlgeschlagen: {e}")
    return dict(DEFAULT_CONFIG)

def save_config(cfg):
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
        log.debug(f"Config gespeichert: {CONFIG_FILE}")
    except Exception as e:
        log.error(f"Config speichern fehlgeschlagen: {e}")


# ─── VITA-49 Discovery ────────────────────────────────────────────────────────
#  Reference: VitaDiscovery.cs, VitaFlex.cs, Discovery.cs (FlexLib v4.1.5)
#  FlexRadio broadcasts binary VITA-49 UDP packets on port 4992.
#  OUI=0x001C2D, PacketClassCode=0xFFFF (SL_VITA_DISCOVERY_CLASS)

FLEX_OUI                     = 0x001C2D
VITA_EXT_DATA_WITH_STREAM    = 0x7
SL_VITA_DISCOVERY_CLASS      = 0xFFFF
DISCOVERY_PORT               = 4992


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

    idx  = 4 + 4           # skip stream_id
    oui  = struct.unpack_from(">I", data, idx)[0] & 0x00FFFFFF;  idx += 4
    pcc  = struct.unpack_from(">I", data, idx)[0] & 0xFFFF;      idx += 4

    if oui != FLEX_OUI or pcc != SL_VITA_DISCOVERY_CLASS:
        return None

    if tsi: idx += 4
    if tsf: idx += 8

    total_bytes   = pkt_size * 4
    payload_end   = total_bytes - (4 if has_trl else 0)
    payload_len   = payload_end - idx

    if payload_len <= 0 or idx + payload_len > len(data):
        return None

    payload = data[idx:idx + payload_len].decode("utf-8", errors="replace").strip("\x00 ")
    result  = {"ip": src_ip}
    for kv in payload.split():
        k, _, v = kv.partition("=")
        if k and v:
            result[k.lower()] = v
    result.setdefault("nickname", result.get("model", "FlexRadio"))
    return result


class FlexDiscovery:
    @staticmethod
    def discover(timeout: float = 4.0) -> list[dict]:
        found: dict[str, dict] = {}
        done  = threading.Event()

        def _listen():
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                sock.settimeout(0.5)
                try:
                    sock.bind(("", DISCOVERY_PORT))
                    log.debug(f"Discovery: lauscht auf UDP port {DISCOVERY_PORT}")
                except OSError as e:
                    log.warning(f"Discovery: Port {DISCOVERY_PORT} belegt ({e}). "
                                "Bitte IP manuell eingeben.")
                    return
                deadline = time.monotonic() + timeout
                while not done.is_set() and time.monotonic() < deadline:
                    try:
                        data, addr = sock.recvfrom(4096)
                        info = _parse_vita_discovery(data, addr[0])
                        if info:
                            key = info.get("ip", addr[0])
                            if key not in found:
                                log.info(f"Discovery: gefunden {info.get('nickname')} @ {key}")
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
        log.info(f"Discovery: {len(found)} Gerät(e) gefunden")
        return list(found.values())


# ─── FlexRadio TCP Client ─────────────────────────────────────────────────────
#  Reference: TcpCommandCommunication.cs, Radio.cs, Slice.cs (FlexLib v4.1.5)
#  TCP port 4992, line-delimited (\n).
#  Slice keys: rf_frequency (MHz, double), mode (string)

class FlexRadioClient(QThread):
    status_changed  = pyqtSignal(str)
    radio_data      = pyqtSignal(dict)
    log_message     = pyqtSignal(str)
    # Emitted whenever the set of known slices changes (new slice, removed,
    # tx flag changed). Carries a snapshot: {idx: {rf_frequency, mode, tx, ...}}
    slices_changed  = pyqtSignal(dict)

    MODE_MAP = {
        "USB": "SSB", "LSB": "SSB",
        "AM":  "AM",  "SAM": "AM",
        "FM":  "FM",  "NFM": "FM",  "DFM": "FM",
        "CW":  "CW",
        "RTTY":"RTTY",
        "DIGU":"DIGI","DIGL":"DIGI","FDV":"DIGI",
    }

    def __init__(self, host: str, port: int = 4992):
        super().__init__()
        self.host      = host
        self.port      = port
        self._running  = False
        self._sock     = None
        self._seq      = 1
        # {idx: {"rf_frequency": float, "mode": str, "tx": bool, ...}}
        self._slices: dict[str, dict] = {}
        self._handle   = ""
        # None  → auto (follow TX slice)
        # "0","1",... → manually pinned slice index
        self.selected_slice: str | None = None

    def run(self):
        self._running = True
        try:
            self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self._sock.settimeout(10)
            log.info(f"FlexRadio: verbinde TCP {self.host}:{self.port}")
            self._sock.connect((self.host, self.port))
            self._sock.settimeout(1.5)
            self.status_changed.emit("connected")
            self.log_message.emit(f"TCP verbunden: {self.host}:{self.port}")

            # Initialisierungssequenz (Radio.cs ~Z.1914-1965)
            for cmd in [
                "client program FlexWavelogBridge",
                "client start_persistence off",
                "sub client all",
                "sub tx all",
                "sub atu all",
                "sub slice all",
                "sub gps all",
            ]:
                self._cmd(cmd)
                log.debug(f"FlexRadio TX: {cmd}")

            buf = ""
            while self._running:
                try:
                    chunk = self._sock.recv(4096).decode("utf-8", errors="replace")
                    if not chunk:
                        break
                    buf += chunk
                    while "\n" in buf:
                        line, buf = buf.split("\n", 1)
                        line = line.strip("\r\0")
                        if line:
                            log.debug(f"FlexRadio RX: {line[:120]}")
                            self._parse_line(line)
                except socket.timeout:
                    self._cmd("ping")
                    continue
                except OSError:
                    break
        except ConnectionRefusedError:
            msg = f"Verbindung zu {self.host}:{self.port} abgelehnt"
            log.warning(f"FlexRadio: {msg}")
            self.status_changed.emit(f"error: {msg}")
            self.log_message.emit(f"✗ {msg}")
        except socket.timeout:
            msg = f"Timeout beim Verbinden zu {self.host}:{self.port}"
            log.warning(f"FlexRadio: {msg}")
            self.status_changed.emit(f"error: {msg}")
            self.log_message.emit(f"✗ {msg}")
        except Exception as e:
            log.error(f"FlexRadio Verbindungsfehler: {e}", exc_info=True)
            self.status_changed.emit(f"error: {e}")
            self.log_message.emit(f"✗ Fehler: {e}")
        finally:
            if self._sock:
                try: self._sock.close()
                except Exception: pass
                self._sock = None
            self.status_changed.emit("disconnected")
            self.log_message.emit("FlexRadio Verbindung getrennt")

    def _cmd(self, command: str):
        if not self._sock:
            return
        try:
            self._sock.sendall(f"C{self._seq}|{command}\n".encode())
            self._seq += 1
        except Exception as e:
            log.debug(f"FlexRadio send error: {e}")

    def _parse_line(self, line: str):
        if not line:
            return
        ch = line[0]
        if ch == 'H':
            parts = line.split("|", 1)
            self._handle = parts[0][1:]
            self.log_message.emit(f"Client-Handle: 0x{self._handle}")
            log.info(f"FlexRadio: Client-Handle 0x{self._handle}")
        elif ch == 'V':
            ver = line[1:]
            self.log_message.emit(f"Protokoll-Version: {ver}")
            log.info(f"FlexRadio: Protokollversion {ver}")
        elif ch == 'S':
            self._parse_status(line)
        elif ch == 'M':
            parts = line.split("|", 1)
            if len(parts) == 2:
                log.info(f"FlexRadio Meldung: {parts[1]}")

    def _parse_status(self, line: str):
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
            slice_idx = words[1]
            if "in_use=0" in body:
                if slice_idx in self._slices:
                    del self._slices[slice_idx]
                    log.debug(f"FlexRadio: Slice {slice_idx} entfernt")
                    self.slices_changed.emit(dict(self._slices))
                return
            update_str = body[len("slice ") + len(slice_idx) + 1:]
            self._update_slice(slice_idx, update_str)

        elif topic == "transmit":
            # S<handle>|transmit tx_slice_handle=<hex_handle>
            # This is broadcast when TX ownership changes between GUI clients.
            # We don't use it directly – the per-slice "tx=0/1" is more reliable.
            pass

    def _update_slice(self, idx: str, update: str):
        is_new = idx not in self._slices
        if is_new:
            self._slices[idx] = {"tx": False}
            log.debug(f"FlexRadio: Slice {idx} angelegt")

        s = self._slices[idx]
        old_tx = s.get("tx", False)
        structure_changed = is_new

        for kv in update.split():
            if "=" not in kv:
                continue
            key, _, val = kv.partition("=")
            k = key.lower()
            if k == "rf_frequency":
                try:
                    s["rf_frequency"] = float(val)
                except ValueError:
                    log.warning(f"Ungültige rf_frequency: {val}")
            elif k == "mode":
                s["mode"] = val.upper()
            elif k == "tx":
                new_tx = (val == "1")
                if new_tx != old_tx:
                    s["tx"] = new_tx
                    structure_changed = True
                    if new_tx:
                        log.info(f"FlexRadio: Slice {idx} ist jetzt TX")
                    # When a slice becomes TX, all others lose TX
                    if new_tx:
                        for other_idx, other_s in self._slices.items():
                            if other_idx != idx and other_s.get("tx"):
                                other_s["tx"] = False
                                log.debug(f"FlexRadio: Slice {other_idx} TX zurückgesetzt")

        if structure_changed:
            self.slices_changed.emit(dict(self._slices))

        # Determine which slice to report based on selection mode
        active_idx = self._active_slice_idx()
        if active_idx != idx:
            return   # this slice is not the one we're reporting

        freq_mhz = s.get("rf_frequency")
        mode_raw = s.get("mode", "")
        if freq_mhz and mode_raw:
            freq_hz      = int(freq_mhz * 1_000_000)
            wavelog_mode = self.MODE_MAP.get(mode_raw, mode_raw)
            log.debug(f"FlexRadio Slice {idx} [{'TX-AUTO' if self.selected_slice is None else 'MANUELL'}]: "
                      f"{freq_mhz:.6f} MHz  {mode_raw} → {wavelog_mode}")
            self.radio_data.emit({
                "frequency": freq_hz,
                "mode":      wavelog_mode,
                "raw_mode":  mode_raw,
                "freq_mhz":  freq_mhz,
                "slice":     idx,
                "tx":        s.get("tx", False),
            })

    def _active_slice_idx(self) -> str | None:
        """Return the index of the slice whose data should be sent to Wavelog."""
        if self.selected_slice is not None:
            # Manual pin: send this slice regardless of TX flag
            return self.selected_slice if self.selected_slice in self._slices else None
        # Auto mode: find the slice with tx=True
        for idx, s in self._slices.items():
            if s.get("tx"):
                return idx
        # Fallback: if no TX slice found, use the numerically lowest slice
        if self._slices:
            return min(self._slices.keys(), key=lambda x: int(x) if x.isdigit() else 0)
        return None

    def stop(self):
        self._running = False
        if self._sock:
            try: self._sock.close()
            except Exception: pass


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
        log.debug(f"Wavelog POST {url}  freq={frequency}  mode={mode}")
        try:
            t0   = time.monotonic()
            resp = self._session.post(url, json=payload, timeout=10)
            ms   = int((time.monotonic() - t0) * 1000)
            log.debug(f"Wavelog response: HTTP {resp.status_code}  ({ms} ms)  {resp.text[:120]}")
            if resp.status_code == 200:
                return True, f"OK ({ms} ms)"
            return False, f"HTTP {resp.status_code}: {resp.text[:200]}"
        except requests.exceptions.SSLError as e:
            log.error(f"Wavelog SSL-Fehler: {e}")
            return False, f"SSL-Fehler: {e}"
        except requests.exceptions.ConnectionError as e:
            log.error(f"Wavelog Verbindungsfehler: {e}")
            return False, f"Verbindung fehlgeschlagen"
        except requests.exceptions.Timeout:
            log.error("Wavelog Timeout")
            return False, "Request-Timeout"
        except Exception as e:
            log.error(f"Wavelog unbekannter Fehler: {e}", exc_info=True)
            return False, str(e)

    def test_connection(self) -> tuple[bool, str, list]:
        """
        Tests /api/version.
        Returns (ok, summary, [detail_lines]) – independent of FlexRadio state.
        """
        url    = f"{self.base_url}/api/version"
        detail = []
        key_preview = self.api_key[:6] + "*" * max(0, len(self.api_key) - 6) if self.api_key else "(leer)"
        detail.append(f"  URL:          {url}")
        detail.append(f"  API-Schlüssel: {key_preview}")
        log.info(f"Wavelog Verbindungstest: POST {url}")
        try:
            t0   = time.monotonic()
            resp = self._session.post(url, json={"key": self.api_key}, timeout=8)
            ms   = int((time.monotonic() - t0) * 1000)
            detail.append(f"  HTTP-Status:   {resp.status_code}  ({ms} ms)")
            detail.append(f"  Antwort:       {resp.text[:300]}")
            log.info(f"Wavelog Verbindungstest: HTTP {resp.status_code}  ({ms} ms)  {resp.text[:200]}")

            if resp.status_code == 200:
                try:
                    ver = resp.json().get("version", "?")
                    detail.append(f"  Wavelog-Version: {ver}")
                    return True, f"Wavelog v{ver} – Verbindung OK", detail
                except Exception:
                    detail.append("  Warnung: Antwort kein gültiges JSON")
                    return True, "Verbindung OK (kein JSON)", detail
            elif resp.status_code == 401:
                detail.append("  → API-Schlüssel ungültig oder abgelaufen")
                return False, "Ungültiger API-Schlüssel (401)", detail
            elif resp.status_code == 404:
                detail.append("  → Endpunkt nicht gefunden – URL korrekt?")
                detail.append("  Tipp: Pfad /api/version erreichbar?")
                return False, "Endpunkt nicht gefunden (404)", detail
            elif resp.status_code == 302:
                location = resp.headers.get("Location", "")
                detail.append(f"  → Weiterleitung nach: {location}")
                detail.append("  Tipp: Evtl. http:// statt https:// – oder umgekehrt?")
                return False, f"Weiterleitung (302)", detail
            else:
                return False, f"HTTP {resp.status_code}", detail

        except requests.exceptions.SSLError as e:
            detail.append(f"  SSL-Fehler: {e}")
            detail.append("  Tipp: Zertifikat ungültig? http:// statt https:// versuchen.")
            log.error(f"Wavelog SSL-Fehler: {e}")
            return False, f"SSL-Fehler", detail
        except requests.exceptions.ConnectionError as e:
            detail.append(f"  Verbindungsfehler: {e}")
            detail.append("  Tipp: Ist die URL erreichbar? Firewall? VPN aktiv?")
            log.error(f"Wavelog Verbindungsfehler: {e}")
            return False, "Verbindung fehlgeschlagen", detail
        except requests.exceptions.Timeout:
            detail.append("  Timeout nach 8 Sekunden")
            detail.append("  Tipp: Server erreichbar? Port offen?")
            log.error("Wavelog Timeout beim Verbindungstest")
            return False, "Timeout", detail
        except Exception as e:
            detail.append(f"  Fehler: {type(e).__name__}: {e}")
            log.error(f"Wavelog Verbindungstest Fehler: {e}", exc_info=True)
            return False, str(e), detail




# ─── Wavelog Connection Tester (QThread – safe cross-thread signal) ───────────

class WavelogTester(QThread):
    """
    Runs test_connection() in a background QThread and emits the result
    via a proper Qt signal – avoids the unreliable QTimer.singleShot-from-
    daemon-thread pattern that caused results to silently disappear.
    """
    result_ready = pyqtSignal(bool, str, list)   # ok, summary, detail_lines

    def __init__(self, url: str, key: str):
        super().__init__()
        self._url = url
        self._key = key

    def run(self):
        client = WavelogClient(self._url, self._key, "test")
        try:
            ok, summary, detail = client.test_connection()
        except Exception as e:
            log.error(f"WavelogTester unhandled exception: {e}", exc_info=True)
            ok, summary, detail = False, str(e), [f"  Ausnahme: {type(e).__name__}: {e}"]
        self.result_ready.emit(ok, summary, detail)

# ─── Periodic Update Worker ───────────────────────────────────────────────────

class UpdateWorker(QThread):
    log_message    = pyqtSignal(str)
    wavelog_status = pyqtSignal(bool, str)

    def __init__(self, config: dict):
        super().__init__()
        self._config       = dict(config)
        self._running      = False
        self._current_data = None
        self._lock         = threading.Lock()
        self._client       = self._make_client()

    def _make_client(self):
        return WavelogClient(
            self._config.get("wavelog_url", ""),
            self._config.get("wavelog_api_key", ""),
            self._config.get("radio_name", "FlexRadio 6600"),
        )

    def set_config(self, config: dict):
        self._config = dict(config)
        self._client = self._make_client()

    def update_radio_data(self, data: dict):
        with self._lock:
            self._current_data = data

    def run(self):
        self._running = True
        while self._running:
            interval = max(1, self._config.get("update_interval", 5))
            time.sleep(interval)
            if not self._running:
                break
            with self._lock:
                data = self._current_data
            if data is None:
                continue
            if not self._config.get("wavelog_url") or not self._config.get("wavelog_api_key"):
                log.debug("UpdateWorker: Wavelog URL oder API-Schlüssel fehlt – übersprungen")
                self.log_message.emit("⚠ Wavelog URL oder API-Schlüssel fehlt")
                continue
            ok, msg = self._client.send_radio_data(data["frequency"], data["mode"])
            freq_mhz = data["frequency"] / 1_000_000
            if ok:
                self.log_message.emit(f"✓ Wavelog: {freq_mhz:.3f} MHz / {data['mode']}")
                self.wavelog_status.emit(True, f"{freq_mhz:.3f} MHz / {data['mode']}")
            else:
                self.log_message.emit(f"✗ Wavelog: {msg}")
                self.wavelog_status.emit(False, f"Fehler: {msg}")

    def stop(self):
        self._running = False


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
EDIT  = "border:1px solid #cbd5e1;border-radius:6px;padding:6px 10px;background:white;color:#1e293b;"


# ─── Discovery Dialog ─────────────────────────────────────────────────────────

class DiscoveryDialog(QDialog):
    radio_selected = pyqtSignal(str, int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("FlexRadio entdecken")
        self.setMinimumWidth(520)
        self.setModal(True)
        self._radios = []
        self._build()

    def _build(self):
        lay = QVBoxLayout(self)
        lay.setSpacing(12)
        lay.setContentsMargins(16, 16, 16, 16)

        title = QLabel("Verfügbare FlexRadio-Geräte")
        title.setStyleSheet("font-size:15px;font-weight:bold;color:#1e293b;")
        lay.addWidget(title)

        hint = QLabel(
            "Sucht via VITA-49 UDP-Broadcast (Port 4992). SmartSDR muss aktiv sein."
        )
        hint.setStyleSheet("color:#64748b;font-size:12px;")
        hint.setWordWrap(True)
        lay.addWidget(hint)

        self.combo = QComboBox()
        self.combo.setMinimumHeight(34)
        self.combo.setStyleSheet(
            "QComboBox{border:1px solid #cbd5e1;border-radius:6px;"
            "padding:5px 10px;font-size:13px;}"
            "QComboBox::drop-down{border:none;}"
        )
        lay.addWidget(self.combo)

        self.status_lbl = QLabel("Bereit zur Suche.")
        self.status_lbl.setStyleSheet("color:#475569;font-size:12px;")
        lay.addWidget(self.status_lbl)

        manual = QGroupBox("Oder manuell eingeben")
        manual.setStyleSheet(
            "QGroupBox{font-size:12px;font-weight:bold;color:#475569;"
            "border:1px solid #e2e8f0;border-radius:6px;margin-top:10px;padding:10px;}"
        )
        ml = QHBoxLayout(manual)
        self.host_edit = QLineEdit()
        self.host_edit.setPlaceholderText("IP-Adresse (z.B. 192.168.1.100)")
        self.host_edit.setStyleSheet(EDIT)
        self.port_edit = QLineEdit("4992")
        self.port_edit.setMaximumWidth(65)
        self.port_edit.setStyleSheet(EDIT)
        ml.addWidget(QLabel("Host:")); ml.addWidget(self.host_edit)
        ml.addWidget(QLabel("Port:")); ml.addWidget(self.port_edit)
        lay.addWidget(manual)

        row = QHBoxLayout()
        self.scan_btn = QPushButton("🔍 Suchen")
        self.scan_btn.clicked.connect(self._scan)
        self.scan_btn.setStyleSheet(BTN_P % ("#3b82f6","#2563eb"))
        ok_btn = QPushButton("Verbinden")
        ok_btn.clicked.connect(self._connect)
        ok_btn.setStyleSheet(BTN_P % ("#22c55e","#16a34a"))
        cancel = QPushButton("Abbrechen")
        cancel.clicked.connect(self.reject)
        cancel.setStyleSheet(BTN_N)
        row.addWidget(self.scan_btn); row.addStretch()
        row.addWidget(cancel); row.addWidget(ok_btn)
        lay.addLayout(row)

    def _scan(self):
        self.scan_btn.setEnabled(False)
        self.scan_btn.setText("Suche läuft…")
        self.status_lbl.setText("Lausche auf VITA-49 Discovery-Pakete…")
        self.combo.clear(); self._radios = []
        QApplication.processEvents()
        threading.Thread(
            target=lambda: QTimer.singleShot(0, lambda: self._done(FlexDiscovery.discover(4.0))),
            daemon=True
        ).start()
        QTimer.singleShot(4500, self._scan_finished)

    def _scan_finished(self):
        self.scan_btn.setEnabled(True)
        self.scan_btn.setText("🔍 Suchen")

    def _done(self, radios):
        self._radios = radios
        self.combo.clear()
        if radios:
            for r in radios:
                label = (f"{r.get('nickname') or r.get('model','FlexRadio')}"
                         f"  –  {r.get('ip','?')}"
                         + (f"  (v{r['version']})" if 'version' in r else "")
                         + (f"  [{r['status']}]"   if 'status'  in r else "")
                         + (f"  S/N:{r['serial']}" if 'serial'  in r else ""))
                self.combo.addItem(label)
            self.status_lbl.setText(f"✓ {len(radios)} Gerät(e) gefunden.")
        else:
            self.combo.addItem("Keine Geräte gefunden")
            self.status_lbl.setText(
                "Kein FlexRadio entdeckt. SmartSDR aktiv? Bitte IP manuell eingeben."
            )
        self.scan_btn.setEnabled(True)
        self.scan_btn.setText("🔍 Suchen")

    def _connect(self):
        manual = self.host_edit.text().strip()
        if manual:
            try:   port = int(self.port_edit.text().strip() or "4992")
            except ValueError: port = 4992
            self.radio_selected.emit(manual, port)
            self.accept(); return
        idx = self.combo.currentIndex()
        if 0 <= idx < len(self._radios):
            r = self._radios[idx]
            if r.get("ip"):
                self.radio_selected.emit(r["ip"], int(r.get("port", 4992)))
                self.accept(); return
        QMessageBox.warning(self, "Kein Gerät",
                            "Bitte Gerät auswählen oder IP manuell eingeben.")


# ─── Main Window ──────────────────────────────────────────────────────────────

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.config   = load_config()
        self._flex    = None
        self._worker  = None
        self._conn    = False
        self.setWindowTitle("FlexRadio → Wavelog Bridge")
        self.setMinimumSize(740, 600)
        self._build_ui()
        self._build_tray()
        self._start_worker()
        if self.config.get("auto_connect") and self.config.get("flex_host"):
            QTimer.singleShot(600, self._auto_connect)

    # ── UI ────────────────────────────────────────────────────────────────────

    def _build_ui(self):
        self.setStyleSheet("""
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
        """)
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setSpacing(0); root.setContentsMargins(0,0,0,0)

        # Header
        hdr = QFrame()
        hdr.setStyleSheet("background:#0f172a;border:none;")
        hdr.setFixedHeight(64)
        hl = QHBoxLayout(hdr); hl.setContentsMargins(20,0,20,0)
        t = QLabel("⚡ FlexRadio → Wavelog Bridge")
        t.setStyleSheet("color:white;font-size:18px;font-weight:bold;")
        hl.addWidget(t); hl.addStretch()
        self.conn_badge = QLabel("● Getrennt")
        self.conn_badge.setStyleSheet("color:#64748b;font-size:13px;font-weight:bold;")
        hl.addWidget(self.conn_badge)
        root.addWidget(hdr)

        # Live status band
        sf = QFrame()
        sf.setStyleSheet("background:#1e293b;border:none;")
        sf.setFixedHeight(50)
        sl = QHBoxLayout(sf); sl.setContentsMargins(20,0,20,0); sl.setSpacing(28)
        self.freq_lbl = QLabel("– – –.– – – MHz")
        self.freq_lbl.setStyleSheet(
            "color:#38bdf8;font-size:22px;font-weight:bold;font-family:'Consolas';"
        )
        self.mode_lbl = QLabel("–")
        self.mode_lbl.setStyleSheet("color:#a78bfa;font-size:18px;font-weight:bold;")
        sl.addWidget(self.freq_lbl); sl.addWidget(self.mode_lbl); sl.addStretch()
        self.wl_badge = QLabel("Wavelog: –")
        self.wl_badge.setStyleSheet("color:#64748b;font-size:12px;")
        sl.addWidget(self.wl_badge)
        root.addWidget(sf)

        # Tabs
        tabs = QTabWidget()
        root.addWidget(tabs)

        # ── Tab Steuerung ─────────────────────────────────────────────────────
        ctrl = QWidget()
        cl   = QVBoxLayout(ctrl); cl.setContentsMargins(16,16,16,16); cl.setSpacing(12)

        fg = QGroupBox("FlexRadio Verbindung")
        ff = QFormLayout(fg); ff.setSpacing(10)
        host_txt = self.config.get("flex_host") or "Nicht konfiguriert"
        self.host_lbl = QLabel(host_txt)
        self.host_lbl.setStyleSheet("color:#475569;font-size:13px;")
        ff.addRow("Gerät:", self.host_lbl)
        br = QHBoxLayout()
        self.discover_btn = QPushButton("🔍 Gerät suchen")
        self.discover_btn.clicked.connect(self._open_discovery)
        self.discover_btn.setStyleSheet(BTN_P % ("#3b82f6","#2563eb"))
        self.connect_btn  = QPushButton("Verbinden")
        self.connect_btn.clicked.connect(self._toggle_conn)
        self.connect_btn.setStyleSheet(BTN_P % ("#22c55e","#16a34a"))
        br.addWidget(self.discover_btn); br.addWidget(self.connect_btn); br.addStretch()
        ff.addRow("", br)
        self.auto_cb = QCheckBox("Beim Start automatisch verbinden")
        self.auto_cb.setChecked(bool(self.config.get("auto_connect")))
        self.auto_cb.toggled.connect(lambda v: self._patch("auto_connect", v))
        ff.addRow("", self.auto_cb)
        cl.addWidget(fg)

        # Slice-Auswahl
        sg = QGroupBox("Slice-Auswahl")
        sg.setStyleSheet(
            "QGroupBox{border:1px solid #e2e8f0;border-radius:8px;"
            "margin-top:14px;padding:12px;background:white;}"
            "QGroupBox::title{subcontrol-origin:margin;left:12px;top:-7px;"
            "background:white;padding:0 6px;color:#475569;font-size:12px;font-weight:bold;}"
        )
        sl_lay = QVBoxLayout(sg)
        sl_lay.setSpacing(8)

        # Mode buttons: Auto-TX vs. Manuell
        mode_row = QHBoxLayout()
        self.slice_auto_btn   = QPushButton("🔄 Auto (TX-Slice)")
        self.slice_manual_btn = QPushButton("☑ Manuell wählen")
        self.slice_auto_btn.setCheckable(True)
        self.slice_manual_btn.setCheckable(True)
        self.slice_auto_btn.setChecked(True)
        for btn in (self.slice_auto_btn, self.slice_manual_btn):
            btn.setStyleSheet(
                "QPushButton{background:#f1f5f9;color:#475569;border-radius:6px;"
                "padding:6px 14px;font-size:13px;border:1px solid #cbd5e1;}"
                "QPushButton:checked{background:#3b82f6;color:white;border-color:#3b82f6;}"
                "QPushButton:hover:!checked{background:#e2e8f0;}"
            )
        self.slice_auto_btn.clicked.connect(self._set_slice_auto)
        self.slice_manual_btn.clicked.connect(self._set_slice_manual)
        mode_row.addWidget(self.slice_auto_btn)
        mode_row.addWidget(self.slice_manual_btn)
        mode_row.addStretch()
        sl_lay.addLayout(mode_row)

        # Slice-Tabelle (ComboBox + Info-Label)
        combo_row = QHBoxLayout()
        self.slice_combo = QComboBox()
        self.slice_combo.setMinimumHeight(32)
        self.slice_combo.setEnabled(False)
        self.slice_combo.setStyleSheet(
            "QComboBox{border:1px solid #cbd5e1;border-radius:6px;"
            "padding:4px 10px;font-size:13px;background:white;}"
            "QComboBox:disabled{background:#f1f5f9;color:#94a3b8;}"
            "QComboBox::drop-down{border:none;}"
        )
        self.slice_combo.currentIndexChanged.connect(self._on_slice_combo_changed)
        combo_row.addWidget(QLabel("Slice:"))
        combo_row.addWidget(self.slice_combo, stretch=1)
        sl_lay.addLayout(combo_row)

        # Status-Zeile: zeigt welcher Slice gerade aktiv ist
        self.slice_status_lbl = QLabel("Kein FlexRadio verbunden")
        self.slice_status_lbl.setStyleSheet(
            "color:#64748b;font-size:12px;padding:2px 0;"
        )
        sl_lay.addWidget(self.slice_status_lbl)
        cl.addWidget(sg)

        lg = QGroupBox("Protokoll")
        lv = QVBoxLayout(lg)
        self.log_out = QTextEdit()
        self.log_out.setReadOnly(True)
        self.log_out.setMinimumHeight(180)
        lv.addWidget(self.log_out)
        clr = QPushButton("Löschen"); clr.setMaximumWidth(80)
        clr.setStyleSheet(BTN_N); clr.clicked.connect(self.log_out.clear)
        lv.addWidget(clr, alignment=Qt.AlignmentFlag.AlignRight)
        cl.addWidget(lg)
        tabs.addTab(ctrl, "Steuerung")

        # ── Tab Konfiguration ─────────────────────────────────────────────────
        cw = QWidget()
        cv = QVBoxLayout(cw); cv.setContentsMargins(16,16,16,16); cv.setSpacing(12)

        wg = QGroupBox("Wavelog Einstellungen")
        wf = QFormLayout(wg); wf.setSpacing(10)

        self.url_edit = QLineEdit(self.config.get("wavelog_url",""))
        self.url_edit.setPlaceholderText("https://log.meineinstanz.de")
        wf.addRow("Wavelog URL:", self.url_edit)

        # API-Schlüssel mit Sichtbarkeits-Toggle
        key_row = QHBoxLayout()
        self.key_edit = QLineEdit(self.config.get("wavelog_api_key",""))
        self.key_edit.setPlaceholderText("API-Schlüssel (Lesen + Schreiben)")
        self.key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.key_edit.setStyleSheet(EDIT)
        self.show_key_btn = QPushButton("👁")
        self.show_key_btn.setCheckable(True)
        self.show_key_btn.setFixedWidth(34)
        self.show_key_btn.setToolTip("Schlüssel anzeigen / verbergen")
        self.show_key_btn.setStyleSheet(
            "QPushButton{border:1px solid #cbd5e1;border-radius:6px;"
            "background:white;font-size:14px;padding:2px;}"
            "QPushButton:checked{background:#dbeafe;border-color:#3b82f6;}"
            "QPushButton:hover{background:#f1f5f9;}"
        )
        self.show_key_btn.toggled.connect(self._toggle_key_visibility)
        key_row.addWidget(self.key_edit)
        key_row.addWidget(self.show_key_btn)
        wf.addRow("API-Schlüssel:", key_row)

        self.rname = QLineEdit(self.config.get("radio_name","FlexRadio 6600"))
        wf.addRow("Funkgerät-Name:", self.rname)

        self.interval = QSpinBox()
        self.interval.setRange(1, 120)
        self.interval.setValue(self.config.get("update_interval", 5))
        self.interval.setSuffix(" Sekunden")
        wf.addRow("Update-Intervall:", self.interval)

        # Wavelog test – INDEPENDENT of FlexRadio connection
        test_btn = QPushButton("🔗 Wavelog-Verbindung testen")
        test_btn.clicked.connect(self._test_wavelog)
        test_btn.setStyleSheet(BTN_P % ("#8b5cf6","#7c3aed"))
        wf.addRow("", test_btn)
        cv.addWidget(wg)

        save_btn = QPushButton("💾 Einstellungen speichern")
        save_btn.clicked.connect(self._save_config)
        save_btn.setStyleSheet(
            "QPushButton{background:#0f172a;color:white;font-weight:bold;"
            "font-size:14px;padding:10px;border-radius:6px;}"
            "QPushButton:hover{background:#1e293b;}"
        )
        cv.addWidget(save_btn); cv.addStretch()
        tabs.addTab(cw, "Konfiguration")

    def _toggle_key_visibility(self, visible: bool):
        mode = QLineEdit.EchoMode.Normal if visible else QLineEdit.EchoMode.Password
        self.key_edit.setEchoMode(mode)
        self.show_key_btn.setText("🙈" if visible else "👁")

    def _build_tray(self):
        self._tray = QSystemTrayIcon(make_tray_icon(False), self)
        self._tray.setToolTip("FlexRadio → Wavelog Bridge")
        menu = QMenu()
        show_a = QAction("Fenster anzeigen", self); show_a.triggered.connect(self.show)
        menu.addAction(show_a); menu.addSeparator()
        self._tray_status = QAction("Status: Getrennt", self)
        self._tray_status.setEnabled(False)
        menu.addAction(self._tray_status); menu.addSeparator()
        quit_a = QAction("Beenden", self); quit_a.triggered.connect(QApplication.quit)
        menu.addAction(quit_a)
        self._tray.setContextMenu(menu)
        self._tray.activated.connect(
            lambda r: (self.show(), self.raise_(), self.activateWindow())
            if r == QSystemTrayIcon.ActivationReason.DoubleClick else None
        )
        self._tray.show()

    def _start_worker(self):
        self._worker = UpdateWorker(self.config)
        self._worker.log_message.connect(self._log)
        self._worker.wavelog_status.connect(self._on_wl_status)
        self._worker.start()

    # ── Actions ───────────────────────────────────────────────────────────────

    def _open_discovery(self):
        dlg = DiscoveryDialog(self)
        dlg.radio_selected.connect(self._on_radio_selected)
        dlg.exec()

    def _on_radio_selected(self, host: str, port: int):
        self.config["flex_host"] = host
        self.config["flex_port"] = port
        self.host_lbl.setText(f"{host}:{port}")
        save_config(self.config)
        self._log(f"Gerät ausgewählt: {host}:{port}")
        self._connect_to(host, port)

    def _toggle_conn(self):
        if self._conn:
            self._disconnect()
        else:
            host = self.config.get("flex_host","")
            if not host:
                QMessageBox.warning(self,"Kein Gerät",
                    "Bitte zuerst ein FlexRadio suchen und auswählen.")
                return
            self._connect_to(host, self.config.get("flex_port", 4992))

    def _auto_connect(self):
        host = self.config.get("flex_host","")
        if host:
            self._connect_to(host, self.config.get("flex_port", 4992))

    def _connect_to(self, host: str, port: int):
        if self._flex and self._flex.isRunning():
            self._flex.stop(); self._flex.wait(2000)
        self._log(f"Verbinde mit {host}:{port}…")
        self._flex = FlexRadioClient(host, port)
        self._flex.status_changed.connect(self._on_flex_status)
        self._flex.radio_data.connect(self._on_radio_data)
        self._flex.log_message.connect(self._log)
        self._flex.slices_changed.connect(self._on_slices_changed)
        self._flex.start()
        self.connect_btn.setText("Trennen")
        self.connect_btn.setStyleSheet(BTN_P % ("#ef4444","#dc2626"))

    def _disconnect(self):
        if self._flex: self._flex.stop()
        self._conn = False
        self.connect_btn.setText("Verbinden")
        self.connect_btn.setStyleSheet(BTN_P % ("#22c55e","#16a34a"))
        self._reset_slice_ui()

    # ── Slots ─────────────────────────────────────────────────────────────────

    @pyqtSlot(str)
    def _on_flex_status(self, status: str):
        if status == "connected":
            self._conn = True
            self.conn_badge.setText("● Verbunden")
            self.conn_badge.setStyleSheet("color:#22c55e;font-size:13px;font-weight:bold;")
            self._tray.setIcon(make_tray_icon(True))
            self._tray_status.setText("Status: Verbunden")
        elif status == "disconnected":
            self._conn = False
            self.conn_badge.setText("● Getrennt")
            self.conn_badge.setStyleSheet("color:#64748b;font-size:13px;font-weight:bold;")
            self._tray.setIcon(make_tray_icon(False))
            self._tray_status.setText("Status: Getrennt")
            self.connect_btn.setText("Verbinden")
            self.connect_btn.setStyleSheet(BTN_P % ("#22c55e","#16a34a"))
        elif status.startswith("error"):
            self._conn = False
            self.conn_badge.setText("● Fehler")
            self.conn_badge.setStyleSheet("color:#ef4444;font-size:13px;font-weight:bold;")
            self._tray.setIcon(make_tray_icon(False))
            self._log(f"✗ FlexRadio: {status}")

    @pyqtSlot(dict)
    def _on_radio_data(self, data: dict):
        freq_mhz = data["frequency"] / 1_000_000
        self.freq_lbl.setText(f"{freq_mhz:.3f} MHz")
        self.mode_lbl.setText(data["mode"])
        self._tray.setToolTip(f"FlexRadio → Wavelog\n{freq_mhz:.3f} MHz / {data['mode']}")
        if self._worker:
            self._worker.update_radio_data(data)

    @pyqtSlot(bool, str)
    def _on_wl_status(self, ok: bool, msg: str):
        if ok:
            self.wl_badge.setText(f"✓ Wavelog: {msg}")
            self.wl_badge.setStyleSheet("color:#22c55e;font-size:12px;")
        else:
            self.wl_badge.setText(f"✗ Wavelog: {msg}")
            self.wl_badge.setStyleSheet("color:#ef4444;font-size:12px;")

    @pyqtSlot(str)
    def _log(self, msg: str):
        ts = datetime.datetime.now().strftime("%H:%M:%S")
        color = "#22c55e" if msg.startswith("✓") else \
                "#ef4444" if msg.startswith("✗") else \
                "#f59e0b" if msg.startswith("⚠") else "#94a3b8"
        self.log_out.append(
            f"<span style='color:#475569'>[{ts}]</span> "
            f"<span style='color:{color}'>{msg}</span>"
        )
        log.info(msg)

    def _log_detail(self, lines: list, ok: bool):
        """Append indented detail lines in dimmer color."""
        for line in lines:
            ts = datetime.datetime.now().strftime("%H:%M:%S")
            color = "#22c55e" if ok else "#f87171"
            self.log_out.append(
                f"<span style='color:#334155;font-size:11px'>[{ts}]"
                f"<span style='color:{color}'>{line}</span></span>"
            )

    # ── Slice selection ───────────────────────────────────────────────────────

    def _set_slice_auto(self):
        self.slice_auto_btn.setChecked(True)
        self.slice_manual_btn.setChecked(False)
        self.slice_combo.setEnabled(False)
        if self._flex:
            self._flex.selected_slice = None
        self._log("Slice-Modus: Auto (TX-Slice wird automatisch verfolgt)")
        self._refresh_slice_status()

    def _set_slice_manual(self):
        self.slice_auto_btn.setChecked(False)
        self.slice_manual_btn.setChecked(True)
        self.slice_combo.setEnabled(True)
        self._log("Slice-Modus: Manuell – bitte Slice auswählen")
        self._on_slice_combo_changed(self.slice_combo.currentIndex())

    def _on_slice_combo_changed(self, index: int):
        if not self.slice_manual_btn.isChecked():
            return
        if index < 0 or self.slice_combo.count() == 0:
            return
        data = self.slice_combo.itemData(index)
        if data is None:
            return
        slice_idx = str(data)
        if self._flex:
            self._flex.selected_slice = slice_idx
        self._log(f"Manueller Slice gewählt: Slice {slice_idx}")
        self._refresh_slice_status()

    @pyqtSlot(dict)
    def _on_slices_changed(self, slices: dict):
        """Called when FlexRadio reports a change in slice inventory or TX state."""
        is_manual = self.slice_manual_btn.isChecked()
        current_data = self.slice_combo.currentData()

        # Rebuild combo without triggering _on_slice_combo_changed prematurely
        self.slice_combo.blockSignals(True)
        self.slice_combo.clear()

        for idx in sorted(slices.keys(), key=lambda x: int(x) if x.isdigit() else 0):
            s        = slices[idx]
            freq_mhz = s.get("rf_frequency", 0.0)
            mode     = s.get("mode", "?")
            is_tx    = s.get("tx", False)
            tx_tag   = "  [TX]" if is_tx else ""
            label    = f"Slice {idx}:  {freq_mhz:.3f} MHz  {mode}{tx_tag}"
            self.slice_combo.addItem(label, userData=idx)

        # Restore previous selection if still present
        if current_data is not None:
            for i in range(self.slice_combo.count()):
                if self.slice_combo.itemData(i) == current_data:
                    self.slice_combo.setCurrentIndex(i)
                    break

        self.slice_combo.blockSignals(False)
        self._refresh_slice_status()

    def _refresh_slice_status(self):
        """Update the status label below the slice combo."""
        if not self._flex or not self._flex.isRunning():
            self.slice_status_lbl.setText("Kein FlexRadio verbunden")
            return

        slices = self._flex._slices
        if not slices:
            self.slice_status_lbl.setText("Keine Slices vorhanden")
            return

        active_idx = self._flex._active_slice_idx()
        if active_idx is None:
            self.slice_status_lbl.setText("⚠ Kein aktiver Slice gefunden")
            return

        s        = slices.get(active_idx, {})
        freq_mhz = s.get("rf_frequency", 0.0)
        mode     = s.get("mode", "?")
        is_tx    = s.get("tx", False)
        mode_str = "Auto (TX)" if (self.slice_auto_btn.isChecked() and is_tx) else                    "Auto (Fallback)" if self.slice_auto_btn.isChecked() else "Manuell"
        self.slice_status_lbl.setText(
            f"Aktiver Slice: {active_idx}  |  {freq_mhz:.3f} MHz  {mode}  |  Modus: {mode_str}"
        )

    def _reset_slice_ui(self):
        self.slice_combo.clear()
        self.slice_status_lbl.setText("Kein FlexRadio verbunden")

    # ── Config ────────────────────────────────────────────────────────────────

    def _patch(self, key, value):
        self.config[key] = value
        save_config(self.config)

    def _save_config(self):
        self.config["wavelog_url"]     = self.url_edit.text().strip().rstrip("/")
        self.config["wavelog_api_key"] = self.key_edit.text().strip()
        self.config["radio_name"]      = self.rname.text().strip()
        self.config["update_interval"] = self.interval.value()
        save_config(self.config)
        if self._worker:
            self._worker.set_config(self.config)
        self._log("✓ Konfiguration gespeichert")
        QMessageBox.information(self, "Gespeichert", "Konfiguration wurde gespeichert.")

    def _test_wavelog(self):
        """
        Tests Wavelog connectivity INDEPENDENTLY of any FlexRadio connection.
        Uses a proper QThread + signal so the result always arrives in the GUI thread.
        """
        url = self.url_edit.text().strip().rstrip("/")
        key = self.key_edit.text().strip()
        if not url:
            QMessageBox.warning(self, "Fehlende Eingabe", "Bitte Wavelog URL eingeben.")
            return
        if not key:
            QMessageBox.warning(self, "Fehlende Eingabe", "Bitte API-Schlüssel eingeben.")
            return

        self._log(f"Teste Wavelog-Verbindung → {url} …")

        # Keep a reference so the thread isn't garbage-collected before finishing
        self._tester = WavelogTester(url, key)
        self._tester.result_ready.connect(self._show_test)
        self._tester.start()

    def _show_test(self, ok: bool, summary: str, detail: list):
        if ok:
            self._log(f"✓ {summary}")
        else:
            self._log(f"✗ Wavelog: {summary}")
        self._log_detail(detail, ok)
        if ok:
            QMessageBox.information(self, "Verbindung OK",
                f"✓ {summary}\n\nDetails im Protokoll-Tab.")
        else:
            QMessageBox.critical(self, "Verbindungsfehler",
                f"✗ {summary}\n\nDetails im Protokoll-Tab (Steuerung).")

    # ── Window events ─────────────────────────────────────────────────────────

    def closeEvent(self, event):
        event.ignore()
        self.hide()
        self._tray.showMessage(
            "FlexRadio → Wavelog",
            "Läuft im Hintergrund. Doppelklick zum Öffnen.",
            QSystemTrayIcon.MessageIcon.Information, 3000,
        )


# ─── Entry Point ──────────────────────────────────────────────────────────────

def main():
    app = QApplication(sys.argv)
    app.setApplicationName("FlexWavelogBridge")
    app.setQuitOnLastWindowClosed(False)
    if not QSystemTrayIcon.isSystemTrayAvailable():
        QMessageBox.critical(None, "System Tray", "Kein System-Tray gefunden!")
        sys.exit(1)
    win = MainWindow()
    win.show()
    sys.exit(app.exec())

if __name__ == "__main__":
    main()
