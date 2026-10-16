#!/usr/bin/env python3
"""Plant die Übertragung der Crawler-Ergebnisse in die Dashboard-Datenbank.

Aufruf: python3 bridge.py <data-Ordner> <versions.json> [--cleanup]
  versions.json: {"<collection>/<doc_id>": version, ...} aus den Listen der Datenbank
Ausgabe (stdout): JSON-Liste von Batches; jeder Batch ist eine Liste von Schreibvorgängen
im Format von ArtifactData action "batch" (op, collection, doc_id, file_path, if_version).
Dokumente in der Datenbank:
  live/<k>            aktueller Stand der Gruppe k
  day/<k>_<Datum>     Stundenwerte (heute und gestern werden aktualisiert; 35 Tage behalten)
  prof/<k>_<Monat>    Wochenprofil (laufender und Vormonat werden aktualisiert; 12 Monate behalten)
  meta/sites          Standortliste
"""
import json, os, sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

MAX_BATCH_BYTES = 850_000
KEEP_DAYS, KEEP_MONTHS = 35, 12


def main():
    data, vers_path = sys.argv[1], sys.argv[2]
    cleanup = "--cleanup" in sys.argv
    vers = json.load(open(vers_path)) if os.path.exists(vers_path) else {}
    exp = os.path.join(data, "export")
    now = datetime.now(ZoneInfo("Europe/Berlin"))
    days = {now.strftime("%Y-%m-%d"), (now - timedelta(days=1)).strftime("%Y-%m-%d")}
    months = {now.strftime("%Y-%m"), (now - timedelta(days=1)).strftime("%Y-%m")}
    writes = []

    def put(coll, doc, path):
        w = {"op": "set", "collection": coll, "doc_id": doc, "file_path": os.path.abspath(path)}
        v = vers.get(f"{coll}/{doc}")
        if v:
            w["if_version"] = int(v)
        writes.append((os.path.getsize(path), w))

    sites_path = os.path.join(exp, "sites.json")
    if os.path.exists(sites_path):
        put("meta", "sites", sites_path)
    for f in sorted(os.listdir(exp)):
        p = os.path.join(exp, f)
        if f.startswith("live-"):
            put("live", f[5:-5], p)
        elif f.startswith("day-") and f[-15:-5] in days:
            k = f[4:-16]
            put("day", f"{k}_{f[-15:-5]}", p)
        elif f.startswith("prof-") and f[-12:-5] in months:
            k = f[5:-13]
            put("prof", f"{k}_{f[-12:-5]}", p)
    if cleanup:
        cut_day = (now - timedelta(days=KEEP_DAYS)).strftime("%Y-%m-%d")
        for key, v in vers.items():
            coll, doc = key.split("/", 1)
            if coll == "day" and doc.split("_", 1)[1] < cut_day:
                writes.append((100, {"op": "delete", "collection": coll, "doc_id": doc, "if_version": int(v)}))
            if coll == "prof":
                y, m = map(int, doc.split("_", 1)[1].split("-"))
                if (now.year - y) * 12 + now.month - m >= KEEP_MONTHS:
                    writes.append((100, {"op": "delete", "collection": coll, "doc_id": doc, "if_version": int(v)}))
    batches, cur, size = [], [], 0
    for sz, w in writes:
        if cur and (size + sz > MAX_BATCH_BYTES or len(cur) >= 50):
            batches.append(cur)
            cur, size = [], 0
        cur.append(w)
        size += sz
    if cur:
        batches.append(cur)
    json.dump(batches, sys.stdout, ensure_ascii=False)
    print()


if __name__ == "__main__":
    main()
