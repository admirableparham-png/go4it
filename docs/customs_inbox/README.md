# Customs import data — inbox (paid channel)

Drop **Georgia importer** CSV exports here (from Volza / Trademo / ImportGenius / Seair),
then run `./.venv/bin/python scripts/load_customs.py`. Every Georgian company that has
actually imported one of the listed chemicals becomes a buyer lead (with the shipment
date → `Lead.posted_at`).

**Export one file per chemical, "buyers/importers in Georgia", these HS codes:**

| HS | Chemical |
|----|----------|
| 2807 | Sulfuric acid |
| 2815 (2815.11/.12) | Sodium hydroxide (caustic soda) |
| 2837.11 | Sodium cyanide |
| 2833.25 | Copper sulphate |
| 2833.29 | Zinc sulphate |
| 2832.10 | Sodium sulfite |
| 2830.10 | Sodium sulfide |
| 2827.32 | Aluminium chloride / PAC |
| 3906.90 | Polyacrylamide (anionic + cationic PAM) |
| 2905.19 | MIBC (methyl isobutyl carbinol) |
| 2930.90 / 3824.99 | Xanthates & prepared flotation reagents |

Columns are matched tolerantly — any export with an **importer/consignee** column plus a
**date** and **HS code** works. Extra columns (supplier, quantity, value, email, phone,
website) are captured when present. `*.csv` files here are gitignored (raw paid data);
the parsed result is committed as `docs/research/ge_customs.json`.
