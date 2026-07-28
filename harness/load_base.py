"""Construit `data/maxizoo.duckdb` a partir de l'export versionne.

L'export vit dans `data/export/`, **dans ce depot**, au format Parquet — six
megaoctets pour 1,66 million de lignes. C'est ce qui rend le banc reproductible
sur une autre machine sans acces a un second depot.

Le chargeur **refuse d'ecrire** si un volume ne tombe pas juste : une derive de
l'export doit etre bruyante, jamais silencieuse. Meme discipline que le
chargeur de `data-analyst-agent`.

    uv run python -m harness.load_base

Origine des donnees : branche orpheline `export-db` du depot
`sales-ops-planning-poc`, variante complete cinq ans, graine 42. Elles sont
**100 % synthetiques** — aucune donnee reelle d'enseigne — a une exception pres,
la table `weather`, qui porte de la vraie meteo historique d'Open-Meteo.
"""

from __future__ import annotations

import argparse
import re
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


_LECTURE_CSV = re.compile(r"read_csv_auto\('tables/(\w+)\.csv'[^)]*\)")


def source_des_tables(sql: str, export: Path) -> str:
    """Fait pointer les INSERT sur les fichiers reellement presents.

    L'export d'origine ecrit ses INSERT sur les CSV. Ce sont les **Parquet** qui
    sont versionnes ici : meme contenu, 6 Mo au lieu de 59, et duckdb les lit
    nativement. Les CSV restent acceptes s'ils sont la — un export complet
    extrait a la main doit continuer de marcher.
    """

    def remplacer(m: re.Match[str]) -> str:
        table = m.group(1)
        if (export / "tables" / f"{table}.csv").is_file():
            return f"read_csv_auto('{export}/tables/{table}.csv', header=true)"
        return f"read_parquet('{export}/tables/{table}.parquet')"

    return _LECTURE_CSV.sub(remplacer, sql)


def charger(export: Path, cible: Path) -> None:
    import duckdb

    for requis in ("schema.sql", "chargement/duckdb.sql", "dictionnaire.md", "tables"):
        if not (export / requis).exists():
            sys.exit(f"export incomplet : {export / requis} est absent")

    manquantes = [
        t
        for t in VOLUMES_ATTENDUS
        if not (export / "tables" / f"{t}.parquet").is_file()
        and not (export / "tables" / f"{t}.csv").is_file()
    ]
    if manquantes:
        sys.exit("export incomplet, aucune donnee pour : " + ", ".join(manquantes))

    cible.parent.mkdir(parents=True, exist_ok=True)
    cible.unlink(missing_ok=True)

    con = duckdb.connect(str(cible))
    try:
        con.execute(f"SET FILE_SEARCH_PATH='{export}'")
        con.execute((export / "schema.sql").read_text(encoding="utf-8"))
        # Les INSERT referencent `tables/*.csv` en relatif : on les reecrit.
        sql = source_des_tables(
            (export / "chargement" / "duckdb.sql").read_text(encoding="utf-8"), export
        )
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
