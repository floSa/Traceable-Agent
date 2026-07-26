"""Etape 1 — le perimetre par tache : dictionnaire, grain, agregation prealable.

Le piege de grain est celui qui a fait rendre un panier article **60 fois trop
petit** a l'agent de data-analyst-agent, et **20 fois trop grand** au notre. Il
n'est pas attrapable par une allowlist : la requete naive n'utilise que des
tables autorisees. Ces tests portent sur le fil-piege qui l'attrape, et sur ses
limites — un controle qui refuserait une requete correcte serait pire que rien.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from engine.config import Config, SqlPackConfig, ToolpackConfig
from engine.toolpacks.base import Ok, Refus
from engine.toolpacks.sql import (
    agrege_au_grain,
    create_demo_database,
    hors_agregation,
    referenced_tables,
)

duckdb = pytest.importorskip("duckdb")

from engine.toolpacks.sql import SqlToolPack  # noqa: E402

SCOPE = {
    "tables": ["sales_daily", "traffic_daily", "stores"],
    "grain": ["store_id", "date"],
    "aggregate_before_join": ["sales_daily"],
}

NAIF = """
SELECT st.store_type, SUM(s.quantity) / SUM(t.nb_tickets) AS panier
FROM sales_daily s
JOIN traffic_daily t ON t.store_id = s.store_id AND t.date = s.date
JOIN stores st ON st.store_id = s.store_id
GROUP BY st.store_type
"""

CORRECT = """
WITH q AS (
  SELECT store_id, date, SUM(quantity) AS qty FROM sales_daily GROUP BY store_id, date
)
SELECT st.store_type, SUM(q.qty) / SUM(t.nb_tickets) AS panier
FROM q
JOIN traffic_daily t ON t.store_id = q.store_id AND t.date = q.date
JOIN stores st ON st.store_id = q.store_id
GROUP BY st.store_type
"""


@pytest.fixture
def base(tmp_path: Path) -> Path:
    """Une base au schema de Maxizoo, vide : on teste `validate`, pas les chiffres."""
    chemin = tmp_path / "mini.duckdb"
    con = duckdb.connect(str(chemin))
    con.execute("CREATE TABLE stores (store_id TEXT, store_type TEXT)")
    con.execute("CREATE TABLE sales_daily (store_id TEXT, date DATE, sku_id TEXT, quantity INTEGER)")
    con.execute("CREATE TABLE traffic_daily (store_id TEXT, date DATE, nb_tickets INTEGER)")
    con.close()
    return chemin


def pack_pour(base: Path, scope: dict, dictionnaire: Path | None = None) -> SqlToolPack:
    cfg = Config(
        toolpack=ToolpackConfig(
            sql=SqlPackConfig(
                database=str(base),
                dictionary=str(dictionnaire) if dictionnaire else None,
            )
        )
    )
    cfg.root = base.parent
    return SqlToolPack(cfg, scope)


# ---- l'analyse de forme, isolement --------------------------------------


def test_agrege_au_grain_reconnait_le_group_by() -> None:
    assert agrege_au_grain("SELECT store_id, date, SUM(q) FROM t GROUP BY store_id, date", ["store_id", "date"])
    assert not agrege_au_grain("SELECT store_id, SUM(q) FROM t GROUP BY store_id", ["store_id", "date"])
    assert not agrege_au_grain("SELECT * FROM t", ["store_id"])
    assert not agrege_au_grain("SELECT * FROM t GROUP BY x", [])


def test_hors_agregation_retire_les_blocs_agreges() -> None:
    reste = hors_agregation(CORRECT, ["store_id", "date"])
    assert "sales_daily" not in reste  # il est dans le CTE agrege
    assert "traffic_daily" in reste

    reste_naif = hors_agregation(NAIF, ["store_id", "date"])
    assert "sales_daily" in reste_naif  # jamais agrege


def test_hors_agregation_sans_grain_ne_retire_rien() -> None:
    assert hors_agregation(CORRECT, []) == CORRECT


# ---- le refus, et surtout ses limites -----------------------------------


def test_la_requete_naive_est_refusee(base: Path) -> None:
    verdict = pack_pour(base, SCOPE).validate("query", {"sql": NAIF})
    assert isinstance(verdict, Refus)
    assert "sales_daily" in verdict.reason and "dupliquee" in verdict.reason


def test_le_refus_nomme_le_correctif(base: Path) -> None:
    """Un refus qui n'indique pas quoi faire fait tourner l'agent en rond."""
    verdict = pack_pour(base, SCOPE).validate("query", {"sql": NAIF})
    assert isinstance(verdict, Refus)
    assert "WITH q AS" in verdict.reason
    assert "GROUP BY store_id, date" in verdict.reason


def test_la_requete_correcte_passe(base: Path) -> None:
    """Le point le plus important : un controle qui refuse le juste est nuisible."""
    assert isinstance(pack_pour(base, SCOPE).validate("query", {"sql": CORRECT}), Ok)


def test_une_requete_sans_jointure_passe(base: Path) -> None:
    """Sans jointure, aucune mesure ne peut etre dupliquee : Q1 ne doit pas tomber."""
    sql = "SELECT SUM(quantity) FROM sales_daily"
    assert isinstance(pack_pour(base, SCOPE).validate("query", {"sql": sql}), Ok)


def test_sans_declaration_de_grain_aucun_controle(base: Path) -> None:
    """Le controle est declaratif : pas de scope, pas de refus."""
    nu = {"tables": SCOPE["tables"]}
    assert isinstance(pack_pour(base, nu).validate("query", {"sql": NAIF}), Ok)


def test_une_sous_requete_agregee_compte_aussi(base: Path) -> None:
    """L'agregation peut vivre dans une sous-requete plutot qu'un CTE."""
    sql = """
    SELECT st.store_type, SUM(q.qty) / SUM(t.nb_tickets) AS panier
    FROM (SELECT store_id, date, SUM(quantity) AS qty FROM sales_daily
          GROUP BY store_id, date) q
    JOIN traffic_daily t ON t.store_id = q.store_id AND t.date = q.date
    JOIN stores st ON st.store_id = q.store_id
    GROUP BY st.store_type
    """
    assert isinstance(pack_pour(base, SCOPE).validate("query", {"sql": sql}), Ok)


# ---- le dictionnaire -----------------------------------------------------


def test_le_dictionnaire_est_injecte_avant_la_question(base: Path, tmp_path: Path) -> None:
    """Les pieges doivent etre connus au moment d'ecrire la requete, pas apres."""
    dico = tmp_path / "dico.md"
    dico.write_text("## Ordres de grandeur\n\nPanier article moyen 2,06 -> 2,76\n", encoding="utf-8")
    description = pack_pour(base, SCOPE, dico).describe()
    assert "Panier article moyen 2,06 -> 2,76" in description
    assert "Dictionnaire de la source" in description
    # il precede la liste des outils, donc la question : c'est tout l'interet
    assert description.index("Dictionnaire de la source") < description.index("Outils :")


def test_le_dictionnaire_est_tronque(base: Path, tmp_path: Path) -> None:
    dico = tmp_path / "dico.md"
    dico.write_text("x" * 50_000, encoding="utf-8")
    cfg = Config(
        toolpack=ToolpackConfig(
            sql=SqlPackConfig(database=str(base), dictionary=str(dico), max_dictionary_chars=500)
        )
    )
    cfg.root = tmp_path
    description = SqlToolPack(cfg, SCOPE).describe()
    assert "dictionnaire tronque" in description


def test_un_dictionnaire_absent_ne_casse_rien(base: Path, tmp_path: Path) -> None:
    description = pack_pour(base, SCOPE, tmp_path / "fantome.md").describe()
    assert "Dictionnaire de la source" not in description


def test_le_grain_est_annonce_dans_la_description(base: Path) -> None:
    description = pack_pour(base, SCOPE).describe()
    assert "Grain impose a cette tache : `store_id`, `date`" in description
    assert "AVANT de joindre" in description


# ---- la regression trouvee sur les vraies requetes ----------------------


def test_extract_from_nest_pas_pris_pour_une_table() -> None:
    """`EXTRACT(YEAR FROM s.date)` faisait refuser une requete parfaitement
    legitime : `s.date` etait compte comme une table hors perimetre."""
    sql = "SELECT EXTRACT(YEAR FROM s.date) AS annee, SUM(s.revenue) FROM sales_daily s GROUP BY 1"
    assert referenced_tables(sql) == {"sales_daily"}


def test_une_requete_avec_extract_passe_la_validation(base: Path) -> None:
    sql = "SELECT EXTRACT(YEAR FROM date) AS annee, SUM(quantity) FROM sales_daily GROUP BY 1"
    assert isinstance(pack_pour(base, SCOPE).validate("query", {"sql": sql}), Ok)


def test_les_resultats_structures_sont_conserves(tmp_path: Path) -> None:
    """Un controle chiffre porte sur le resultat de la base, jamais sur la prose."""
    base = create_demo_database(tmp_path / "demo.duckdb")
    pack = pack_pour(base, {"tables": ["ventes"]})
    pack.call("query", {"sql": "SELECT segment, montant FROM ventes ORDER BY montant DESC"})
    assert pack.tables_produites[0]["columns"] == ["segment", "montant"]
    assert pack.tables_produites[0]["rows"][0] == ["Entreprise", 1200.0]


def test_les_decimaux_deviennent_des_nombres(tmp_path: Path) -> None:
    """DuckDB rend des Decimal sur les agregats arrondis : sans conversion, le
    controle ne les voit pas comme des nombres et laisse passer n'importe quoi."""
    base = create_demo_database(tmp_path / "demo.duckdb")
    pack = pack_pour(base, {"tables": ["ventes"]})
    pack.call("query", {"sql": "SELECT ROUND(SUM(montant), 2) AS total FROM ventes"})
    (valeur,) = pack.tables_produites[0]["rows"][0]
    assert isinstance(valeur, float) and valeur == 2850.0
