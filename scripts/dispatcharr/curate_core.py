#!/usr/bin/env python3
"""
curate_core.py — build a curated ~core playlist spec for Dispatcharr.

Merges matching streams across BOTH sources (by tvg_id, else name) into one
channel each = automatic failover. Dedupes globally so no channel repeats.
Writes core.yaml, which you then review and `./playlist.py apply core.yaml`.

Run:  ./curate_core.py        # writes core.yaml
"""
from __future__ import annotations
import re
from pathlib import Path

import yaml
from playlist import Client, load_creds, _norm  # reuse the tool's client

# Groups we trust as "real channels" — excludes event feeds like
# "SPORTS MIX PLAYOFFS", "Sports | CBC", "Sports | MLB", etc.
ALLOWED_GROUPS = {
    "US | Sports", "US | News", "US | Entertainment",
    "US | Local ABC", "US | Local CBS", "US | Local NBC", "US | Local FOX",
    "US | Local PBS", "US | Local CW & MY",
    "USA FULL HD", "USA TV", "USA TV WEST", "NEWS NETWORKS",
    "USA ABC LOCALS", "USA CBS LOCALS", "USA NBC LOCALS", "USA FOX LOCALS",
    "USA PBS LOCALS", "USA CW LOCALS", "USA NBA", "USA NFL", "USA NHL",
    "CA | Sports", "CA | Regional", "CA | French", "CANADIAN TV",
}

# Per-block allowed source groups. The provider mis-tags some streams (e.g. a
# US|Entertainment stream carrying tvg=ABCKABC.us), so we only merge a tvg's streams
# from groups that make sense for the block — prevents cross-category contamination.
G_LOCALS = {"US | Local ABC", "US | Local CBS", "US | Local NBC", "US | Local FOX",
            "US | Local PBS", "US | Local CW & MY", "USA ABC LOCALS", "USA CBS LOCALS",
            "USA NBC LOCALS", "USA FOX LOCALS", "USA PBS LOCALS", "USA CW LOCALS"}
G_NEWS = {"US | News", "NEWS NETWORKS", "USA FULL HD"}
G_ENT = {"US | Entertainment", "USA FULL HD", "USA TV", "USA TV WEST"}
G_USSPORT = {"US | Sports", "USA FULL HD", "USA NBA", "USA NFL", "USA NHL"}
G_CA = {"CA | Sports", "CA | Regional", "CA | French", "CANADIAN TV"}

# Explicit blocks: (block label, start number, allowed-groups, [(display, tvg_id), ...])
# Entries marked REC are my recommended additions beyond your explicit asks.
US_LOCALS = ("US Locals — Los Angeles", 100, G_LOCALS, [
    ("ABC — KABC Los Angeles", "ABCKABC.us"),           # REC (you listed FOX/CBS/NBC/PBS)
    ("CBS — KCBS Los Angeles", "CBSKCBS.us"),
    ("CBS — KCAL Los Angeles", "NewsKCAL.us"),
    ("NBC — KNBC Los Angeles", "NBCKNBC.us"),
    ("FOX — KTTV Los Angeles", "FOXKTTV.us"),
    ("PBS — KOCE Los Angeles", "PBSKOCE.us"),
    ("PBS — KLCS Los Angeles", "KLCS-DT.us_locals1"),
    ("CW — KTLA 5 Los Angeles", "CWKTLA.us"),           # REC (major LA local news)
    ("MyNet — KCOP 13 Los Angeles", "Fox11+KCOP.us"),   # REC
])
US_NEWS = ("US News", 130, G_NEWS, [
    ("CNN", "CNN.us"),
    ("Fox News", "FoxNewsChannel.us"),
    ("MSNBC (MS NOW)", "MSNOW.us"),                      # MSNBC rebranded to MS NOW
    ("CNBC", "CNBC.us"),                                 # REC
    ("Fox Business", "FoxBusiness.us"),                  # REC
    ("The Weather Channel", "TheWeatherChannel.us"),     # REC
    ("BBC World News", "BBCWorldNews.us"),               # REC
    ("NewsNation", "NewsNation.us"),                     # REC
])
US_ENT = ("US Entertainment (incl. playoff carriers)", 200, G_ENT, [
    ("TNT", "TNT.us"),
    ("TBS", "TBS.us"),
    ("truTV", "truTV.us"),                               # REC (NBA/MLB playoff overflow)
    ("USA Network", "USANetwork.us"),                    # REC (WWE/some NFL)
    ("Discovery Channel", "DiscoveryChannel.us"),
    ("History", "History.us"),
    ("HGTV", "HGTV.us"),
    ("Food Network", "FoodNetwork.us"),
    ("A&E", "AandENetwork.us"),                          # REC
    ("AMC", "AMC.us"),                                   # REC
    ("FX", "FX.us"),                                     # REC
    ("FXX", "FXX.us"),                                   # REC
    ("Bravo", "Bravo.us"),                               # REC
    ("Comedy Central", "ComedyCentral.us"),              # REC
    ("Paramount Network", "ParamountNetwork.us"),        # REC
    ("TLC", "TLC.us"),                                   # REC
    ("National Geographic", "NationalGeographic.us"),    # REC
    ("Nat Geo Wild", "NationalGeographicWild.us"),       # REC
    ("Animal Planet", "AnimalPlanet.us"),               # REC
    ("Syfy", "Syfy.us"),                                 # REC
    ("Freeform", "Freeform.us"),                         # REC
    ("Hallmark Channel", "HallmarkChannel.us"),          # REC
    ("Lifetime", "Lifetime.us"),                         # REC
])
US_SPORTS = ("US Sports", 300, G_USSPORT, [
    ("ESPN", "ESPN.us"),
    ("ESPN2", "ESPN2.us"),
    ("ESPNU", "ESPNU.us"),
    ("ESPNews", "ESPNEWS.us"),
    ("ESPN Deportes", "ESPNDeportes.us"),
    ("ESPN8: The Ocho", "ESPN8TheOcho.us"),
    ("Spectrum SportsNet LA (Dodgers)", "SpectrumSportsNetLADodgers.us"),
    ("Spectrum SportsNet (Lakers)", "SpectrumSportsNetLALakers.us"),  # REC
    ("Tennis Channel", "TennisChannel.us"),
    ("Tennis Channel 2", "T2.us"),
    ("Fox Sports 1", "FoxSports1.us"),                   # REC
    ("Fox Sports 2", "FoxSports2.us"),                   # REC
    ("NFL Network", "NFLNetwork.us"),                    # REC
    ("NBA TV", "NBAtv.us"),                              # REC
    ("MLB Network", "MLBNetwork.us"),                    # REC
    ("Golf Channel", "GolfChannel.us"),                  # REC
    ("CBS Sports Network", "CBSSportsNetwork.us"),       # REC
    ("beIN Sports", "beINSports.us"),                    # REC
])
CA_LOCALS = ("CA Locals — Alberta + national", 500, G_CA, [
    ("CBC Calgary", "CBCCalgary-CBRT.ca"),
    ("CBC Edmonton", "CBCEdmonton-CBXT.ca"),
    ("CBC News Network", "CBCNewsNetwork.ca"),
    ("CTV Calgary", "CTVCalgary-CFCN.ca"),
    ("CTV Edmonton", "CTVEdmonton-CFRN.ca"),
    ("CTV News Channel", "CTVNewsChannel.ca"),
    ("Global Calgary", "GlobalCalgary-CICT.ca"),
    ("Global Edmonton", "GlobalEdmonton-CITV.ca"),
])
CA_FRENCH = ("CA French", 600, G_CA, [
    ("RDS", "ReseaudesSports-RDS.ca"),
    ("RDS 2", "RDS2.ca"),
    ("RDS Info", "ReseaudesSportsInfo-RDSI.ca"),
    ("TVA Sports", "TVASports.ca"),                      # REC (French sports)
    ("TVA Sports 2", "TVASports2.ca"),                   # REC
    ("ICI Radio-Canada (Montréal)", "ICIGrandMontreal-CBFT.ca"),
    ("ICI RDI", "ICI-RDI.ca"),
    ("Noovo", "NOOVO-Montreal-CFJP.ca"),
    ("TVA (Montréal)", "TVA-Montreal-CFTM.ca"),
])
EXPLICIT_BLOCKS = [US_LOCALS, US_NEWS, US_ENT, US_SPORTS, CA_LOCALS, CA_FRENCH]

# Whole-group block: every real channel in "CA | Sports" (your "all CA sports").
CA_SPORTS = ("CA Sports — all", 400, "CA | Sports")


def main():
    c = Client(*load_creds())
    gmap = {g["id"]: g["name"] for g in c.get("/api/channels/groups/")}

    # Build index: tvg_id -> [streams], name_norm -> [streams], group -> [streams]
    by_tvg: dict = {}
    by_group: dict = {}
    for s in c.paginate("/api/channels/streams/"):
        grp = gmap.get(s.get("channel_group"), "")
        if grp not in ALLOWED_GROUPS:
            continue
        s["_grp"] = grp
        by_group.setdefault(grp, []).append(s)
        if s.get("tvg_id"):
            by_tvg.setdefault(s["tvg_id"], []).append(s)

    def order(streams):
        # failover order: source 3 (Eros, often 1080p) first, skip "backup" to back
        return sorted(streams, key=lambda s: (
            0 if s["m3u_account"] == 3 else 1,
            1 if "backup" in s["name"].lower() else 0,
            s["id"],
        ))

    used_tvg: set = set()
    channels: list = []

    def add(name, num, streams, tvg):
        streams = order(streams)
        channels.append({
            "name": name, "number": num, "group": streams[0]["_grp"],
            "tvg_id": tvg or "", "streams": [s["id"] for s in streams],
            "enabled": True,
            "_failover": len(streams),
            "_picked_from": sorted({s["m3u_account"] for s in streams}),
        })

    # explicit blocks
    for label, start, allowed, entries in EXPLICIT_BLOCKS:
        n = start
        for disp, tvg in entries:
            streams = [s for s in by_tvg.get(tvg, []) if s["_grp"] in allowed]
            if not streams:
                print(f"  !! MISSING {disp} (tvg={tvg}) — skipped")
                continue
            add(disp, float(n), streams, tvg)
            used_tvg.add(tvg)
            n += 1

    # whole-group CA Sports (skip anything already placed; merge by tvg, else name)
    label, start, grp = CA_SPORTS
    seen_keys: set = set()
    buckets: dict = {}
    for s in by_group.get(grp, []):
        key = ("tvg:" + s["tvg_id"]) if s.get("tvg_id") else ("name:" + _norm(s["name"]))
        if s.get("tvg_id") and s["tvg_id"] in used_tvg:
            continue
        buckets.setdefault(key, []).append(s)
    def clean(nm):
        nm = re.sub(r"^(CA|FRCAN|US)\s*:\s*", "", nm)   # strip "CA : " provider prefix
        nm = re.sub(r"^\(CA\)\s*", "", nm)
        nm = re.sub(r"\s+", " ", nm).strip()
        return nm

    n = start
    for key, streams in sorted(buckets.items(), key=lambda kv: kv[1][0]["name"].lower()):
        tvg = streams[0].get("tvg_id") or ""
        # pull same-tvg feeds from the other source (e.g. Delta CANADIAN TV) for failover
        if tvg:
            streams = [s for s in by_tvg.get(tvg, streams) if s["_grp"] in G_CA] or streams
        name = clean(order(streams)[0]["name"])         # prefer Eros-source name, cleaned
        add(name, float(n), streams, tvg)
        n += 1

    spec = {"profile": "Core", "channels": channels}
    out = Path(__file__).resolve().parent / "core.yaml"
    out.write_text(yaml.safe_dump(spec, sort_keys=False, allow_unicode=True, width=200))
    multi = sum(1 for ch in channels if ch["_failover"] > 1)
    print(f"\nWrote {len(channels)} channels -> {out}")
    print(f"  {multi} have multi-source failover; "
          f"{len(channels)-multi} single-source.")


if __name__ == "__main__":
    main()
