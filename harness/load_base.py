"""Construit `data/maxizoo.duckdb` a partir de l'export.

L'export n'est pas versionne ici : il vit dans la branche orpheline `export-db`
du depot `sales-ops-planning-poc`. On l'extrait, on charge, et on **refuse
d'ecrire** si un volume ne tombe pas juste — une derive de l'export doit etre
bruyante, jamais silencieuse. Meme discipline que le chargeur de
`data-analyst-agent`.

    uv run python -m harness.load_base --export data/export
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

# Volumes annonces par le MANIFEST.md de l'export (variante complete 5 ans).
VOLUMES_ATTENDUS = {
    "stores": 13,
    "store_hours": 91,
    "products": 60,
    "promo_calendar": 56,
    "promo_scope": 349,
    "inflation": 66,
    "weather": 26_130,
    "traffic_daily": 23_738,
    "sales_daily": 1_363_726,
    "sales_hourly": 244_952,
}
TOTAL_ATTENDU = 1_659_181


def charger(export: Path, cible: Path) -> None:
    import duckdb

    for requis in ("schema.sql", "chargement/duckdb.sql", "dictionnaire.md", "tables"):
        if not (export / requis).exists():
            sys.exit(f"export incomplet : {export / requis} est absent")

    cible.parent.mkdir(parents=True, exist_ok=True)
    cible.unlink(missing_ok=True)

    con = duckdb.connect(str(cible))
    try:
        con.execute(f"SET FILE_SEARCH_PATH='{export}'")
        con.execute((export / "schema.sql").read_text(encoding="utf-8"))
        # Les INSERT referencent `tables/*.csv` en relatif : on prefixe.
        sql = (export / "chargement" / "duckdb.sql").read_text(encoding="utf-8")
        sql = sql.replace("read_csv_auto('tables/", f"read_csv_auto('{export}/tables/")
        for instruction in sql.split(";"):
            if instruction.strip().upper().startswith("INSERT"):
                con.execute(instruction)

        ecarts = []
        for table, attendu in VOLUMES_ATTENDUS.items():
            obtenu = con.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            if obtenu != attendu:
                ecarts.append(f"  {table}: {obtenu} lignes, attendu {attendu}")
        if ecarts:
            con.close()
            cible.unlink(missing_ok=True)
            sys.exit("l'export a derive, base non ecrite :\n" + "\n".join(ecarts))

        total = sum(VOLUMES_ATTENDUS.values())
        print(f"OK  {len(VOLUMES_ATTENDUS)} tables, {total:,} lignes".replace(",", " "))
        if total != TOTAL_ATTENDU:
            print(f"    (le MANIFEST annonce {TOTAL_ATTENDU})")
    finally:
        con.close()

    dictionnaire = cible.parent / "maxizoo_dictionnaire.md"
    shutil.copyfile(export / "dictionnaire.md", dictionnaire)
    print(f"OK  {cible} ({cible.stat().st_size / 1e6:.0f} Mo)")
    print(f"OK  {dictionnaire}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--export", type=Path, default=Path("data/export"))
    parser.add_argument("--out", type=Path, default=Path("data/maxizoo.duckdb"))
    args = parser.parse_args()
    charger(args.export.resolve(), args.out.resolve())


if __name__ == "__main__":
    main()
