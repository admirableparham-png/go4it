"""Generic buyer-fit scoring — driven by a line spec's `scoring` block.

Replaces the per-product CAT_BASE / FAMILIES / BOOST / PENALTY dicts that were copy-pasted into
scripts/enrich_uae_optical_buyers.py and scripts/enrich_uae_buyers.py. Same formula
(base + 6*boost_hits - 12*penalty_hits, clamped 0..100); the taxonomy now lives in config, not code.
"""


def score_and_fits(company, categories, cfg):
    """Score a buyer 0-100 for a product line + return which families fit.

    cfg = {cat_base:{label:int}, families_for:{label:[family]}, boost:[str], penalty:[str],
           default_base:int}. `categories` is the buyer's directory category labels.
    """
    cfg = cfg or {}
    cats = categories or []
    default_base = int(cfg.get("default_base", 55))
    cat_base = cfg.get("cat_base") or {}
    boost = cfg.get("boost") or []
    penalty = cfg.get("penalty") or []
    families_for = cfg.get("families_for") or {}
    base = max((cat_base.get(c, default_base) for c in cats), default=default_base)
    name = (company or "").lower()
    s = base + 6 * sum(1 for k in boost if k in name) - 12 * sum(1 for k in penalty if k in name)
    fams = []
    for c in cats:
        for fam in families_for.get(c, []):
            if fam not in fams:
                fams.append(fam)
    return max(0, min(100, s)), fams


def bulk_likely(company, signals):
    """True if the company name carries a bulk-buyer signal (distributor / wholesale / FZE / ...).
    Matched against " <lowercased name> " so leading/trailing tokens (e.g. ' fze') hit."""
    lo = " " + (company or "").lower() + " "
    return any(k in lo for k in (signals or []))
