# Ladeauslastung Rastanlagen – Crawler

Fragt alle 30 Minuten den AFIR-Live-Status der Schnellladepunkte (≥ 50 kW) an den Standorten in `sites.json` ab.
Quelle: MobiData BW – Open ChargePoint DataBase (https://api.mobidata-bw.de/ocpdb/), Datenlizenz Deutschland – Namensnennung 2.0.

| Ordner | Inhalt | Größe (Richtwert) |
|---|---|---|
| `live/<ID>.json` | aktueller Stand je Ladepunkt | wird überschrieben |
| `agg/<ID>/<Datum>.json` | Stundenwerte je Betreiber: belegt_min, beobachtet_min, gestört_min, Ladevorgänge, Spitze | ≈ 1–2 KB je Standort und Tag |
| `events/<ID>/<Monat>.csv` | Rohdaten: nur Statuswechsel | ≈ 0,3–1 MB je Standort und Monat |
| `state/<ID>.json` | letzter Stand (für die Rekonstruktion) | wird überschrieben |

Zwischen zwei Abfragen wird der Verlauf aus dem Zeitpunkt des letzten Statuswechsels (`last_updated`) rekonstruiert.

Neuen Standort aufnehmen: Eintrag in `sites.json` ergänzen (`locIds` leer = alle Ladeorte mit Live-Daten ≥ 50 kW im Radius).
Manuell starten: Actions → „Ladeauslastung abfragen“ → Run workflow.
