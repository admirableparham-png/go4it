"""Harvest buyers for a product line from its spec (docs/research/lines/<slug>.json).

    ./.venv/bin/python scripts/harvest_line.py cd-dvd

UAE lines pull the yellowpages directory (spec.harvest.slugs); other-country lines pull OpenStreetMap
(spec.harvest.osm = {label: {selectors:[...]}}). Writes docs/research/<spec.buyers_file>. Deduped by
company id. This is the ONE generic harvester that replaced the per-product scripts/harvest_uae_*.py.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.harvest_lib import harvest_osm_directory, harvest_uae_lines  # noqa: E402
from app.line_spec import load_spec, research_path  # noqa: E402


def _harvest_osm(spec):
    iso = spec["dest"]["iso"]
    buyers = {}
    for label, cfg in (spec.get("harvest", {}).get("osm") or {}).items():
        rows, capped = harvest_osm_directory(iso, cfg.get("selectors", []))
        for r in rows:
            rec = buyers.get(r["osmid"])
            if rec is None:
                rec = {"osmid": r["osmid"], "company": r.get("company", ""),
                       "city": r.get("city", ""),
                       "phones": [r["phone"]] if r.get("phone") else [],
                       "website": r.get("website", ""), "categories": [], "fits": [],
                       "needs_enrichment": not (r.get("phone") or r.get("website"))}
                buyers[r["osmid"]] = rec
            if label not in rec["categories"]:
                rec["categories"].append(label)
        print(f"   {label:24} -> {len(rows)} osm rows{' (capped)' if capped else ''}")
    recs = list(buyers.values())
    return {"source": "openstreetmap", "dest": iso, "total": len(recs),
            "with_phone": sum(1 for r in recs if r["phones"]),
            "with_website": sum(1 for r in recs if r["website"]),
            "buyers": recs}


def run(slug):
    spec = load_spec(slug)
    directory = spec.get("dest", {}).get("directory", "uae")
    if directory == "uae":
        result = harvest_uae_lines(spec.get("harvest", {}).get("slugs", {}), log=print)
    elif directory == "osm":
        result = _harvest_osm(spec)
    else:
        print(f"unknown directory kind '{directory}' in spec {slug}")
        return
    out = research_path(spec["buyers_file"])
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2, ensure_ascii=False)
    print(f"\n{result['total']} unique buyers "
          f"({result.get('with_phone', 0)} phone, {result.get('with_website', 0)} website) -> wrote {out}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: harvest_line.py <line-slug>   (e.g. cd-dvd, decoration)")
        sys.exit(1)
    run(sys.argv[1])
