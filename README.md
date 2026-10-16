# Ladeauslastung Rastanlagen – Crawler

Fragt alle 15 Minuten den AFIR-Live-Status der Schnellladepunkte (≥ 50 kW) an den Standorten in `sites.json` ab (ausgelegt für bis zu ~500 Standorte, 12 parallele Abfragen).
Quelle: MobiData BW – Open ChargePoint DataBase (https://api.mobidata-bw.de/ocpdb/), Datenlizenz Deutschland – Namensnennung 2.0.

Die Ergebnisse liegen im Branch `data`. Er besteht immer nur aus einem Commit, der bei jedem Lauf ersetzt wird; das Repository wächst dadurch nicht mit jeder Abfrage.

| Datei im Branch `data` | Inhalt | Aufbewahrung |
|---|---|---|
| `export/live-<k>.json` | aktueller Stand je Ladepunkt, Standortgruppe k (8 Gruppen) | wird überschrieben |
| `export/day-<k>-<Datum>.json` | Stundenwerte je Standort und Betreiber: belegt_min, beobachtet_min, gestört_min, Ladevorgänge, Spitze | 35 Tage |
| `export/prof-<k>-<Monat>.json` | Wochenprofil (Wochentag × Stunde) je Standort und Betreiber | 24 Monate |
| `events/<Datum>.csv.gz` | Rohdaten: alle Statuswechsel | 400 Tage |
| `state.json` | letzter Stand je Ladepunkt (Rekonstruktion) | wird überschrieben |

`bridge.py` plant die stündliche Übertragung in die Dashboard-Datenbank (Claude-Task „Ladeauslastung – Dashboard aktualisieren“).

Neuen Standort aufnehmen: Eintrag in `sites.json` ergänzen (`locIds` leer = alle Ladeorte mit Live-Daten ≥ 50 kW im Radius).
Manuell starten: Actions → „Ladeauslastung abfragen“ → Run workflow.
