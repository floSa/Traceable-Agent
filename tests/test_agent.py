"""Etape 4b — le sous-agent : boucle bornee, refus, blocked sans crash."""

from __future__ import annotations

from pathlib import Path

from conftest import make_plan

from engine.agent import run_task
from engine.config import AgentConfig, Config, FilesPackConfig, ToolpackConfig
from engine.plan import PlanStore
from engine.providers import FakeProvider
from engine.toolpacks.files import FilesToolPack


def cfg_for(corpus: Path, **agent_kw) -> Config:
    c = Config(
        toolpack=ToolpackConfig(files=FilesPackConfig(root=str(corpus))),
        agent=AgentConfig(**agent_kw),
    )
    c.root = corpus.parent
    return c


def setup(corpus: Path, tmp_path: Path, scope=None, **agent_kw):
    cfg = cfg_for(corpus, **agent_kw)
    plan = make_plan(n_tasks=1)
    plan.brief.tasks[0].scope = scope if scope is not None else {"globs": ["notes/**/*.md"]}
    store = PlanStore.create(tmp_path / "inv", plan)
    pack = FilesToolPack(cfg, plan.brief.tasks[0].scope)
    return cfg, plan, plan.brief.tasks[0], pack, store


TOOL_STEP = {"action": "call_tool", "tool": "grep", "args": {"pattern": "churn", "glob": "notes/**/*.md"}}
FINAL = {
    "action": "final_answer",
    "summary": "notes/a.md:2 mentionne un churn de 12%",
    "insight": "Le churn augmente au T3.",
    "influence": "confirme le signal",
}


def test_un_outil_puis_une_reponse(corpus: Path, tmp_path: Path) -> None:
    cfg, plan, task, pack, store = setup(corpus, tmp_path)
    provider = FakeProvider(script=[TOOL_STEP, FINAL])
    res = run_task(plan, task, pack, provider, cfg, store)

    assert res.status == "done"
    assert res.insight == "Le churn augmente au T3."
    assert res.tool_calls == 1
    assert res.tokens > 0


def test_le_resultat_de_l_outil_est_reinjecte_dans_le_tour_suivant(
    corpus: Path, tmp_path: Path
) -> None:
    cfg, plan, task, pack, store = setup(corpus, tmp_path)
    provider = FakeProvider(script=[TOOL_STEP, FINAL])
    run_task(plan, task, pack, provider, cfg, store)
    _, _, second_user = provider.prompts[1]
    assert "notes/a.md:2" in second_user
    assert "Observations deja obtenues" in second_user


def test_l_artefact_est_ecrit_sur_disque_et_reference(corpus: Path, tmp_path: Path) -> None:
    cfg, plan, task, pack, store = setup(corpus, tmp_path)
    res = run_task(plan, task, pack, FakeProvider(script=[TOOL_STEP, FINAL]), cfg, store)

    assert res.artifacts == ["artifacts/t1_01_grep.txt"]
    trace = (store.dir / res.artifacts[0]).read_text(encoding="utf-8")
    assert "grep -rnE" in trace
    # write-only : la trace n'est jamais renvoyee au modele
    assert all("grep -rnE" not in user for _, _, user in FakeProvider().prompts)


def test_un_refus_de_validate_n_execute_pas_l_outil(corpus: Path, tmp_path: Path) -> None:
    cfg, plan, task, pack, store = setup(corpus, tmp_path)
    executes: list[str] = []
    pack.call = lambda n, a: executes.append(n)  # type: ignore[assignment]

    interdit = {"action": "call_tool", "tool": "read", "args": {"path": "secret.txt"}}
    provider = FakeProvider(script=[interdit, FINAL])
    res = run_task(plan, task, pack, provider, cfg, store)

    assert executes == []  # `call` n'a jamais ete atteint
    assert res.status == "done"
    _, _, second_user = provider.prompts[1]
    assert "REFUSE" in second_user and "hors perimetre" in second_user
    assert res.artifacts == []  # pas d'artefact pour un outil non execute


def test_budget_d_outils_depasse_donne_blocked(corpus: Path, tmp_path: Path) -> None:
    cfg, plan, task, pack, store = setup(corpus, tmp_path, max_tool_calls=2)
    provider = FakeProvider(script=[TOOL_STEP] * 5)
    res = run_task(plan, task, pack, provider, cfg, store)

    assert res.status == "blocked"
    assert "budget d'outils epuise" in res.reason
    assert res.tool_calls == 2  # pas un de plus


def test_budget_de_tokens_depasse_donne_blocked(corpus: Path, tmp_path: Path) -> None:
    cfg, plan, task, pack, store = setup(corpus, tmp_path, max_tokens=150, max_tool_calls=10)
    provider = FakeProvider(script=[TOOL_STEP] * 10, tokens_per_call=100)
    res = run_task(plan, task, pack, provider, cfg, store)

    assert res.status == "blocked" and "budget de tokens" in res.reason


def test_le_budget_du_plan_plafonne_celui_de_la_config(corpus: Path, tmp_path: Path) -> None:
    cfg, plan, task, pack, store = setup(corpus, tmp_path, max_tokens=999999, max_tool_calls=10)
    plan.budget_tokens_per_task = 150
    provider = FakeProvider(script=[TOOL_STEP] * 10, tokens_per_call=100)
    assert run_task(plan, task, pack, provider, cfg, store).status == "blocked"


def test_modele_inexploitable_donne_blocked_sans_crash(corpus: Path, tmp_path: Path) -> None:
    cfg, plan, task, pack, store = setup(corpus, tmp_path)
    provider = FakeProvider(script=["pas du json"] * 3, max_validation_retries=3)
    res = run_task(plan, task, pack, provider, cfg, store)

    assert res.status == "blocked"
    assert "inexploitable" in res.reason
    assert res.tokens > 0  # les tokens gaches sont quand meme comptes


def test_le_system_prompt_impose_la_tache_unique(corpus: Path, tmp_path: Path) -> None:
    cfg, plan, task, pack, store = setup(corpus, tmp_path)
    provider = FakeProvider(script=[FINAL])
    run_task(plan, task, pack, provider, cfg, store)
    _, system, _ = provider.prompts[0]
    assert "You execute exactly one task." in system
    assert "You do not summarize." in system
    assert "You do not comment on other tasks." in system
    assert "notes/**/*.md" in system  # la description du pack est injectee


def test_l_agent_ne_voit_pas_les_autres_taches(corpus: Path, tmp_path: Path) -> None:
    cfg = cfg_for(corpus)
    plan = make_plan(n_tasks=3, chain=True)
    store = PlanStore.create(tmp_path / "inv", plan)
    pack = FilesToolPack(cfg, plan.brief.tasks[0].scope)
    provider = FakeProvider(script=[FINAL])
    run_task(plan, plan.brief.tasks[0], pack, provider, cfg, store)
    _, _, user = provider.prompts[0]
    assert "Tache 2" not in user and "Question 2" not in user


# ---- robustesse decouverte en run reel ----------------------------------


def test_un_appel_identique_repete_n_est_pas_rejoue(corpus: Path, tmp_path: Path) -> None:
    """Un petit modele rejoue volontiers a l'identique un appel infructueux
    jusqu'a epuiser son budget. On lui renvoie le resultat deja obtenu, on ne
    repaye pas l'execution, et on lui dit de changer."""
    cfg, plan, task, pack, store = setup(corpus, tmp_path, max_tool_calls=4)
    executes: list[dict] = []
    vrai_call = pack.call
    pack.call = lambda n, a: (executes.append(a), vrai_call(n, a))[1]  # type: ignore[assignment]

    provider = FakeProvider(script=[TOOL_STEP, TOOL_STEP, TOOL_STEP, FINAL])
    res = run_task(plan, task, pack, provider, cfg, store)

    assert res.status == "done"
    assert len(executes) == 1  # l'outil n'a tourne qu'une fois
    assert len(res.artifacts) == 1  # et n'a produit qu'une trace
    _, _, dernier = provider.prompts[-1]
    assert "DEJA FAIT" in dernier and "Ne le repete pas" in dernier


def test_la_description_du_pack_ancre_l_agent_dans_le_corpus_reel(
    corpus: Path, tmp_path: Path
) -> None:
    """Sans echantillon des fichiers presents, l'agent invente des chemins."""
    cfg, plan, task, pack, store = setup(corpus, tmp_path)
    description = pack.describe()
    assert "notes/a.md" in description  # fichiers reellement presents
    assert "secret.txt" not in description  # mais seulement dans le perimetre
    assert "n'ecris JAMAIS" in description  # pas de prefixe de racine


def test_un_motif_sans_resultat_redit_ou_chercher(corpus: Path, tmp_path: Path) -> None:
    cfg, plan, task, pack, store = setup(corpus, tmp_path, scope={"globs": ["**/*"]})
    res = pack.call("list", {"glob": "**/*.csv"})
    assert res.ok and "aucun fichier ne correspond" in res.content
    assert "notes/a.md" in res.content  # on redonne l'arborescence reelle
