"""Etape 2 — la these entiere, sous forme executable.

Un modele produit un panier article faux. Aucune prose ne le trahit : la phrase
est bien tournee, le tableau est propre, seul le chiffre est absurde. Deux
agents differents sont tombes dans le meme piege, de deux facons opposees :

    data-analyst-agent    0,047   ->  60 fois trop PETIT
    ce moteur             44,5    ->  20 fois trop GRAND

Ajouter au prompt « verifie ton ordre de grandeur » a ete essaye, et mesure sans
effet. Ces tests verifient qu'un controle **de code** fait ce qu'une consigne ne
fait pas : le chiffre faux n'atteint ni le plan comme resultat, ni les taches
suivantes, ni la synthese.

Aucun LLM : le provider est scripte pour rendre exactement la requete naive.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from engine.checks.verify import apply_gate, verify
from engine.config import Config, FilesPackConfig, SqlPackConfig, ToolpackConfig
from engine.loop import run
from engine.plan import PlanStore
from engine.providers import FakeProvider
from engine.schemas import Brief, Check, Plan, Task, TaskResult
from engine.synthesize import render_logbook

duckdb = pytest.importorskip("duckdb")

from engine.toolpacks.sql import SqlToolPack  # noqa: E402

# Le panier article vaut entre 2,06 (petit magasin) et 2,76 (en ligne).
# Ces bornes viennent de la table « Ordres de grandeur » du dictionnaire de la
# source : elles n'exigent AUCUNE reponse de reference, donc ce controle
# fonctionne aussi sur une question inedite.
PANIER = Check(
    kind="magnitude",
    label="panier article",
    column="panier_article",
    low=2.0,
    high=3.0,
)

SQL_NAIF = """
SELECT st.store_type, SUM(s.quantity) / SUM(t.nb_tickets) AS panier_article
FROM sales_daily s
JOIN traffic_daily t ON t.store_id = s.store_id AND t.date = s.date
JOIN stores st ON st.store_id = s.store_id
GROUP BY st.store_type
"""

SQL_CORRECT = """
WITH q AS (
  SELECT store_id, date, SUM(quantity) AS qty FROM sales_daily GROUP BY store_id, date
)
SELECT st.store_type, ROUND(SUM(q.qty) / SUM(t.nb_tickets), 2) AS panier_article
FROM q
JOIN traffic_daily t ON t.store_id = q.store_id AND t.date = q.date
JOIN stores st ON st.store_id = q.store_id
WHERE t.nb_tickets > 0
GROUP BY st.store_type
"""


@pytest.fixture
def base(tmp_path: Path) -> Path:
    """Une mini-base au schema de Maxizoo, avec de vrais chiffres.

    Deux magasins, deux SKU, deux jours. Panier article reel : 2,5 articles par
    ticket — dans l'ordre de grandeur documente. La requete naive duplique
    `nb_tickets` une fois par SKU : avec 2 SKU, elle rend 1,25.

    C'est la forme exacte du piege documente par l'export (« sinon `nb_tickets`
    est duplique 60 fois, une fois par SKU, et le panier est divise par 60 »).
    Sur la vraie base, DAA rendait ainsi 0,047. Notre agent, lui, a produit 44,5
    par un autre chemin — plus grand, pas plus petit. **Le meme controle attrape
    les deux** : c'est tout l'interet de verifier la valeur plutot que la forme
    de la requete.
    """
    chemin = tmp_path / "mini.duckdb"
    con = duckdb.connect(str(chemin))
    con.execute("CREATE TABLE stores (store_id TEXT, store_type TEXT)")
    con.execute("CREATE TABLE sales_daily (store_id TEXT, date DATE, sku_id TEXT, quantity INTEGER)")
    con.execute("CREATE TABLE traffic_daily (store_id TEXT, date DATE, nb_tickets INTEGER)")
    con.execute("INSERT INTO stores VALUES ('S1','grand'), ('S2','petit')")
    for magasin in ("S1", "S2"):
        for jour in ("2025-01-01", "2025-01-02"):
            con.execute(
                f"INSERT INTO traffic_daily VALUES ('{magasin}', DATE '{jour}', 6)"
            )
            for sku, qte in (("A", 8), ("B", 7)):
                con.execute(
                    f"INSERT INTO sales_daily VALUES "
                    f"('{magasin}', DATE '{jour}', '{sku}', {qte})"
                )
    con.close()
    return chemin


def config_pour(base: Path) -> Config:
    cfg = Config(
        toolpack=ToolpackConfig(
            files=FilesPackConfig(root=str(base.parent)),
            sql=SqlPackConfig(database=str(base)),
        )
    )
    cfg.root = base.parent
    return cfg


def plan_q5(base: Path, avec_grain: bool = False) -> Plan:
    """Deux taches : le panier, puis une tache qui en depend."""
    scope = {"tables": ["sales_daily", "traffic_daily", "stores"]}
    if avec_grain:
        scope |= {"grain": ["store_id", "date"], "aggregate_before_join": ["sales_daily"]}
    return Plan(
        slug="q5",
        title="Panier article",
        toolpack="sql",
        status="ready",
        brief=Brief(
            question="Quel est le panier article moyen par type de magasin ?",
            tasks=[
                Task(
                    id="t1",
                    title="Panier article par type de magasin",
                    question="Quel est le panier article moyen par type de magasin ?",
                    scope=scope,
                    sanity_check="le panier article vaut entre 2 et 3 articles",
                    checks=[PANIER],
                ),
                Task(
                    id="t2",
                    title="Comparer au budget",
                    question="Le panier est-il au-dessus de l'objectif ?",
                    scope=scope,
                    depends_on=["t1"],
                ),
            ],
        ),
    )


def provider_qui_rend(sql: str) -> FakeProvider:
    return FakeProvider(
        script=[
            {"action": "call_tool", "tool": "query", "args": {"sql": sql}},
            {
                "action": "final_answer",
                "summary": "Le panier article moyen ressort du calcul ci-dessus.",
                "insight": "Le panier article est calcule par type de magasin.",
                "influence": "sert de base a la comparaison au budget",
            },
        ]
        * 2
    )


# ---- le chiffre faux est bien produit ------------------------------------


def test_la_requete_naive_produit_un_chiffre_absurde(base: Path) -> None:
    """Le point de depart : rien dans la reponse ne signale l'erreur."""
    pack = SqlToolPack(config_pour(base), {"tables": ["sales_daily", "traffic_daily", "stores"]})
    resultat = pack.call("query", {"sql": SQL_NAIF})

    assert resultat.ok  # la requete tourne, elle ne leve rien, rien ne signale l'erreur
    paniers = [ligne[1] for ligne in pack.tables_produites[0]["rows"]]
    # divise par le nombre de SKU (2 ici, 60 sur la vraie base)
    assert all(p < 2.0 for p in paniers), f"le piege ne se reproduit pas : {paniers}"


def test_la_requete_correcte_tombe_dans_l_ordre_de_grandeur(base: Path) -> None:
    pack = SqlToolPack(config_pour(base), {"tables": ["sales_daily", "traffic_daily", "stores"]})
    pack.call("query", {"sql": SQL_CORRECT})
    paniers = [ligne[1] for ligne in pack.tables_produites[0]["rows"]]
    assert all(2.0 <= p <= 3.0 for p in paniers), paniers


# ---- le controle attrape ce que la prose cache --------------------------


def test_le_controle_rejette_le_chiffre_absurde(base: Path) -> None:
    pack = SqlToolPack(config_pour(base), {"tables": ["sales_daily", "traffic_daily", "stores"]})
    pack.call("query", {"sql": SQL_NAIF})

    rapport = verify([PANIER], prose="Le panier moyen est correct.", tables=pack.tables_produites)
    assert rapport.failed
    assert "hors de l'ordre de grandeur" in rapport.failures[0].detail
    assert "trop petit" in rapport.failures[0].detail


@pytest.mark.parametrize(
    "produit,attendu",
    [(0.047, "trop petit"), (44.5, "trop grand"), (1.25, "trop petit")],
)
def test_le_meme_controle_attrape_les_deux_directions(produit: float, attendu: str) -> None:
    """0,047 (data-analyst-agent) et 44,5 (ce moteur) sont deux erreurs opposees
    sur la meme question. Un controle de FORME de requete n'en attraperait
    qu'une ; un controle de VALEUR les attrape toutes les deux."""
    tables = [{"columns": ["store_type", "panier_article"], "rows": [["online", produit]]}]
    rapport = verify([PANIER], prose="Voici le panier.", tables=tables)
    assert rapport.failed and attendu in rapport.failures[0].detail


def test_le_controle_accepte_le_chiffre_juste(base: Path) -> None:
    """Un controle qui refuserait le juste serait pire que pas de controle."""
    pack = SqlToolPack(config_pour(base), {"tables": ["sales_daily", "traffic_daily", "stores"]})
    pack.call("query", {"sql": SQL_CORRECT})
    assert not verify([PANIER], tables=pack.tables_produites).failed


def test_une_affirmation_chiffree_sans_donnee_est_rejetee() -> None:
    """« Le panier est de 2,5 » sans requete derriere ne doit pas passer."""
    rapport = verify([PANIER], prose="Le panier article est de 2,5.", tables=[])
    assert rapport.failed and "aucune donnee a verifier" in rapport.failures[0].detail


# ---- la porte : le chiffre faux n'entre pas dans le plan ----------------


def test_la_porte_bloque_la_tache(base: Path) -> None:
    pack = SqlToolPack(config_pour(base), {"tables": ["sales_daily", "traffic_daily", "stores"]})
    pack.call("query", {"sql": SQL_NAIF})
    task = plan_q5(base).brief.tasks[0]
    resultat = TaskResult(task_id="t1", status="done", summary="tout va bien", insight="ok")

    apres = apply_gate(resultat, task, pack)
    assert apres.status == "blocked"
    assert "panier article" in apres.reason
    assert [o.passed for o in apres.checks] == [False]


def test_la_porte_non_contraignante_journalise_sans_arreter(base: Path) -> None:
    """Pour mesurer ce que les controles attraperaient avant de les activer."""
    pack = SqlToolPack(config_pour(base), {"tables": ["sales_daily", "traffic_daily", "stores"]})
    pack.call("query", {"sql": SQL_NAIF})
    task = plan_q5(base).brief.tasks[0]
    resultat = TaskResult(task_id="t1", status="done", insight="ok")

    apres = apply_gate(resultat, task, pack, binding=False)
    assert apres.status == "done"
    assert apres.checks and not apres.checks[0].passed  # trace gardee


# ---- de bout en bout : le plan, les dependantes, la synthese ------------


def test_de_bout_en_bout_le_chiffre_faux_ne_circule_jamais(base: Path, tmp_path: Path) -> None:
    """Le test qui porte la these.

    L'agent rend la requete naive. On verifie les quatre proprietes qui font
    qu'un controle est contraignant et pas decoratif.
    """
    store = PlanStore.create(tmp_path / "inv", plan_q5(base))
    run(store, config_pour(base), provider_qui_rend(SQL_NAIF))
    plan = store.load()

    # 1. la tache n'est pas `done`
    assert plan.task("t1").state == "blocked"

    # 2. le plan dit POURQUOI, avec la valeur produite et l'intervalle attendu
    finding = plan.finding("t1")
    (echec,) = [o for o in finding.checks if not o.passed]
    assert "hors de l'ordre de grandeur attendu [2.0, 3.0]" in echec.detail

    # 3. la tache dependante n'est jamais devenue executable
    assert plan.task("t2").state == "pending"
    assert plan.next_runnable() is None

    # 4. la synthese ne peut pas citer le chiffre : elle ne recoit que le motif
    logbook = render_logbook(plan)
    assert "AUCUN RESULTAT EXPLOITABLE" in logbook
    assert "N'invente pas sa reponse" in logbook
    assert "tout va bien" not in logbook  # la prose du modele ne passe pas


def test_de_bout_en_bout_la_requete_correcte_passe(base: Path, tmp_path: Path) -> None:
    """Le pendant indispensable : la porte laisse passer le travail juste."""
    store = PlanStore.create(tmp_path / "inv", plan_q5(base))
    run(store, config_pour(base), provider_qui_rend(SQL_CORRECT))
    plan = store.load()

    assert plan.task("t1").state == "done"
    assert plan.task("t2").state == "done"
    assert all(o.passed for o in plan.finding("t1").checks)


def test_le_perimetre_refuse_la_requete_naive_avant_de_l_executer(base: Path, tmp_path: Path) -> None:
    """Deux lignes de defense, pas une : le controle de grain agit AVANT
    l'execution, le controle chiffre APRES. La premiere fait gagner un tour,
    la seconde est celle sur laquelle on compte."""
    store = PlanStore.create(tmp_path / "inv", plan_q5(base, avec_grain=True))
    provider = provider_qui_rend(SQL_NAIF)
    run(store, config_pour(base), provider, single_step=True)

    _, _, dernier = provider.prompts[-1]
    assert "est jointe sans avoir ete agregee" in dernier
    assert "WITH q AS" in dernier  # le refus nomme le correctif
