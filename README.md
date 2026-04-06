# FlexRadio → Wavelog Bridge

Verbindet ein **FlexRadio 6600** (und kompatible Modelle) mit **Wavelog** und überträgt Frequenz und Betriebsart automatisch und in Echtzeit.

Implementiert auf Basis der **FlexLib API v4.1.5** (FlexLib_API_v4.1.5.39794).

---

## Funktionen

- **Automatische Geräteerkennung** via VITA-49 UDP-Broadcast (Port 4992)
- **SmartSDR TCP-API** – vollständige Implementierung des Kommando-Protokolls
- **TX-Slice-Verfolgung** – sendet immer die Daten des aktiven Sende-Slice
- **Manuelle Slice-Auswahl** – feste Auswahl unabhängig vom TX-Status
- **Echtzeit-Übertragung** an Wavelog `POST /api/radio`, konfigurierbares Intervall (1–120 s)
- **System-Tray-Betrieb** – läuft unsichtbar im Hintergrund, Doppelklick öffnet das Fenster
- **Wavelog-Verbindungstest** – unabhängig vom FlexRadio, mit detaillierter Diagnose
- **Persistente Konfiguration** unter `%APPDATA%\FlexWavelogBridge\config.json`
- **Detailliertes Logging** in `%APPDATA%\FlexWavelogBridge\bridge.log`

---

## Voraussetzungen

| Komponente | Anforderung |
|---|---|
| Betriebssystem | Windows 10/11 (64-bit) |
| Python | 3.11 oder neuer – aus dem **Microsoft Store** |
| FlexRadio | 6600 (oder anderes SmartSDR-kompatibles Gerät) |
| SmartSDR | Muss aktiv laufen (für Discovery und TCP-Verbindung) |
| Wavelog | Beliebige Version mit aktivem API-Schlüssel (Lesen + Schreiben) |

---

## Installation

### Python über den Microsoft Store installieren

1. **Start → Microsoft Store** öffnen
2. Nach **"Python 3.12"** suchen und installieren
3. Python ist dann direkt über `python` oder `python3` im Terminal verfügbar

### Programm einrichten

Alle Dateien in einen Ordner entpacken, dann `start.bat` doppelklicken.

Das Skript:
1. Sucht automatisch die Store-Python-Installation
2. Erstellt eine virtuelle Umgebung im Unterordner `venv\`
3. Installiert alle Abhängigkeiten (`PyQt6`, `requests`)
4. Startet das Programm ohne Konsolenfenster (`pythonw`)

**Manuell (alternativ):**
```bat
python -m venv venv
venv\Scripts\pip install -r requirements.txt
venv\Scripts\pythonw flex_wavelog_bridge.py
```

---

## Erste Schritte

### 1. Wavelog konfigurieren

Tab **Konfiguration** öffnen:

- **Wavelog URL** – z.B. `https://log.meinecall.de` (kein abschliessend `/`)
- **API-Schlüssel** – in Wavelog unter *Benutzerkonto → API-Schlüssel* erstellen (Lesen + Schreiben)
- **Funkgerät-Name** – beliebiger Name, erscheint so in Wavelog
- **Update-Intervall** – Sendefrequenz an Wavelog (Standard: 5 s)

Auf **"Einstellungen speichern"** klicken, dann **"Wavelog-Verbindung testen"**.

### 2. FlexRadio verbinden

Tab **Steuerung**:

1. **"Gerät suchen"** – sucht 4 Sekunden via UDP-Broadcast
2. Gefundenes Gerät auswählen und **"Verbinden"** klicken
3. Alternativ: IP-Adresse manuell eingeben (z.B. bei belegtem UDP-Port)

### 3. Slice-Auswahl

Nach erfolgreicher Verbindung erscheinen alle offenen Slices in der Slice-Auswahl:

| Modus | Verhalten |
|---|---|
| Auto (TX-Slice) | Verfolgt automatisch den Slice mit `tx=1` |
| Manuell wählen | Feste Auswahl eines Slice, unabhaengig vom TX-Status |

Im Auto-Modus wird der erste (niedrigste) Slice als Fallback verwendet wenn kein TX-Slice aktiv ist.

---

## Autostart

1. `Win+R` → `shell:startup` eingeben
2. Verknuepfung zu `start.bat` in den Autostart-Ordner legen
3. In der App **"Beim Start automatisch verbinden"** aktivieren

---

## Dateien

```
flex_wavelog_bridge.py   Hauptprogramm
requirements.txt         Python-Abhaengigkeiten (PyQt6, requests)
start.bat                Starter-Skript fuer Windows
README.md                Diese Datei
CLAUDE.md                Technische Dokumentation fuer Entwickler
```

**Benutzerdaten** (werden automatisch angelegt):
```
%APPDATA%\FlexWavelogBridge\
    config.json          Konfiguration
    bridge.log           Detailliertes Log (DEBUG-Level)
```

---

## Modus-Mapping FlexRadio → ADIF/Wavelog

| FlexRadio-Mode | Wavelog/ADIF |
|---|---|
| USB, LSB | SSB |
| AM, SAM | AM |
| FM, NFM, DFM | FM |
| CW | CW |
| RTTY | RTTY |
| DIGU, DIGL, FDV | DIGI |

Unbekannte Modi werden unveraendert uebertragen.

---

## Fehlerbehebung

| Problem | Ursache | Loesung |
|---|---|---|
| Discovery findet kein Geraet | SmartSDR nicht aktiv, UDP/4992 geblockt | SmartSDR starten; Firewall pruefen; IP manuell eingeben |
| Port nicht verfuegbar | Anderes Programm belegt UDP/4992 | IP manuell im Dialog eingeben |
| Verbindung abgelehnt | SmartSDR laeuft nicht / falsche IP | SmartSDR-Status und IP pruefen |
| Timeout | Anderes Subnetz / VPN / Firewall | Erreichbarkeit mit `ping` pruefen |
| Wavelog 401 | API-Schluessel ungueltig oder zu wenig Rechte | Neuen Schluessel mit Lesen+Schreiben erstellen |
| Wavelog 404 | URL falsch | URL mit `/index.php/` testen |
| Wavelog 302 | HTTP/HTTPS verwechselt | `https://` statt `http://` (oder umgekehrt) |
| Kein Slice sichtbar | Slice in SmartSDR nicht geoeffnet | Mindestens einen Slice in SmartSDR oeffnen |
| Falscher Slice | Kein TX-Slice aktiv | TX in SmartSDR aktivieren oder Manuell-Modus nutzen |
| Programm nicht sichtbar | `pythonw` startet ohne Fenster | System-Tray pruefen (Pfeil neben der Uhr) |
