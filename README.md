# FlexRadio → Wavelog Bridge

Verbindet ein **FlexRadio 6600** mit **Wavelog** und überträgt Frequenz und Betriebsart automatisch.  
Implementiert auf Basis der **FlexLib API v4.1.5** (FlexLib_API_v4.1.5.39794).

---

## Funktionen

- **VITA-49 Discovery** – findet FlexRadio-Geräte via binärem UDP-Broadcast (Port 4992)
- **SmartSDR TCP API** – verbindet sich auf Port 4992, empfängt Slice-Status-Updates
- **Echtzeit-Übertragung** – sendet Frequenz & Betriebsart an Wavelog (`POST /api/radio`)
- **System-Tray-Betrieb** – läuft im Hintergrund, Doppelklick öffnet das Fenster
- **Konfigurierbares Intervall** – Standard 5 Sekunden, einstellbar 1–120 s
- **Wavelog-Verbindungstest** – prüft API-Schlüssel und Erreichbarkeit

---

## Voraussetzungen

- Python 3.11+ (64-bit, Windows)
- FlexRadio 6600 mit SmartSDR (lokales Netzwerk)
- Wavelog-Instanz mit aktivem API-Schlüssel (Lesen + Schreiben)

---

## Installation & Start

### Option 1: `start.bat` (empfohlen)
Doppelklick auf `start.bat` – richtet automatisch virtuelle Umgebung ein.

### Option 2: Manuell
```bat
python -m venv venv
venv\Scripts\pip install -r requirements.txt
venv\Scripts\pythonw flex_wavelog_bridge.py
```

---

## Protokoll-Details (FlexLib API v4.1.5)

### Discovery (Discovery.cs + VitaDiscovery.cs)
Das FlexRadio sendet **binäre VITA-49 UDP-Pakete** auf Port 4992:

| Offset | Bytes | Inhalt |
|--------|-------|--------|
| 0      | 4     | VITA-Header (pkt_type=0x7, C-Flag=1) |
| 4      | 4     | Stream-ID |
| 8      | 4     | OUI-Word (bits 23–0 = `0x001C2D`) |
| 12     | 4     | ClassCode-Word (bits 15–0 = `0xFFFF`) |
| 16+    | N     | UTF-8 Payload: `key=value`-Paare, leerzeichen-getrennt |

Relevante Payload-Keys: `ip`, `model`, `nickname`, `version`, `status`, `serial`, `port`

### TCP-Verbindung (TcpCommandCommunication.cs + Radio.cs)
- Port: **4992**
- Framing: Zeilen, abgeschlossen mit `\n`
- Kommandos senden: `C<seq>|<command>\n`

**Initialisierungs-Sequenz** (Radio.cs ~Z. 1914–1965):
```
client program FlexWavelogBridge
client start_persistence off
sub client all
sub tx all
sub atu all
sub slice all
sub gps all
```

**Eingehende Nachrichten:**
- `H<handle>|...`  → Client-Handle zugewiesen
- `V<version>`     → Protokollversion
- `S<handle>|<topic> [<idx>] <k>=<v> ...` → Status-Update
- `R<seq>|<code>|<msg>` → Antwort auf Kommando

### Slice-Status (Slice.cs::StatusUpdate)
Format: `S<handle>|slice <idx> <k>=<v> ...`

| Key | Bedeutung |
|-----|-----------|
| `rf_frequency` | Frequenz in MHz (double, z.B. `14.225000`) |
| `mode` | Betriebsart: `USB`, `LSB`, `AM`, `SAM`, `FM`, `NFM`, `DFM`, `CW`, `RTTY`, `DIGU`, `DIGL` |
| `in_use` | `0` = Slice entfernt, `1` = aktiv |
| `active` | Aktuell aktiver Slice |

### Modus-Mapping → ADIF/Wavelog

| FlexRadio | Wavelog |
|-----------|---------|
| USB / LSB | SSB |
| AM / SAM  | AM  |
| FM / NFM / DFM | FM |
| CW        | CW  |
| RTTY      | RTTY |
| DIGU / DIGL / FDV | DIGI |

---

## Wavelog API

Endpunkt: `POST /api/radio` (Standard Radio API Call)

```json
{
  "key":       "IHR_API_SCHLUESSEL",
  "radio":     "FlexRadio 6600",
  "frequency": 14225000,
  "mode":      "SSB",
  "timestamp": "2025/01/15 14:30"
}
```

---

## Autostart (optional)

1. `Win+R` → `shell:startup`
2. Verknüpfung zu `start.bat` im Autostart-Ordner ablegen
3. In der App: **"Beim Start automatisch verbinden"** aktivieren

---

## Logdatei

`%APPDATA%\FlexWavelogBridge\bridge.log`

---

## Fehlerbehebung

| Problem | Lösung |
|---------|--------|
| Discovery findet kein Gerät | SmartSDR läuft? Firewall gibt UDP/4992 frei? IP manuell eingeben |
| Port 4992 belegt | SmartSDR oder anderes Programm nutzt Port – direkte IP-Eingabe verwenden |
| Wavelog-Fehler 401 | API-Schlüssel ungültig oder nur Lese-Berechtigung |
| Modus wird falsch übertragen | Raw-Modus im Protokollfenster prüfen |
