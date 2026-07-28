"""Load a few realistic demands & offers so you can see matching work.

Run with:  make seed   (or:  python -m app.seed)
This wipes existing rows first — it's for demo/testing, not production data.
"""
from sqlmodel import Session, select

from .config import MATCH_THRESHOLD
from .db import engine, init_db
from .matching import score_pair
from .models import Demand, Match, Offer

SAMPLE_DEMANDS = [
    dict(product="Steel rebar 12mm", category="metals", spec="grade B500B",
         quantity=50, unit="ton", target_price=650, currency="USD",
         location="Dubai", contact="Buyer A · +971-50-000-0001"),
    dict(product="Basmati rice", category="food", spec="1121 sella",
         quantity=200, unit="ton", target_price=1100, currency="USD",
         location="Jebel Ali", contact="Buyer B · +98-912-000-0002"),
    dict(product="Used iPhone 13", category="electronics", spec="128GB grade A",
         quantity=100, unit="pcs", target_price=380, currency="USD",
         location="Tehran", contact="Buyer C"),
]

SAMPLE_OFFERS = [
    dict(product="Rebar steel 12mm", category="metals", spec="B500B grade",
         quantity=80, unit="ton", price=630, currency="USD",
         location="Dubai / UAE", contact="Seller X · trader@example.com"),
    dict(product="Sella basmati rice 1121", category="food", spec="premium sella",
         quantity=150, unit="ton", price=1080, currency="USD",
         location="Jebel Ali", contact="Seller Y"),
    dict(product="Copper cathode", category="metals", spec="99.99% purity",
         quantity=25, unit="ton", price=8500, currency="USD",
         location="Bandar Abbas", contact="Seller Z"),
]


def run() -> None:
    init_db()
    with Session(engine) as s:
        for table in (Match, Demand, Offer):
            for row in s.exec(select(table)).all():
                s.delete(row)
        s.commit()

        demands = [Demand(**d) for d in SAMPLE_DEMANDS]
        offers = [Offer(**o) for o in SAMPLE_OFFERS]
        for d in demands:
            s.add(d)
        for o in offers:
            s.add(o)
        s.commit()
        for d in demands:
            s.refresh(d)
        for o in offers:
            s.refresh(o)

        n = 0
        for d in demands:
            for o in offers:
                score, reasons = score_pair(d, o)
                if score >= MATCH_THRESHOLD:
                    s.add(Match(demand_id=d.id, offer_id=o.id, score=score, reasons=reasons))
                    n += 1
        s.commit()

    print(f"Seeded {len(SAMPLE_DEMANDS)} demands, {len(SAMPLE_OFFERS)} offers, {n} matches.")


if __name__ == "__main__":
    run()
