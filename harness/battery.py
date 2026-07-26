"""La batterie des 13 questions de reference — le seul juge de ce projet.

Deux cibles, **un seul verificateur**. C'est ce qui rend la comparaison honnete :
comparer un score neuf a un chiffre repris d'un README ne prouverait rien.

    # la ligne de base : l'agent conversationnel de data-analyst-agent, tel quel
    uv run python -m harness.battery --target daa-api --base-url http://localhost:8000

    # notre approche : une tache atomique par question, contexte vierge
    uv run python -m harness.battery --target agent
    uv run python -m harness.battery --target agent --scope --checks

Trois compteurs, jamais un seul :

    juste   la reponse porte les valeurs de reference
    bloque  la tache s'est arretee (budget, perimetre, controle en echec)
    faux    la reponse a l'air d'une reponse, et ses chiffres sont faux

**`faux` est la metrique a conduire a zero.** Maximiser `juste` viendrait apres,
et un `juste` obtenu par accident ne vaut rien. Une conception qui transforme des
reponses fausses en reponses absentes a fait son travail.

Limite methodologique, a dire a chaque fois qu'on cite un chiffre : les deux
cibles ne partagent ni le meme agent SQL, ni le meme prompt. On compare **deux
architectures** sur le meme jeu d'epreuve, pas deux versions d'un meme systeme.
"""

from __future__ import annotations

import argparse
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from engine.checks.values import missing_values
from harness.reference_questions import (
    ReferenceQuestion,
    checks_de_forme,
    checks_du_dictionnaire,
    derive_scope,
    load_reference_questions,
)

VERT, ROUGE, JAUNE, GRIS, RAZ = "\033[32m", "\033[31m", "\033[33m", "\033[90m", "\033[0m"

# Controles qu'on n'a pas pu appliquer, cumules sur la batterie. Les compter
# separement evite de faire passer une non-couverture pour un succes.
NON_VERIFIES = [0]


@dataclass
class Verdict:
    question: ReferenceQuestion
    statut: str  # "juste" | "bloque" | "faux"
    detail: str = ""
    duree_s: float = 0.0
    tokens: int = 0
    manquantes: list[str] = field(default_factory=list)

    @property
    def couleur(self) -> str:
        return {"juste": VERT, "bloque": JAUNE, "faux": ROUGE}[self.statut]

    @property
    def marque(self) -> str:
        return {"juste": "OK ", "bloque": "BLO", "faux": "FAU"}[self.statut]


def juger(
    question: ReferenceQuestion,
    *,
    prose: str,
    tables: list[dict[str, Any]],
    bloque: str = "",
    duree_s: float = 0.0,
    tokens: int = 0,
) -> Verdict:
    """Le seul endroit ou un statut est decide, pour les deux cibles."""
    if bloque:
        return Verdict(question, "bloque", bloque, duree_s, tokens)
    manquantes = missing_values(question.expected, prose=prose, tables=tables)
    if manquantes:
        return Verdict(question, "faux", manquantes[0], duree_s, tokens, manquantes)
    return Verdict(question, "juste", "", duree_s, tokens)


# --------------------------------------------------------------------------
# Cible 1 — data-analyst-agent, interroge par HTTP, sans qu'un fichier bouge
# --------------------------------------------------------------------------


def interroger_daa(question: ReferenceQuestion, base_url: str, timeout: float) -> Verdict:
    import json

    import httpx

    debut = time.monotonic()
    try:
        reponse = httpx.post(
            f"{base_url.rstrip('/')}/chat",
            json={"message": question.question},
            timeout=timeout,
        )
        reponse.raise_for_status()
        charge = reponse.json()
    except Exception as exc:
        return Verdict(question, "bloque", f"appel impossible : {exc}", time.monotonic() - debut)

    tables: list[dict[str, Any]] = []
    for artefact in charge.get("artifacts") or []:
        if artefact.get("mime") == "application/json":
            try:
                tables.append(json.loads(artefact["data"]))
            except (ValueError, KeyError, TypeError):
                continue

    return juger(
        question,
        prose=charge.get("answer") or "",
        tables=tables,
        bloque=charge.get("error") or "",
        duree_s=time.monotonic() - debut,
    )


# --------------------------------------------------------------------------
# Cible 2 — une tache atomique par question, sur notre pack SQL
# --------------------------------------------------------------------------

TOUTES_LES_TABLES = [
    "sales_daily",
    "sales_hourly",
    "traffic_daily",
    "stores",
    "store_hours",
    "products",
    "promo_calendar",
    "promo_scope",
    "weather",
    "inflation",
]


def interroger_agent(
    question: ReferenceQuestion,
    *,
    cfg,
    provider,
    scope_reel: bool,
    avec_controles: bool = False,
) -> Verdict:
    """Une question = une tache, un contexte vierge, un perimetre declare."""
    from engine.agent import run_task
    from engine.checks.verify import apply_gate
    from engine.schemas import Brief, Check, Plan, Task
    from engine.toolpacks.base import build_toolpack

    # Le perimetre vient du SQL de reference quand on l'active ; sinon la base
    # entiere, comme le fait data-analyst-agent aujourd'hui.
    scope = derive_scope(question) if scope_reel else {"tables": TOUTES_LES_TABLES}

    task = Task(
        id=question.id.lower(),
        title=question.kind,
        question=question.question,
        scope=scope,
        expected_shape="les chiffres demandes, avec leurs libelles",
        sanity_check=question.trap,
        checks=[
            Check(**c)
            for c in (
                checks_du_dictionnaire(question) + checks_de_forme(question)
                if avec_controles
                else []
            )
        ],
    )
    plan = Plan(
        slug="batterie",
        title="Batterie des questions de reference",
        toolpack="sql",
        status="ready",
        model=cfg.llm.model,
        brief=Brief(question=question.question, tasks=[task]),
    )

    debut = time.monotonic()
    pack = build_toolpack("sql", cfg, scope)
    resultat = run_task(plan, task, pack, provider, cfg, store=None)
    resultat = apply_gate(resultat, task, pack, binding=avec_controles)
    NON_VERIFIES[0] += sum(1 for o in resultat.checks if not o.applicable)
    duree = time.monotonic() - debut

    if resultat.status == "blocked":
        return Verdict(question, "bloque", resultat.reason, duree, resultat.tokens)

    return juger(
        question,
        prose=f"{resultat.summary}\n{resultat.insight}",
        tables=list(pack.tables_produites),
        duree_s=duree,
        tokens=resultat.tokens,
    )


# --------------------------------------------------------------------------


def afficher(verdict: Verdict) -> None:
    q = verdict.question
    print(
        f"{verdict.couleur}{verdict.marque}{RAZ} {q.id:4} "
        f"{GRIS}{verdict.duree_s:5.1f}s {verdict.tokens:>6} tk{RAZ}  {q.question[:64]}"
    )
    if verdict.detail:
        print(f"       {GRIS}{verdict.detail[:150]}{RAZ}")


def bilan(verdicts: list[Verdict], non_verifies: int = 0) -> int:
    comptes = {"juste": 0, "bloque": 0, "faux": 0}
    for v in verdicts:
        comptes[v.statut] += 1
    total = len(verdicts)

    print()
    print(f"  {VERT}juste  {comptes['juste']:>3}{RAZ} / {total}")
    print(f"  {JAUNE}bloque {comptes['bloque']:>3}{RAZ} / {total}")
    print(f"  {ROUGE}faux   {comptes['faux']:>3}{RAZ} / {total}   <- la metrique a conduire a zero")
    if non_verifies:
        print()
        print(f"{GRIS}{non_verifies} controle(s) non verifiable(s) : la grandeur contrainte")
        print(f"n'etait pas identifiable dans le resultat. Ni un succes, ni un echec.{RAZ}")
    print()
    print(f"{GRIS}Les deux cibles ne partagent pas le meme agent SQL : on compare deux")
    print(f"architectures sur le meme jeu d'epreuve, pas deux versions d'un systeme.{RAZ}")
    return comptes["faux"]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", choices=("daa-api", "agent"), default="agent")
    parser.add_argument("--base-url", default="http://localhost:8000", help="cible daa-api")
    parser.add_argument("--export", type=Path, default=Path("data/export"))
    parser.add_argument("--database", type=Path, default=Path("data/maxizoo.duckdb"))
    parser.add_argument("--config", default="config.yml")
    parser.add_argument("--timeout", type=float, default=300.0)
    parser.add_argument("--scope", action="store_true", help="perimetre issu du SQL de reference")
    parser.add_argument(
        "--checks", action="store_true",
        help="controles contraignants issus des ordres de grandeur du dictionnaire",
    )
    parser.add_argument("--only", nargs="*", help="ex: --only Q5 Q10")
    args = parser.parse_args()

    questions = load_reference_questions(args.export.resolve(), args.database.resolve())
    if args.only:
        voulus = {q.upper() for q in args.only}
        questions = [q for q in questions if q.id in voulus]

    options = "".join(f" · {o}" for o in ("scope" if args.scope else "", "checks" if args.checks else "") if o)
    print(f"{len(questions)} question(s) · cible {args.target}{options}")
    print()

    verdicts: list[Verdict] = []
    if args.target == "daa-api":
        for question in questions:
            verdicts.append(interroger_daa(question, args.base_url, args.timeout))
            afficher(verdicts[-1])
    else:
        from engine.config import load_config
        from engine.providers import build_provider

        cfg = load_config(args.config)
        cfg.toolpack.sql.database = str(args.database.resolve())
        provider = build_provider(cfg.llm)
        for question in questions:
            verdicts.append(
                interroger_agent(
                    question,
                    cfg=cfg,
                    provider=provider,
                    scope_reel=args.scope,
                    avec_controles=args.checks,
                )
            )
            afficher(verdicts[-1])

    sys.exit(1 if bilan(verdicts, NON_VERIFIES[0]) else 0)


if __name__ == "__main__":
    main()
