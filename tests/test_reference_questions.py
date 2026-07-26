"""Etape 0 — le parseur des questions de reference.

Le harnais mesure ; s'il se trompe, la mesure ment. Ces tests portent sur un
extrait fige, independant de l'export : ils doivent rester verts meme si
l'export n'est pas monte sur la machine.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from harness.reference_questions import (
    NB_QUESTIONS_ATTENDU,
    MAX_LIGNES_CONTRAINTES,
    ReferenceQuestion,
    _valeurs_de_controle,
    checks_de_forme,
    load_reference_questions,
    parse_questions,
    resolve,
)

EXTRAIT = """# Questions de référence — jeu d'évaluation

3 questions, avec le SQL attendu et la réponse réellement obtenue.

---

## Q1 — facile · agrégat simple
> **Quel est le chiffre d'affaires total de 2025 ?**

```sql
SELECT ROUND(SUM(revenue), 2) AS ca FROM sales_daily
WHERE date BETWEEN DATE '2025-01-01' AND DATE '2025-12-31';
```
**Réponse : 10 186 669,50 €**

---

## Q5 — moyen · jointure de deux faits de grains différents
> **Quel est le panier article moyen par type de magasin en 2025 ?**

```sql
WITH q AS (
  SELECT store_id, date, SUM(quantity) AS qty FROM sales_daily GROUP BY store_id, date
)
SELECT st.store_type, ROUND(SUM(q.qty) / SUM(t.nb_tickets), 2) AS panier_article
FROM q
JOIN traffic_daily t ON t.store_id = q.store_id AND t.date = q.date
JOIN stores st ON st.store_id = q.store_id
GROUP BY st.store_type;
```
| store_type | panier_article |
|---|---:|
| online | 2,76 |

> **Le piège :** il faut agréger `sales_daily` au grain magasin × jour **avant**
> de joindre `traffic_daily`, sinon `nb_tickets` est dupliqué 60 fois.

---

## Q10 — difficile · la question qui justifie le modèle
> **Quel est l'uplift des campagnes de type « produits » sur les SKU ciblés,
> comparé aux 4 semaines précédant la campagne ?**

```sql
SELECT campaign_name FROM promo_calendar WHERE promo_type = 'produits';
```
| campaign_name | uplift |
|---|---:|
| Black Friday 2025 | ×1,96 |
"""


# ---- decoupage -----------------------------------------------------------


def test_les_trois_questions_sont_trouvees() -> None:
    questions = parse_questions(EXTRAIT)
    assert [q.id for q in questions] == ["Q1", "Q5", "Q10"]
    assert [q.numero for q in questions] == [1, 5, 10]


def test_la_question_est_nettoyee_du_gras() -> None:
    q1, *_ = parse_questions(EXTRAIT)
    assert q1.question == "Quel est le chiffre d'affaires total de 2025 ?"


def test_une_question_sur_deux_lignes_est_recollee() -> None:
    """Q10 court sur deux lignes de citation : les couper perdrait la moitie
    de la question, donc le sens."""
    q10 = parse_questions(EXTRAIT)[2]
    assert q10.question.startswith("Quel est l'uplift des campagnes")
    assert q10.question.endswith("comparé aux 4 semaines précédant la campagne ?")


def test_le_sql_est_capture_entier() -> None:
    q5 = parse_questions(EXTRAIT)[1]
    assert q5.sql.startswith("WITH q AS")
    assert "JOIN stores st" in q5.sql
    assert "```" not in q5.sql


def test_le_perimetre_se_deduit_du_sql() -> None:
    """Chaque question fournit gratuitement son allowlist. La CTE `q` ne doit
    pas y figurer : ce n'est pas une table."""
    q5 = parse_questions(EXTRAIT)[1]
    assert q5.tables == {"sales_daily", "traffic_daily", "stores"}
    assert "q" not in q5.tables


def test_la_categorie_est_conservee() -> None:
    q5 = parse_questions(EXTRAIT)[1]
    assert "jointure de deux faits" in q5.kind


def test_le_piege_est_capture_quand_il_existe() -> None:
    q1, q5, q10 = parse_questions(EXTRAIT)
    assert "agréger `sales_daily` au grain magasin" in q5.trap
    assert q1.trap == "" and q10.trap == ""


# ---- refus bruyants ------------------------------------------------------


def test_document_sans_question_refuse() -> None:
    with pytest.raises(ValueError, match="aucune section"):
        parse_questions("# Un titre\n\nDu texte, aucune question.\n")


def test_question_sans_sql_refusee() -> None:
    with pytest.raises(ValueError, match="Q1 : aucun bloc SQL"):
        parse_questions("## Q1 — facile · x\n> **Une question ?**\n\nPas de SQL.\n")


def test_question_sans_citation_refusee() -> None:
    with pytest.raises(ValueError, match="aucune question"):
        parse_questions("## Q1 — facile · x\n\n```sql\nSELECT 1;\n```\n")


def test_compte_different_de_treize_refuse(tmp_path: Path) -> None:
    """Le garde-fou qui compte : on ne mesure rien sur une base mouvante."""
    (tmp_path / "questions_reference.md").write_text(EXTRAIT, encoding="utf-8")
    with pytest.raises(ValueError, match=f"3 questions trouvees, {NB_QUESTIONS_ATTENDU}"):
        load_reference_questions(tmp_path, tmp_path / "absente.duckdb")


def test_export_absent_refuse(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="export incomplet"):
        load_reference_questions(tmp_path, tmp_path / "x.duckdb")


def test_base_absente_indique_comment_la_construire(tmp_path: Path) -> None:
    q = ReferenceQuestion(id="Q1", kind="x", question="?", sql="SELECT 1")
    with pytest.raises(FileNotFoundError, match="harness.load_base"):
        resolve([q], tmp_path / "absente.duckdb")


def test_sql_de_reference_invalide_refuse(tmp_path: Path) -> None:
    """Si la requete de reference ne s'execute pas, l'export a derive."""
    duckdb = pytest.importorskip("duckdb")
    base = tmp_path / "vide.duckdb"
    duckdb.connect(str(base)).close()

    q = ReferenceQuestion(id="Q1", kind="x", question="?", sql="SELECT * FROM table_absente")
    with pytest.raises(ValueError, match="Q1 : le SQL de reference ne s'execute pas"):
        resolve([q], base)


# ---- choix des valeurs de controle ---------------------------------------


def test_resultat_court_exige_toutes_les_lignes() -> None:
    valeurs = _valeurs_de_controle(["type", "panier"], [["online", 2.76], ["petit", 2.06]])
    assert valeurs == [2.76, 2.06]


def test_resultat_long_exige_la_premiere_ligne_et_les_extremums() -> None:
    """Le cas Q7 : trie par jour, la premiere ligne est lundi. Sans l'extremum,
    le controle validerait une reponse qui rate le samedi."""
    lignes = [[j, 100.0 * j] for j in range(1, 8)]
    valeurs = _valeurs_de_controle(["jour", "ca"], lignes)
    assert 1.0 in valeurs and 100.0 in valeurs  # la premiere ligne
    assert 700.0 in valeurs  # le maximum : la vraie reponse


def test_une_colonne_constante_n_est_pas_un_extremum() -> None:
    lignes = [[2025, i * 10.0] for i in range(1, 8)]
    valeurs = _valeurs_de_controle(["annee", "ca"], lignes)
    assert valeurs.count(2025.0) == 1  # pas ajoutee une seconde fois


def test_les_lignes_sans_nombre_rendent_leur_texte() -> None:
    valeurs = _valeurs_de_controle(["sku", "date"], [["SKU023", "2024-03-01"]])
    assert valeurs == ["SKU023", "2024-03-01"]


def test_resultat_vide_ne_produit_aucun_controle() -> None:
    assert _valeurs_de_controle(["a"], []) == []


# ---- controles de forme derives de la reference --------------------------


def _question(columns: list[str], rows: list[list[object]]) -> ReferenceQuestion:
    q = ReferenceQuestion(id="Q6", kind="x", question="?", sql="SELECT 1")
    q.columns, q.rows = columns, rows
    return q


def test_la_forme_contraint_les_colonnes_et_les_lignes() -> None:
    """Les deux controles qui adressent le mode de defaillance dominant. Ils
    sont **contraignants malgre leurs faux positifs** : mesure sur les 13
    questions, cinq blocages a raison pour un a tort, et `faux` tombe de 6 a 1.
    Voir le faux positif nomme dans `checks_de_forme` — Q6."""
    forme = checks_de_forme(_question(["sku", "nom", "univers", "ca"], [["S1", "n", "u", 1.0]]))
    assert [c["kind"] for c in forme] == ["column_count", "row_count"]
    assert forme[0]["min_columns"] == 4
    assert "max_columns" not in forme[0]  # une reponse plus riche reste juste


def test_un_resultat_trop_long_ne_contraint_pas_les_lignes() -> None:
    """Exiger un compte exact sur un gros resultat bloquerait une reponse
    justement resumee. Les colonnes restent contraintes : leur nombre ne
    depend pas de la longueur."""
    lignes = [[i] for i in range(MAX_LIGNES_CONTRAINTES + 1)]
    forme = checks_de_forme(_question(["a"], lignes))
    assert [c["kind"] for c in forme] == ["column_count"]


def test_le_compte_de_lignes_est_exact() -> None:
    forme = checks_de_forme(_question(["jour", "ca"], [[j, 1.0] for j in range(7)]))
    lignes = next(c for c in forme if c["kind"] == "row_count")
    assert lignes["min_rows"] == 7 and lignes["max_rows"] == 7
