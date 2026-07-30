"""Safety-net: force every stored string to Latin/English (no Georgian/Persian script).

    ./.venv/bin/python scripts/anglicize.py

Walks all docs/research/*.json and the live DB (Lead/Product/Supplier), and for any
string that still contains Georgian or Persian/Arabic script, replaces it with the
English/transliterated form via geo_en. Idempotent - safe to re-run. Harvesters already
anglicize at source; this is the belt-and-suspenders pass for older data.
"""
import glob
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from geo_en import fa_translit, geo_to_en  # noqa: E402

KA = re.compile(r"[Ⴀ-ჿ]")
FA = re.compile(r"[؀-ۿ]")
BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def fix_str(v):
    if not isinstance(v, str):
        return v
    if KA.search(v):
        v = geo_to_en(v)
    if FA.search(v):
        v = fa_translit(v)
    return v


def walk(v):
    if isinstance(v, str):
        return fix_str(v)
    if isinstance(v, list):
        return [walk(x) for x in v]
    if isinstance(v, dict):
        return {k: walk(x) for k, x in v.items()}
    return v


def anglicize_files():
    for f in sorted(glob.glob(os.path.join(BASE, "docs", "research", "*.json"))):
        d = json.load(open(f, encoding="utf-8"))
        before = len(KA.findall(json.dumps(d, ensure_ascii=False))) + \
            len(FA.findall(json.dumps(d, ensure_ascii=False)))
        d = walk(d)
        json.dump(d, open(f, "w", encoding="utf-8"), indent=2, ensure_ascii=False)
        after = len(KA.findall(json.dumps(d, ensure_ascii=False))) + \
            len(FA.findall(json.dumps(d, ensure_ascii=False)))
        print(f"  {os.path.basename(f):34} {before:>5} -> {after} non-latin")


def anglicize_db():
    from sqlmodel import Session, select  # noqa: E402
    from app.db import engine  # noqa: E402
    from app.models import Lead, Product, Supplier  # noqa: E402

    fields = {
        Lead: ["buyer_company", "product", "spec", "notes", "dest_city", "contact_name"],
        Product: ["name", "spec", "origin_region"],
        Supplier: ["name", "city", "contact"],
    }
    with Session(engine) as s:
        n = 0
        for model, cols in fields.items():
            for row in s.exec(select(model)).all():
                changed = False
                for c in cols:
                    val = getattr(row, c, None)
                    if isinstance(val, str) and (KA.search(val) or FA.search(val)):
                        setattr(row, c, fix_str(val))
                        changed = True
                if changed:
                    if hasattr(row, "name_normalized"):
                        row.name_normalized = re.sub(r"\s+", " ", (row.name or "").strip().lower())
                    s.add(row)
                    n += 1
            s.commit()
        print(f"  DB rows anglicized: {n}")


if __name__ == "__main__":
    print("=== files ===")
    anglicize_files()
    print("=== database ===")
    anglicize_db()
