# CLAUDE.md – Technische Dokumentation

FlexRadio → Wavelog Bridge  
Entwicklerdokumentation für Claude und zukünftige Maintainer.

---

## Projektübersicht

Dieses Programm ist eine Windows-Desktop-Applikation (Python / PyQt6), die:

1. Ein FlexRadio-Gerät per SmartSDR-TCP-API abonniert
2. Frequenz und Betriebsart des aktiven TX-Slice empfängt
3. Diese Daten periodisch an die Wavelog-REST-API weiterleitet
4. Als System-Tray-Anwendung dauerhaft im Hintergrund läuft

---

## Architektur

### Thread-Modell

Das Programm nutzt vier Threads:

```
GUI-Thread (Qt main)
│
├── UpdateWorker (QThread)
│     Sendet periodisch radio_data an Wavelog-API
│     Kommunikation: pyqtSignal(str) / pyqtSignal(bool, str)
│
├── FlexRadioClient (QThread)
│     TCP-Verbindung zu SmartSDR auf Port 4992
│     Kommunikation: pyqtSignal(str) für alle Signale
│     – status_changed(str)
│     – radio_data(str)       ← JSON-String, kein pyqtSignal(dict)!
│     – log_message(str)
│     – slices_changed(str)   ← JSON-String
│
└── WavelogTester (QThread)
      Einmaliger Verbindungstest, gibt Ergebnis via Signal zurück
      – result_ready(bool, str, list)
```

### Kritische Design-Entscheidungen

**Warum JSON-Strings statt `pyqtSignal(dict)`?**

PyQt6 kann `dict`-Objekte mit verschachtelten Werten beim cross-thread Signal-Delivery nicht zuverlässig serialisieren. Das führt zu stillen Exceptions, die Qt verschluckt. Alle komplexen Daten werden daher als `json.dumps(...)` gesendet und im Slot mit `json.loads(...)` wieder geparst.

**Warum kein `logging.StreamHandler(sys.stdout)`?**

Unter `pythonw.exe` (das für den fensterloseen Start verwendet wird) ist `sys.stdout = None`. Ein StreamHandler auf `None` wirft `AttributeError` beim ersten Log-Aufruf. Da dieser Fehler innerhalb eines Qt-Signal-Slots auftritt, bricht Qt die gesamte Signal-Delivery-Kette ab – ohne jede Fehlermeldung. Der StreamHandler wird daher nur angelegt wenn `sys.stdout is not None`.

**Warum atomarer Socket-Tausch in `stop()` und `finally`?**

```python
sock, self._sock = self._sock, None   # atomarer Tausch
```

`stop()` wird aus dem GUI-Thread aufgerufen, `run()` / `finally` laufen im Worker-Thread. Ohne atomaren Tausch würde `finally` den Socket ein zweites Mal schließen → `WinError 10038 (WSAENOTSOCK)`. Mit dem Tausch besitzt immer nur einer den Socket.

---

## Klassen-Referenz

### `FlexDiscovery`

**Zweck:** Empfängt VITA-49 UDP-Broadcast-Pakete vom FlexRadio.

**Protokoll (FlexLib: `Discovery.cs`, `VitaDiscovery.cs`, `VitaFlex.cs`):**

Das FlexRadio sendet binäre VITA-49-Pakete auf UDP-Port 4992:

```
Offset  Bytes  Inhalt
  0       4    VITA-Header (big-endian uint32)
                  bits 31-28: pkt_type  → muss 0x7 sein (ExtDataWithStream)
                  bit  27:    C-Flag    → muss 1 sein (Class-ID present)
                  bit  26:    T-Flag    (Trailer present)
                  bits 25-24: tsi       (Timestamp Integer type)
                  bits 23-22: tsf       (Timestamp Fractional type)
                  bits 15-0:  packet_size (in 32-bit-Worten)
  4       4    Stream-ID (uint32, bei ExtDataWithStream immer present)
  8       4    OUI-Word  (bits 23-0 = OUI, muss 0x001C2D sein)
 12       4    ClassCode-Word (bits 15-0 = PacketClassCode, muss 0xFFFF sein)
 16+      N    UTF-8-Payload: space-separated key=value Paare
 end-4    4    Trailer (optional, nur wenn T-Flag=1)
```

Relevante Payload-Keys: `ip`, `model`, `nickname`, `version`, `status`, `serial`, `callsign`, `port`, `available_slices`, `max_slices`

**Methoden:**
- `discover(timeout=4.0) → list[dict]` – blockiert bis `timeout` Sekunden, gibt alle gefundenen Radios zurück

---

### `FlexRadioClient(QThread)`

**Zweck:** TCP-Verbindung zum SmartSDR Command API auf Port 4992.

**Protokoll (FlexLib: `TcpCommandCommunication.cs`, `Radio.cs`, `Slice.cs`):**

Alle Nachrichten sind UTF-8, zeilengetrennt (`\n`).

Kommandos vom Client:
```
C<seq>|<command>\n
```

Eingehende Nachrichtentypen:
```
H<handle>|...           → Client-Handle zugewiesen (nach Connect)
V<version>              → Protokollversion
S<handle>|<topic> ...   → Status-Update
R<seq>|<code>|<msg>     → Antwort auf Kommando (wird ignoriert)
M<uptime>|<msg>         → Radio-Meldung (wird geloggt)
```

Initialisierungssequenz (wird direkt nach Connect gesendet):
```
client program FlexWavelogBridge
client start_persistence off
sub client all
sub tx all
sub atu all
sub slice all
sub gps all
```

**Slice-Status-Format (`Slice.cs::StatusUpdate`):**
```
S<handle>|slice <idx> <k>=<v> <k>=<v> ...
```

Relevante Keys:
| Key | Typ | Bedeutung |
|---|---|---|
| `rf_frequency` | double | Frequenz in MHz, z.B. `14.225000` |
| `mode` | string | Demodulationsmodus (USB/LSB/AM/FM/CW/RTTY/DIGU/DIGL/...) |
| `tx` | 0 oder 1 | Dieser Slice ist der aktive TX-Slice |
| `in_use` | 0 oder 1 | Slice existiert (0 = entfernt) |

**TX-Slice-Logik:**
- Jeder `tx=1`-Update setzt alle anderen Slices auf `tx=False`
- `_active_slice_idx()` gibt zurück:
  - `selected_slice` wenn manuell gesetzt und in `_slices` vorhanden
  - Den Slice mit `tx=True` im Auto-Modus
  - Den numerisch niedrigsten Slice als Fallback

**Signals:**
| Signal | Typ | Inhalt |
|---|---|---|
| `status_changed` | `str` | `"connected"` / `"disconnected"` / `"error: <msg>"` |
| `radio_data` | `str` | JSON: `{frequency, mode, raw_mode, freq_mhz, slice, tx}` |
| `log_message` | `str` | Klartext für GUI-Log |
| `slices_changed` | `str` | JSON: `{idx: {rf_frequency, mode, tx}, ...}` |

**Fehlerbehandlung:**
- `ConnectionRefusedError` → freundliche Meldung, kein Stack-Trace
- `socket.timeout` (beim Connect) → freundliche Meldung
- `OSError` mit WinError 10038/10054/10053 oder `_run=False` → erwartet, wird ignoriert
- Andere `OSError` → als echter Fehler gemeldet

---

### `WavelogClient`

**Zweck:** REST-Client für die Wavelog-API.

**Relevante Endpunkte:**

`POST /api/radio` – Frequenz und Mode übertragen:
```json
{
  "key":       "API_KEY",
  "radio":     "FlexRadio 6600",
  "frequency": 14225000,
  "mode":      "SSB",
  "timestamp": "2025/01/15 14:30"
}
```

`POST /api/version` – Verbindungstest:
```json
{ "key": "API_KEY" }
```

**Methoden:**
- `send_radio_data(frequency: int, mode: str) → (bool, str)` – sendet Daten, gibt `(ok, message)` zurück
- `test_connection() → (bool, str, list)` – testet `/api/version`, gibt `(ok, summary, detail_lines)` zurück

---

### `WavelogTester(QThread)`

**Zweck:** Führt `WavelogClient.test_connection()` im Hintergrund aus und liefert das Ergebnis über ein Qt-Signal zurück. Löst das Problem dass `QTimer.singleShot()` aus einem `daemon`-Thread nicht zuverlässig funktioniert.

**Signal:** `result_ready(bool, str, list)` – `(ok, summary, detail_lines)`

---

### `UpdateWorker(QThread)`

**Zweck:** Sendet periodisch die letzte bekannte Frequenz/Mode an Wavelog.

**Funktionsweise:**
1. Schläft `update_interval` Sekunden
2. Prüft ob `_current_data` gesetzt ist
3. Prüft ob URL und API-Key konfiguriert sind
4. Sendet via `WavelogClient.send_radio_data()`

`update_radio_data(data: dict)` – thread-safe Update der zu sendenden Daten (über `threading.Lock`)

---

### `MainWindow(QMainWindow)`

**Tabs:**

**Steuerung:**
- FlexRadio-Verbindungsgruppe (Discovery-Button, Verbinden-Button, Auto-Connect-Checkbox)
- Slice-Auswahl (Auto/Manuell, Combo mit Live-Update, Statuszeile)
- Protokollfenster (HTML-formatiert, farbcodiert)

**Konfiguration:**
- Wavelog URL, API-Schlüssel (mit Sichtbarkeits-Toggle), Funkgerät-Name, Intervall
- Verbindungstest-Button (unabhängig von FlexRadio-Status)
- Speichern-Button

**Wichtige Slots:**
| Slot | Signal-Quelle | Funktion |
|---|---|---|
| `_on_flex_status(str)` | `FlexRadioClient.status_changed` | Badge, Tray-Icon, Button-Farbe |
| `_on_radio_data_json(str)` | `FlexRadioClient.radio_data` | Frequenz/Mode anzeigen, Worker updaten |
| `_on_slices_changed_json(str)` | `FlexRadioClient.slices_changed` | Combo neu befüllen |
| `_append_log(str)` | diverse `log_message` | HTML ins Protokollfenster |
| `_show_test_result(bool,str,list)` | `WavelogTester.result_ready` | Ergebnis + Details anzeigen |

---

## Konfigurationsdatei

Gespeichert unter `%APPDATA%\FlexWavelogBridge\config.json`:

```json
{
  "wavelog_url":     "https://log.example.com",
  "wavelog_api_key": "abc123...",
  "radio_name":      "FlexRadio 6600",
  "update_interval": 5,
  "flex_host":       "192.168.1.100",
  "flex_port":       4992,
  "auto_connect":    false
}
```

---

## Bekannte Einschränkungen

- **Nur Windows:** `pythonw.exe`-Erkennung und WinError-Codes sind Windows-spezifisch. Unter Linux/macOS würde `pythonw` fehlen und `errno.EBADF` (9) statt WinError 10038 auftreten (im `_CLOSED_ERRNOS`-Set bereits enthalten).
- **UDP-Port 4992:** Wenn SmartSDR bereits auf dem gleichen Rechner läuft, kann der Discovery-Port belegt sein. In diesem Fall muss die IP manuell eingegeben werden. `SO_REUSEADDR` sollte helfen, ist aber nicht auf allen Windows-Versionen ausreichend.
- **Nur ein FlexRadio:** Es kann immer nur eine TCP-Verbindung gleichzeitig aktiv sein. Mehrere Radios werden bei der Discovery erkannt, aber nur eines kann aktiv sein.
- **Kein Reconnect:** Bei Verbindungsverlust muss manuell neu verbunden werden. Auto-Reconnect ist nicht implementiert.

---

## Abhängigkeiten

| Paket | Version | Zweck |
|---|---|---|
| `PyQt6` | >=6.4.0 | GUI, Threading, Signals |
| `requests` | >=2.28.0 | HTTP-Client für Wavelog-API |

Python-Stdlib (keine Installation nötig): `socket`, `struct`, `threading`, `json`, `logging`, `datetime`, `time`, `pathlib`, `os`, `sys`

---

## Entwicklungshinweise

### Neuen Modus hinzufügen

In `FlexRadioClient.MODE_MAP` eintragen:
```python
MODE_MAP = {
    ...
    "NEUER_MODE": "ADIF_EQUIVALENT",
}
```

### Weitere Slice-Keys auswerten

In `FlexRadioClient._update_slice()` im `for kv in update.split()`-Block ergänzen:
```python
elif k == "neuer_key":
    s["neuer_key"] = val
    changed = True
```

### Weitere Wavelog-API-Felder senden

In `WavelogClient.send_radio_data()` das `payload`-Dict erweitern, z.B. für Sendeleistung:
```python
payload = {
    ...
    "power": s.get("rf_power", ""),
}
```

### Logging-Level anpassen

In `flex_wavelog_bridge.py` Zeile ~30:
```python
logging.basicConfig(level=logging.DEBUG, ...)  # DEBUG, INFO, WARNING
```

---

## Changelog

### Version aktuell (FlexLib 4.1.5)

- Discovery: Binäres VITA-49-Protokoll korrekt implementiert (statt Klartext-UDP)
- Frequenz-Key: `rf_frequency` (lowercase, korrekt per Slice.cs)
- TX-Slice-Tracking: `tx=0/1` aus Slice-Status-Updates
- Manuelle Slice-Auswahl: unabhängig vom TX-Status
- Alle Signale als JSON-String (`pyqtSignal(str)`) statt `pyqtSignal(dict)`
- `sys.stdout`-Guard für `pythonw.exe`
- Atomarer Socket-Tausch verhindert WinError 10038
- Wavelog-Test unabhängig von FlexRadio-Verbindung (eigener `QThread`)
- TCP-Init-Sequenz vollständig (inkl. `client program`, `client start_persistence off`)
