"""TROU 6 — reprise apres echec, prouvee en tuant vraiment le process.

Le moteur est redemarrable par construction : `plan.md` est la seule source de
verite, il est relu au debut de chaque tour, et chaque ecriture est atomique.
Ces tests le verifient avec un SIGKILL (pas un SIGTERM : aucune chance de
nettoyage, aucun `finally` execute).
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest
from conftest import make_plan

from engine.config import Config, FilesPackConfig, ToolpackConfig
from engine.loop import run
from engine.plan import PlanStore
from engine.providers import FakeProvider

CHILD = Path(__file__).parent / "_crash_child.py"
FINAL = {
    "action": "final_answer",
    "summary": "trouve dans notes/a.md",
    "insight": "Le churn augmente au T3.",
    "influence": "confirme",
}


def attendre(predicat, timeout: float = 40.0, pas: float = 0.02):
    fin = time.monotonic() + timeout
    while time.monotonic() < fin:
        try:
            if predicat():
                return True
        except Exception:  # fichier en cours de remplacement : on reessaie
            pass
        time.sleep(pas)
    return False


def cfg_for(corpus: Path) -> Config:
    c = Config(toolpack=ToolpackConfig(files=FilesPackConfig(root=str(corpus))))
    c.root = corpus.parent
    return c


def test_kill_en_pleine_boucle_puis_relance_reprend_au_bon_endroit(
    corpus: Path, tmp_path: Path
) -> None:
    store = PlanStore.create(tmp_path / "inv", make_plan(n_tasks=6))

    child = subprocess.Popen(
        [sys.executable, str(CHILD), str(store.dir), str(corpus), "loop"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        assert attendre(lambda: store.load().counts()["done"] >= 2), "l'enfant n'a rien produit"
        os.kill(child.pid, signal.SIGKILL)  # aucune chance de se rattraper
    finally:
        child.wait(timeout=10)
    assert child.returncode == -signal.SIGKILL

    # --- le fichier survit au kill : toujours valide, jamais a moitie ecrit
    interrompu = store.load()
    faites = interrompu.counts()["done"]
    assert 2 <= faites < 6
    assert len(interrompu.logbook.findings) == faites
    # invariant : finding et run_log sont ecrits dans la meme transaction
    assert len(interrompu.logbook.run_log) == faites
    assert interrompu.status == "in_progress"  # personne n'a clos l'enquete

    deja_faites = [t.id for t in interrompu.brief.tasks if t.state == "done"]

    # --- relance : reprend a la premiere pending, sans rejouer les faites
    outcome = run(store, cfg_for(corpus), FakeProvider(script=[FINAL] * 6))
    final = store.load()

    assert outcome.tasks_run == 6 - faites
    assert final.counts()["done"] == 6
    assert final.status == "awaiting_review"
    executees = [e.task_id for e in final.logbook.run_log]
    assert len(executees) == len(set(executees)) == 6  # aucune tache rejouee
    assert not any(t in [r.task_id for r in outcome.results] for t in deja_faites)


def test_kill_pendant_une_rafale_d_ecritures_laisse_un_fichier_valide(
    corpus: Path, tmp_path: Path
) -> None:
    """Preuve directe de l'ecriture atomique : on tue au hasard au milieu de
    centaines de reecritures, le fichier reste parsable et complet."""
    store = PlanStore.create(tmp_path / "inv", make_plan(n_tasks=20))

    child = subprocess.Popen(
        [sys.executable, str(CHILD), str(store.dir), str(corpus), "commits"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        assert attendre(lambda: store.load().logbook.findings != [])
        time.sleep(0.25)  # on laisse tourner la rafale
        os.kill(child.pid, signal.SIGKILL)
    finally:
        child.wait(timeout=10)

    plan = store.load()  # revalidation integrale : leve si le fichier est casse
    assert plan.brief.tasks and plan.logbook.findings
    # le contenu n'est pas tronque : le corps markdown va jusqu'a sa derniere section
    texte = store.plan_path.read_text(encoding="utf-8")
    assert texte.startswith("---\n") and texte.count("\n---\n") >= 1
    assert "### Evaluations du critere d'arret" in texte  # derniere section du template


@pytest.mark.parametrize("tue_avant", [1, 3])
def test_kill_repete_converge_quand_meme(corpus: Path, tmp_path: Path, tue_avant: int) -> None:
    """Meme tue plusieurs fois de suite, l'enquete finit par se terminer."""
    store = PlanStore.create(tmp_path / "inv", make_plan(n_tasks=5))

    for _ in range(2):
        child = subprocess.Popen(
            [sys.executable, str(CHILD), str(store.dir), str(corpus), "loop"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        depart = store.load().counts()["done"]
        cible = min(depart + tue_avant, 5)
        try:
            attendre(lambda: store.load().counts()["done"] >= cible, timeout=40)
            os.kill(child.pid, signal.SIGKILL)
        finally:
            child.wait(timeout=10)

    run(store, cfg_for(corpus), FakeProvider(script=[FINAL] * 5))
    plan = store.load()
    assert plan.counts()["done"] == 5
    executees = [e.task_id for e in plan.logbook.run_log]
    assert len(executees) == len(set(executees)) == 5


def test_un_temporaire_orphelin_est_balaye(corpus: Path, tmp_path: Path) -> None:
    """Un kill pile entre mkstemp et os.replace laisse un temporaire. Il n'a
    jamais pu corrompre plan.md, et la prochaine ecriture le balaie."""
    store = PlanStore.create(tmp_path / "inv", make_plan(n_tasks=1))
    orphelin = store.dir / ".plan.abcdef.tmp"
    orphelin.write_text("ecriture interrompue a moitie", encoding="utf-8")
    os.utime(orphelin, (0, 0))  # vieux : c'est un debris, pas une ecriture en vol

    en_vol = store.dir / ".plan.zzzzzz.tmp"
    en_vol.write_text("ecriture d'un autre process, maintenant", encoding="utf-8")

    store.save(store.load())

    assert not orphelin.exists()
    assert en_vol.exists()  # trop recent pour etre un debris : on n'y touche pas
    assert store.load().brief.tasks
