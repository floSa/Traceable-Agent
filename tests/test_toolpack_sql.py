"""Etape 7 — le pack `sql` : il se branche sans que le moteur bouge,
et le moteur tourne sur `files` sans jamais charger duckdb."""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
from conftest import make_plan

from engine.agent import run_task
from engine.config import AgentConfig, Config, SqlPackConfig, ToolpackConfig
from engine.plan import PlanStore
from engine.providers import FakeProvider
from engine.toolpacks.base import Ok, Refus, ToolPack, build_toolpack

duckdb = pytest.importorskip("duckdb")

from engine.toolpacks.sql import SqlToolPack, create_demo_database, strip_sql  # noqa: E402


@pytest.fixture
def base(tmp_path: Path) -> Path:
    return create_demo_database(tmp_path / "data" / "demo.duckdb")


def cfg_for(base: Path, **kw) -> Config:
    c = Config(toolpack=ToolpackConfig(sql=SqlPackConfig(database=str(base), **kw)))
    c.root = base.parent
    return c


@pytest.fixture
def pack(base: Path) -> SqlToolPack:
    return SqlToolPack(cfg_for(base), {"tables": ["ventes"]})


# ---- contrat identique a celui du pack files ----------------------------


def test_le_pack_respecte_le_meme_protocole(pack: SqlToolPack) -> None:
    assert isinstance(pack, ToolPack)
    assert {t.name for t in pack.tools()} == {"schema", "query"}
    assert "ventes" in pack.describe()


# ---- validate : lecture seule -------------------------------------------


@pytest.mark.parametrize(
    "sql",
    [
        "INSERT INTO ventes VALUES (9,'X',1,'T4')",
        "UPDATE ventes SET montant = 0",
        "DELETE FROM ventes",
        "DROP TABLE ventes",
        "CREATE TABLE t AS SELECT 1",
        "ATTACH 'autre.duckdb' AS autre",
        "COPY ventes TO 'sortie.csv'",
        "PRAGMA database_list",
    ],
)
def test_validate_refuse_tout_ce_qui_n_est_pas_select(pack: SqlToolPack, sql: str) -> None:
    assert isinstance(pack.validate("query", {"sql": sql}), Refus)


def test_validate_accepte_select_et_with(pack: SqlToolPack) -> None:
    assert isinstance(pack.validate("query", {"sql": "SELECT * FROM ventes"}), Ok)
    verdict = pack.validate(
        "query", {"sql": "WITH t AS (SELECT * FROM ventes) SELECT count(*) FROM t"}
    )
    assert isinstance(verdict, Ok)  # la CTE `t` n'est pas prise pour une table


def test_validate_refuse_une_table_non_declaree(pack: SqlToolPack) -> None:
    v = pack.validate("query", {"sql": "SELECT * FROM secrets"})
    assert isinstance(v, Refus) and "hors perimetre" in v.reason and "secrets" in v.reason


def test_validate_refuse_une_jointure_vers_une_table_non_declaree(pack: SqlToolPack) -> None:
    v = pack.validate("query", {"sql": "SELECT * FROM ventes JOIN secrets ON true"})
    assert isinstance(v, Refus) and "secrets" in v.reason


def test_validate_refuse_les_fonctions_de_lecture_de_fichiers(pack: SqlToolPack) -> None:
    v = pack.validate("query", {"sql": "SELECT * FROM read_csv('/etc/passwd')"})
    assert isinstance(v, Refus) and "fichiers" in v.reason


def test_validate_refuse_plusieurs_instructions(pack: SqlToolPack) -> None:
    v = pack.validate("query", {"sql": "SELECT 1 FROM ventes; DROP TABLE ventes"})
    assert isinstance(v, Refus) and "seule instruction" in v.reason


def test_validate_refuse_sans_tables_declarees(base: Path) -> None:
    v = SqlToolPack(cfg_for(base), {}).validate("query", {"sql": "SELECT 1"})
    assert isinstance(v, Refus) and "aucune table declaree" in v.reason


def test_les_commentaires_ne_masquent_pas_l_intention(pack: SqlToolPack) -> None:
    v = pack.validate("query", {"sql": "-- SELECT ok\nDROP TABLE ventes"})
    assert isinstance(v, Refus)
    assert strip_sql("SELECT 1 -- commentaire\n;") == "SELECT 1"


# ---- validate : EXPLAIN et LIMIT ----------------------------------------


def test_l_explain_attrape_une_colonne_inexistante(pack: SqlToolPack) -> None:
    v = pack.validate("query", {"sql": "SELECT colonne_absente FROM ventes"})
    assert isinstance(v, Refus) and "EXPLAIN" in v.reason


def test_le_limit_est_injecte(pack: SqlToolPack) -> None:
    v = pack.validate("query", {"sql": "SELECT * FROM ventes"})
    assert isinstance(v, Ok) and "LIMIT 1000" in v.args["sql"]


def test_le_limit_existant_est_respecte(pack: SqlToolPack) -> None:
    v = pack.validate("query", {"sql": "SELECT * FROM ventes LIMIT 2"})
    assert isinstance(v, Ok) and v.args["sql"].count("LIMIT") == 1


# ---- execution -----------------------------------------------------------


def test_schema_ne_montre_que_les_tables_declarees(pack: SqlToolPack) -> None:
    res = pack.call("schema", {})
    assert res.ok and "ventes(" in res.content and "montant" in res.content
    assert "secrets" not in res.content


def test_query_renvoie_les_lignes(pack: SqlToolPack) -> None:
    res = pack.call("query", {"sql": "SELECT segment, sum(montant) AS total FROM ventes GROUP BY 1"})
    assert res.ok and "Entreprise" in res.content and res.meta["rows"] == 2


def test_la_base_est_ouverte_en_lecture_seule(pack: SqlToolPack) -> None:
    """Meme si validate etait contourne, la connexion refuse l'ecriture."""
    res = pack.call("query", {"sql": "CREATE TABLE nouvelle (x INT)"})
    assert not res.ok


def test_base_absente_donne_une_erreur_lisible(tmp_path: Path) -> None:
    pack = SqlToolPack(cfg_for(tmp_path / "absente.duckdb"), {"tables": ["ventes"]})
    assert "introuvable" in pack.call("schema", {}).error


def test_max_rows_plafonne(base: Path) -> None:
    pack = SqlToolPack(cfg_for(base, max_rows=2), {"tables": ["ventes"]})
    res = pack.call("query", {"sql": "SELECT * FROM ventes"})
    assert res.meta["rows"] == 2 and "tronque" in res.content


# ---- artefact ------------------------------------------------------------


def test_l_artefact_contient_le_sql_reellement_execute(pack: SqlToolPack) -> None:
    verdict = pack.validate("query", {"sql": "SELECT * FROM ventes"})
    assert isinstance(verdict, Ok)
    res = pack.call("query", verdict.args)
    trace = pack.artifact("query", verdict.args, res)
    assert "LIMIT 1000" in trace and "rows=4" in trace and str(pack.database) in trace


# ---- il se branche sans que le moteur bouge -----------------------------


def test_le_meme_sous_agent_tourne_avec_le_pack_sql(base: Path, tmp_path: Path) -> None:
    cfg = cfg_for(base)
    cfg.agent = AgentConfig(max_tool_calls=3)
    plan = make_plan(n_tasks=1, toolpack="sql")
    plan.brief.tasks[0].scope = {"tables": ["ventes"]}
    store = PlanStore.create(tmp_path / "inv", plan)

    provider = FakeProvider(
        script=[
            {"action": "call_tool", "tool": "schema", "args": {}},
            {"action": "call_tool", "tool": "query",
             "args": {"sql": "SELECT sum(montant) AS total FROM ventes WHERE trimestre='T3'"}},
            {"action": "final_answer", "summary": "1500 au T3",
             "insight": "Le T3 pese 1500.", "influence": "fort"},
        ]
    )
    res = run_task(plan, plan.brief.tasks[0], build_toolpack("sql", cfg, {"tables": ["ventes"]}),
                   provider, cfg, store)

    assert res.status == "done" and res.tool_calls == 2
    assert len(res.artifacts) == 2
    trace = (store.dir / res.artifacts[1]).read_text(encoding="utf-8")
    assert "sum(montant)" in trace and "LIMIT 1000" in trace


# ---- le moteur tourne sur `files` sans charger duckdb -------------------


def test_le_moteur_tourne_sur_files_sans_importer_duckdb(corpus: Path, tmp_path: Path) -> None:
    """Verifie dans un process neuf : apres une investigation complete sur le
    pack `files`, `duckdb` n'est meme pas dans sys.modules — alors qu'il EST
    installe. La genericite ne coute rien au pack par defaut."""
    script = textwrap.dedent(f"""
        import sys
        sys.path.insert(0, {str(Path.cwd())!r})
        import importlib.util
        assert importlib.util.find_spec("duckdb") is not None, "duckdb n'est pas installe"

        from engine.config import Config, FilesPackConfig, ToolpackConfig
        from engine.loop import run
        from engine.plan import PlanStore
        from engine.providers import FakeProvider
        from engine.schemas import Brief, Plan, Task

        cfg = Config(toolpack=ToolpackConfig(files=FilesPackConfig(root={str(corpus)!r})))
        cfg.root = {str(corpus.parent)!r}
        plan = Plan(slug="sans-sql", title="Sans SQL", toolpack="files", status="ready",
                    brief=Brief(question="Q ?", tasks=[
                        Task(id="t1", title="A", question="?", scope={{"globs": ["notes/**/*.md"]}})]))
        store = PlanStore.create({str(tmp_path / "inv")!r}, plan)
        run(store, cfg, FakeProvider(script=[
            {{"action": "call_tool", "tool": "grep",
              "args": {{"pattern": "churn", "glob": "notes/**/*.md"}}}},
            {{"action": "final_answer", "summary": "s", "insight": "i", "influence": "f"}},
        ]))
        assert store.load().counts()["done"] == 1

        charges = sorted(m for m in sys.modules if "duckdb" in m or "engine.toolpacks.sql" in m)
        assert charges == [], f"modules SQL charges a tort: {{charges}}"
        print("OK")
    """)
    proc = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    assert "OK" in proc.stdout
