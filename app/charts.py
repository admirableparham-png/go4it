"""Phase 8 — server-side chart geometry (no client JS, no CDN).

Pure functions that turn aggregate data into geometry the Jinja SVG/bar macros render. Every chart has an
accessible label set and a table fallback (the raw rows are always available to the template). Nothing here
touches the DB.
"""


def bar_rows(counts: dict, *, top=None, drop_empty=False):
    """{label: count} -> sorted rows each with a 0-100 width pct (mirrors main._bars)."""
    items = [(k or "—", v) for k, v in counts.items() if not (drop_empty and not k)]
    items.sort(key=lambda x: -x[1])
    if top:
        items = items[:top]
    mx = max((v for _, v in items), default=1) or 1
    return [{"label": k, "count": v, "pct": round(v / mx * 100)} for k, v in items]


def funnel_rows(stages: list):
    """funnel stages (from analytics.funnel) -> rows with width relative to the first stage + conversion."""
    top = stages[0]["count"] if stages else 0
    top = top or 1
    return [{"key": s["key"], "label": s["label"], "count": s["count"],
             "pct": round(s["count"] / top * 100), "conversion": s.get("conversion"),
             "insufficient": s.get("insufficient", False)} for s in stages]


def sparkline(series: list, *, width=240, height=48, pad=4):
    """series = [(label, value), ...] -> {points, area, min, max, last, empty} for an inline SVG polyline.
    Empty/one-point series are handled (empty=True) so the template can show a fallback."""
    vals = [float(v or 0) for _, v in series]
    if len(vals) < 2:
        return {"points": "", "area": "", "min": min(vals or [0]), "max": max(vals or [0]),
                "last": vals[-1] if vals else 0, "empty": True, "width": width, "height": height}
    lo, hi = min(vals), max(vals)
    span = (hi - lo) or 1.0
    n = len(vals)
    xs = [pad + (width - 2 * pad) * i / (n - 1) for i in range(n)]
    ys = [pad + (height - 2 * pad) * (1 - (v - lo) / span) for v in vals]
    points = " ".join(f"{x:.1f},{y:.1f}" for x, y in zip(xs, ys))
    area = f"{xs[0]:.1f},{height - pad:.1f} " + points + f" {xs[-1]:.1f},{height - pad:.1f}"
    return {"points": points, "area": area, "min": lo, "max": hi, "last": vals[-1], "empty": False,
            "width": width, "height": height}


def series_from_buckets(buckets: dict):
    """{'2026-01': 3, '2026-02': 5, ...} -> a chronologically sorted [(label, value)] series."""
    return [(k, buckets[k]) for k in sorted(buckets)]
