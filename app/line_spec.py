"""Line specs — one JSON per product x country under docs/research/lines/.

A spec is the single config that drives the whole buyer pipeline for a product line:
harvest (directory slugs / OSM selectors) -> enrich+score (taxonomy) -> load (source/category) ->
the /lines/<slug> hub (families, meta, display order). Adding a product line = drop one spec file
here and run harvest_line -> enrich_line -> load_line; no new code.
"""
import json
import os

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LINES_DIR = os.path.join(BASE, "docs", "research", "lines")
RESEARCH_DIR = os.path.join(BASE, "docs", "research")


def spec_path(slug):
    return os.path.join(LINES_DIR, f"{slug}.json")


def load_spec(slug):
    with open(spec_path(slug), encoding="utf-8") as f:
        return json.load(f)


def all_specs():
    """Every valid line spec in LINES_DIR, sorted by filename (a spec must carry a `slug`)."""
    out = []
    if not os.path.isdir(LINES_DIR):
        return out
    for fn in sorted(os.listdir(LINES_DIR)):
        if not fn.endswith(".json"):
            continue
        try:
            spec = load_spec(fn[:-5])
        except Exception:  # noqa: BLE001
            continue
        if isinstance(spec, dict) and spec.get("slug"):
            out.append(spec)
    return out


def research_path(name):
    """Absolute path to a docs/research/<name> data file (buyers_file / rfqs_file)."""
    return os.path.join(RESEARCH_DIR, name)
