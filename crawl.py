#!/usr/bin/env python3
"""Ladeauslastung Rastanlagen – Crawler.

Fragt je Standort aus sites.json den AFIR-Live-Status der Schnellladepunkte
(MobiData BW OCPDB) ab und schreibt
  live/<site>.json             aktueller Stand je Ladepunkt
  agg/<site>/<YYYY-MM-DD>.json Stundenwerte je Betreiber (Ortszeit Europe/Berlin)
  events/<site>/<YYYY-MM>.csv  Statuswechsel (Rohdaten, nur Änderungen)
  state/<site>.json            letzter Stand je Ladepunkt (für die Rekonstruktion)
Nur Python-Standardbibliothek.
"""
import csv, json, os, sys, time, urllib.request
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

API = "https://api.mobidata-bw.de/ocpdb/api/public/v1/locations"
TZ = ZoneInfo("Europe/Berlin")
MIN_KW = 50
OCC = {"CHARGING", "BLOCKED", "RESERVED"}
BAD = {"OUTOFORDER", "INOPERATIVE"}
GAP_MIN = 120      # längere Lücke zwischen zwei Abfragen = keine Rekonstruktion
BACKFILL_MIN = 60  # beim ersten Stand höchstens so weit zurück rekonstruieren
ROOT = os.path.dirname(os.path.abspath(__file__))


def fetch(site, fixture=None):
    if fixture:
        return json.load(open(fixture, encoding="utf-8"))
    url = f"{API}?lat={site['lat']}&lon={site['lon']}&radius={site.get('radius', 1000)}&limit=200"
    req = urllib.request.Request(url, headers={"User-Agent": "ladeauslastung-crawler/1.0"})
    for i in range(3):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.load(r)
        except Exception as e:  # noqa: BLE001
            if i == 2:
                raise
            time.sleep(5 * (i + 1))


def kw_of(evse):
    return round(max([c.get("max_electric_power") or 0 for c in evse.get("connectors", [])] or [0]) / 1000)


def to_min(iso):
    return int(datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp() // 60)


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


def local(tmin):
    return datetime.fromtimestamp(tmin * 60, tz=timezone.utc).astimezone(TZ)


class Agg:
    """Tagesdateien mit Stundenwerten je Betreiber: [belegt_min, beobachtet_min, gestört_min, Ladevorgänge, Spitze]."""

    def __init__(self, site_id):
        self.site, self.days = site_id, {}

    def day(self, date):
        if date not in self.days:
            self.days[date] = load(os.path.join(ROOT, "agg", self.site, f"{date}.json"),
                                   {"site": self.site, "date": date, "ops": {}, "h": {}})
        return self.days[date]

    def cell(self, tmin, op):
        d = local(tmin)
        day = self.day(d.strftime("%Y-%m-%d"))
        rows = day["h"].setdefault(op, [[0, 0, 0, 0, 0] for _ in range(24)])
        return rows[d.hour]

    def add(self, a, b, status, op):
        t = a
        while t < b:
            e = min(b, (t // 60 + 1) * 60)
            c = self.cell(t, op)
            c[1] += e - t
            if status in OCC:
                c[0] += e - t
            if status in BAD:
                c[2] += e - t
            t = e

    def save(self):
        for date, obj in self.days.items():
            for rows in obj["h"].values():
                for r in rows:
                    r[0], r[1], r[2] = round(r[0], 1), round(r[1], 1), round(r[2], 1)
            save(os.path.join(ROOT, "agg", self.site, f"{date}.json"), obj)


def run_site(site, now_min, fixture=None):
    data = fetch(site, fixture)
    ids = set(site.get("locIds") or [])
    locs = [l for l in data.get("items", [])
            if ((l["id"] in ids) if ids else any(e.get("status") != "STATIC" and kw_of(e) >= MIN_KW for e in l.get("evses", [])))]
    state_p = os.path.join(ROOT, "state", f"{site['id']}.json")
    state = load(state_p, {})
    agg = Agg(site["id"])
    live = {"site": site["id"], "t": now_min, "ts": local(now_min).isoformat(), "locs": []}
    events, occ_now, new_state = [], {}, {}
    for l in locs:
        op = (l.get("operator") or {}).get("name") or "unbekannt"
        lv = {"id": l["id"], "op": op, "name": l.get("name") or l.get("address") or "", "e": []}
        for e in l.get("evses", []):
            kw = kw_of(e)
            st = e.get("status", "UNKNOWN")
            if kw < MIN_KW or st == "STATIC":
                continue
            eid = e.get("evse_id") or e.get("uid")
            lu = min(to_min(e["last_updated"]), now_min) if e.get("last_updated") else now_min
            lv["e"].append([eid, kw, st, lu])
            p = state.get(eid)
            if not p or now_min - p["t"] > GAP_MIN:
                agg.add(max(lu, now_min - BACKFILL_MIN), now_min, st, op)
                if st in OCC and lu > now_min - BACKFILL_MIN:
                    agg.cell(lu, op)[3] += 1
            else:
                if p["lu"] < lu and p["t"] < lu < now_min:
                    agg.add(p["t"], lu, p["st"], op)
                    agg.add(lu, now_min, st, op)
                else:
                    agg.add(p["t"], now_min, st, op)
                if st in OCC and (p["st"] not in OCC or lu > p["t"]):
                    agg.cell(max(lu, p["t"]), op)[3] += 1
            if not p or p["st"] != st or p["lu"] != lu:
                events.append([local(lu).isoformat(), eid, op, kw, p["st"] if p else "", st])
            new_state[eid] = {"st": st, "lu": lu, "t": now_min}
            if st in OCC:
                occ_now[op] = occ_now.get(op, 0) + 1
        live["locs"].append(lv)
        day = agg.day(local(now_min).strftime("%Y-%m-%d"))
        day["ops"][op] = {"lp": len(lv["e"]), "kw": sum(x[1] for x in lv["e"]), "name": lv["name"]}
    for op in {x["op"] for x in live["locs"]}:
        c = agg.cell(now_min - 1, op)
        c[4] = max(c[4], occ_now.get(op, 0))
    agg.save()
    save(state_p, new_state)
    save(os.path.join(ROOT, "live", f"{site['id']}.json"), live)
    if events:
        ep = os.path.join(ROOT, "events", site["id"], local(now_min).strftime("%Y-%m") + ".csv")
        os.makedirs(os.path.dirname(ep), exist_ok=True)
        new = not os.path.exists(ep)
        with open(ep, "a", newline="", encoding="utf-8") as f:
            w = csv.writer(f, delimiter=";")
            if new:
                w.writerow(["zeit_statuswechsel", "evse_id", "betreiber", "kw", "von", "nach"])
            w.writerows(events)
    return len(live["locs"]), sum(len(x["e"]) for x in live["locs"]), sum(occ_now.values())


def main():
    sites = load(os.path.join(ROOT, "sites.json"), [])
    now_min = int(time.time() // 60)
    fixture = os.environ.get("CRAWL_FIXTURE")
    if os.environ.get("CRAWL_NOW_MIN"):
        now_min = int(os.environ["CRAWL_NOW_MIN"])
    failed = 0
    for s in sites:
        try:
            n_loc, n_lp, n_occ = run_site(s, now_min, fixture)
            print(f"{s['id']}: {n_loc} Ladeorte, {n_lp} Ladepunkte, {n_occ} belegt")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"{s['id']}: FEHLER {e}", file=sys.stderr)
    sys.exit(1 if failed == len(sites) and sites else 0)


if __name__ == "__main__":
    main()
