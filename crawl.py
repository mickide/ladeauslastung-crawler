#!/usr/bin/env python3
"""Ladeauslastung Rastanlagen – Crawler (ausgelegt für bis zu ~500 Standorte).

Fragt je Standort aus sites.json den AFIR-Live-Status der Schnellladepunkte
(MobiData BW OCPDB) ab. Alle Ergebnisse liegen im Ordner DATA (Branch "data",
wird bei jedem Lauf ohne Historie überschrieben, daher wächst das Repository nicht):

  state.json                        letzter Stand je Ladepunkt (Rekonstruktion)
  export/sites.json                 Standorte inkl. Gruppe (chunk)
  export/live-<k>.json              aktueller Stand, Gruppe k (8 Gruppen)
  export/day-<k>-<YYYY-MM-DD>.json  Stundenwerte je Standort/Betreiber (35 Tage)
  export/prof-<k>-<YYYY-MM>.json    Wochenprofil (Wochentag × Stunde) je Monat (24 Monate)
  events/<YYYY-MM-DD>.csv.gz        Rohdaten: Statuswechsel aller Standorte (400 Tage)

Stundenwerte: [belegt_min, beobachtet_min, gestört_min, Ladevorgänge, Spitze]
Wochenprofil: 168 Zellen (Mo 0 Uhr … So 23 Uhr) mit [belegt_min, beobachtet_min, gestört_min, Ladevorgänge]
Nur Python-Standardbibliothek.
"""
import csv, gzip, io, json, os, sys, time, urllib.request, zlib
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

API = "https://api.mobidata-bw.de/ocpdb/api/public/v1/locations"
TZ = ZoneInfo("Europe/Berlin")
MIN_KW = 50
OCC = {"CHARGING", "BLOCKED", "RESERVED"}
BAD = {"OUTOFORDER", "INOPERATIVE"}
GAP_MIN = 60        # längere Lücke zwischen zwei Abfragen = nicht rekonstruieren
BACKFILL_MIN = 30   # erster Stand: höchstens so weit zurück
CHUNKS = 8
KEEP_DAYS, KEEP_MONTHS, KEEP_EVENT_DAYS = 35, 24, 400
ROOT = os.path.dirname(os.path.abspath(__file__))
DATA = os.environ.get("CRAWL_DATA", os.path.join(ROOT, "data"))


def chunk_of(site_id):
    return zlib.crc32(site_id.encode()) % CHUNKS


def fetch(site, fixture=None):
    if fixture:
        return json.load(open(fixture, encoding="utf-8"))
    url = f"{API}?lat={site['lat']}&lon={site['lon']}&radius={site.get('radius', 1000)}&limit=200"
    req = urllib.request.Request(url, headers={"User-Agent": "ladeauslastung-crawler/2.0"})
    for i in range(3):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.load(r)
        except Exception:  # noqa: BLE001
            if i == 2:
                raise
            time.sleep(4 * (i + 1))


def kw_of(evse):
    return round(max([c.get("max_electric_power") or 0 for c in evse.get("connectors", [])] or [0]) / 1000)


def to_min(iso):
    return int(datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp() // 60)


def local(tmin):
    return datetime.fromtimestamp(tmin * 60, tz=timezone.utc).astimezone(TZ)


def load(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default


def save(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, path)


class Store:
    """Hält Tages- und Monatsdateien der Gruppen im Speicher und schreibt sie am Ende."""

    def __init__(self):
        self.files = {}

    def get(self, name, default):
        if name not in self.files:
            self.files[name] = load(os.path.join(DATA, "export", name), default)
        return self.files[name]

    def day(self, k, date):
        return self.get(f"day-{k}-{date}.json", {"chunk": k, "date": date, "sites": {}})

    def prof(self, k, month):
        return self.get(f"prof-{k}-{month}.json", {"chunk": k, "month": month, "sites": {}})

    def cells(self, site, k, op, tmin):
        d = local(tmin)
        day = self.day(k, d.strftime("%Y-%m-%d"))["sites"].setdefault(site, {"ops": {}, "h": {}})
        h = day["h"].setdefault(op, [[0, 0, 0, 0, 0] for _ in range(24)])[d.hour]
        pr = self.prof(k, d.strftime("%Y-%m"))["sites"].setdefault(site, {}).setdefault(op, [[0, 0, 0, 0] for _ in range(168)])
        return h, pr[d.weekday() * 24 + d.hour]

    def add(self, site, k, op, a, b, status):
        t = a
        while t < b:
            e = min(b, (t // 60 + 1) * 60)
            for c in self.cells(site, k, op, t):
                c[1] += e - t
                if status in OCC:
                    c[0] += e - t
                if status in BAD:
                    c[2] += e - t
            t = e

    def session(self, site, k, op, tmin):
        for c in self.cells(site, k, op, tmin):
            c[3] += 1

    def peak(self, site, k, op, tmin, n):
        h, _ = self.cells(site, k, op, tmin)
        h[4] = max(h[4], n)

    def flush(self):
        for name, obj in self.files.items():
            for s in obj["sites"].values():
                rows = s["h"].values() if "h" in s else s.values()
                for r in rows:
                    for c in r:
                        for i in range(3):
                            c[i] = round(c[i], 1)
            save(os.path.join(DATA, "export", name), obj)


def process(site, data, now_min, state, store, events):
    k = chunk_of(site["id"])
    ids = set(site.get("locIds") or [])
    locs = [l for l in data.get("items", [])
            if ((l["id"] in ids) if ids else any(e.get("status") != "STATIC" and kw_of(e) >= MIN_KW for e in l.get("evses", [])))]
    live = {"t": now_min, "locs": []}
    occ_now, ops = {}, {}
    for l in locs:
        op = (l.get("operator") or {}).get("name") or "unbekannt"
        lv = {"id": l["id"], "op": op, "name": l.get("name") or l.get("address") or "", "e": []}
        for e in l.get("evses", []):
            kw, st = kw_of(e), e.get("status", "UNKNOWN")
            if kw < MIN_KW or st == "STATIC":
                continue
            eid = e.get("evse_id") or e.get("uid")
            lu = min(to_min(e["last_updated"]), now_min) if e.get("last_updated") else now_min
            lv["e"].append([eid, kw, st, lu])
            key = site["id"] + "|" + eid
            p = state.get(key)
            if not p or now_min - p["t"] > GAP_MIN:
                store.add(site["id"], k, op, max(lu, now_min - BACKFILL_MIN), now_min, st)
                if st in OCC and lu > now_min - BACKFILL_MIN:
                    store.session(site["id"], k, op, lu)
            else:
                if p["t"] < lu < now_min:
                    store.add(site["id"], k, op, p["t"], lu, p["st"])
                    store.add(site["id"], k, op, lu, now_min, st)
                else:
                    store.add(site["id"], k, op, p["t"], now_min, st)
                if st in OCC and (p["st"] not in OCC or lu > p["t"]):
                    store.session(site["id"], k, op, max(lu, p["t"]))
            if not p or p["st"] != st or p["lu"] != lu:
                events.append([site["id"], local(lu).isoformat(), eid, op, kw, p["st"] if p else "", st])
            state[key] = {"st": st, "lu": lu, "t": now_min}
            if st in OCC:
                occ_now[op] = occ_now.get(op, 0) + 1
        live["locs"].append(lv)
        o = ops.setdefault(op, {"lp": 0, "kw": 0, "name": []})
        o["lp"] += len(lv["e"])
        o["kw"] += sum(x[1] for x in lv["e"])
        o["name"].append(lv["name"])
    day = store.day(k, local(now_min).strftime("%Y-%m-%d"))["sites"].setdefault(site["id"], {"ops": {}, "h": {}})
    for op, o in ops.items():
        day["ops"][op] = {"lp": o["lp"], "kw": o["kw"], "name": " / ".join(o["name"])}
        store.peak(site["id"], k, op, now_min - 1, occ_now.get(op, 0))
    return k, live, sum(o["lp"] for o in ops.values()), sum(occ_now.values())


def cleanup(now_min):
    today = local(now_min).date()
    exp = os.path.join(DATA, "export")
    for f in os.listdir(exp) if os.path.isdir(exp) else []:
        try:
            if f.startswith("day-") and (today - datetime.strptime(f[-15:-5], "%Y-%m-%d").date()).days > KEEP_DAYS:
                os.remove(os.path.join(exp, f))
            if f.startswith("prof-"):
                y, m = map(int, f[-12:-5].split("-"))
                if (today.year - y) * 12 + today.month - m >= KEEP_MONTHS:
                    os.remove(os.path.join(exp, f))
        except ValueError:
            pass
    ev = os.path.join(DATA, "events")
    for f in os.listdir(ev) if os.path.isdir(ev) else []:
        try:
            if (today - datetime.strptime(f[:10], "%Y-%m-%d").date()).days > KEEP_EVENT_DAYS:
                os.remove(os.path.join(ev, f))
        except ValueError:
            pass


def main():
    sites = load(os.path.join(ROOT, "sites.json"), [])
    now_min = int(os.environ.get("CRAWL_NOW_MIN") or time.time() // 60)
    fixture = os.environ.get("CRAWL_FIXTURE")
    state = load(os.path.join(DATA, "state.json"), {})
    store, events, lives, failed = Store(), [], {}, []
    with ThreadPoolExecutor(max_workers=12) as ex:
        results = list(ex.map(lambda s: (s, _safe(fetch, s, fixture)), sites))
    total_lp = total_occ = 0
    for s, res in results:
        if isinstance(res, Exception):
            failed.append(s["id"])
            print(f"{s['id']}: FEHLER {res}", file=sys.stderr)
            continue
        k, live, n_lp, n_occ = process(s, res, now_min, state, store, events)
        lives.setdefault(k, {})[s["id"]] = live
        total_lp, total_occ = total_lp + n_lp, total_occ + n_occ
    # Live-Dateien: bestehende Einträge fehlgeschlagener Standorte behalten
    for k in range(CHUNKS):
        path = os.path.join(DATA, "export", f"live-{k}.json")
        old = load(path, {"chunk": k, "sites": {}})
        old["sites"] = {sid: v for sid, v in old["sites"].items() if any(x["id"] == sid for x in sites)}
        old["sites"].update(lives.get(k, {}))
        old["t"] = now_min
        save(path, old)
    store.flush()
    known = {s["id"] for s in sites}
    save(os.path.join(DATA, "state.json"), {k: v for k, v in state.items() if k.split("|")[0] in known})
    save(os.path.join(DATA, "export", "sites.json"),
         {"t": now_min, "sites": [dict(s, chunk=chunk_of(s["id"]), order=i + 1) for i, s in enumerate(sites)]})
    if events:
        ep = os.path.join(DATA, "events", local(now_min).strftime("%Y-%m-%d") + ".csv.gz")
        os.makedirs(os.path.dirname(ep), exist_ok=True)
        buf = io.StringIO()
        w = csv.writer(buf, delimiter=";")
        if not os.path.exists(ep):
            w.writerow(["standort", "zeit_statuswechsel", "evse_id", "betreiber", "kw", "von", "nach"])
        w.writerows(events)
        with gzip.open(ep, "at", encoding="utf-8", newline="") as f:  # angehängtes gzip-Member
            f.write(buf.getvalue())
    cleanup(now_min)
    print(f"{len(sites) - len(failed)}/{len(sites)} Standorte, {total_lp} Ladepunkte, {total_occ} belegt, {len(events)} Statuswechsel")
    sys.exit(1 if sites and len(failed) == len(sites) else 0)


def _safe(fn, *a):
    try:
        return fn(*a)
    except Exception as e:  # noqa: BLE001
        return e


if __name__ == "__main__":
    main()
