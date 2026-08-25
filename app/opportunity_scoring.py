"""Phase 8 — transparent, deterministic, VERSIONED opportunity scoring.

Every component is visible in the returned breakdown — there are no hidden weights. Weights live in config
(`OPP_SCORE_WEIGHTS`, env-tunable) and the `score_version` is derived from them, so changing a weight produces a
NEW version and never silently rewrites a historical snapshot. Insufficient evidence LOWERS confidence and
applies a missing-data penalty; nothing is AI-generated; a score is never based on lead volume.
"""
import hashlib
from datetime import datetime

from .config import OPP_SCORE_WEIGHTS

_STRENGTH_POINTS = {"strong": 1.0, "moderate": 0.6, "weak": 0.3}


def scoring_version() -> str:
    """A stable version string that CHANGES when the weights change (so old snapshots keep their own version)."""
    blob = ",".join(f"{k}={OPP_SCORE_WEIGHTS[k]}" for k in sorted(OPP_SCORE_WEIGHTS))
    return "s1:" + hashlib.sha256(blob.encode()).hexdigest()[:8]


def _clamp01(x):
    return max(0.0, min(1.0, x))


def score(signals, matches, *, now=None):
    """Compute (score:int 0-100, confidence:int 0-100, breakdown:list). `signals` = DemandSignal rows,
    `matches` = OpportunityMatch rows. Pure — no DB, no side effects."""
    now = now or datetime.utcnow()
    W = OPP_SCORE_WEIGHTS
    comps = []

    def add(name, factor01, weight, note=""):
        pts = round(_clamp01(factor01) * weight, 2)
        comps.append({"component": name, "weight": weight, "factor": round(_clamp01(factor01), 3),
                      "points": pts, "note": note})
        return pts

    n = len(signals)
    # demand strength — the best evidence tier present (a single Deal outweighs many weak signals)
    best = max((_STRENGTH_POINTS.get(s.strength, 0.3) for s in signals), default=0.0)
    total = add("demand_strength", best, W["demand_strength"], "best evidence tier among signals")
    # signal quality — mean confidence of the supporting signals
    avg_conf = (sum(s.confidence for s in signals) / n / 100.0) if n else 0.0
    total += add("signal_quality", avg_conf, W["signal_quality"], "mean signal confidence")
    # signal freshness — newest signal age (fresher = higher); >180d contributes ~0
    if n:
        newest = max((s.observed_at or s.created_at) for s in signals)
        age_d = max(0.0, (now - newest).total_seconds() / 86400.0)
        fresh = _clamp01(1.0 - age_d / 180.0)
    else:
        fresh = 0.0
    total += add("signal_freshness", fresh, W["signal_freshness"], "age of newest supporting signal")
    # independent sources — distinct source types (diminishing after ~3)
    sources = len({(s.source or "?") for s in signals})
    total += add("independent_sources", _clamp01(sources / 3.0), W["independent_sources"],
                 f"{sources} distinct source(s)")
    # supply availability — any matched product
    has_supply = any(m.product_id for m in matches)
    total += add("supply_available", 1.0 if has_supply else 0.0, W["supply_available"],
                 "at least one matched product" if has_supply else "no matched supply")
    # supplier readiness — a verified match
    ready = any(m.verified for m in matches)
    total += add("supplier_readiness", 1.0 if ready else (0.4 if has_supply else 0.0),
                 W["supplier_readiness"], "a verified supplier match" if ready else "unverified/none")
    # quote/Deal evidence — a strong commercial signal present
    strong = any(s.strength == "strong" for s in signals)
    total += add("quote_deal_evidence", 1.0 if strong else 0.0, W["quote_deal_evidence"],
                 "an accepted quote or Deal supports this" if strong else "no accepted-quote/Deal evidence")
    # feasibility proxy — a matched product with few missing requirements
    feas = 0.0
    if matches:
        clean = sum(1 for m in matches if not (m.missing_requirements or "").strip())
        feas = clean / len(matches)
    total += add("feasibility", feas, W["feasibility"], "share of matches with no missing requirements")

    # missing-data penalty (subtracted) — thin evidence must not score high
    missing = 0.0
    if n == 0:
        missing = 1.0
    else:
        gaps = 0
        if not has_supply:
            gaps += 1
        if not strong:
            gaps += 1
        if sources < 2:
            gaps += 1
        missing = _clamp01(gaps / 3.0)
    penalty = round(missing * W["missing_data_penalty"], 2)
    comps.append({"component": "missing_data_penalty", "weight": -W["missing_data_penalty"],
                  "factor": round(missing, 3), "points": -penalty,
                  "note": "insufficient evidence lowers the score"})
    total -= penalty

    final = int(max(0, min(100, round(total))))
    # confidence tracks how much evidence backs the score (independent of the score value)
    confidence = int(max(0, min(100, round(100 * (0.4 * best + 0.3 * avg_conf + 0.3 * _clamp01(n / 3.0))))))
    return final, confidence, comps
