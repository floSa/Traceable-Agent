"""Role 1 — `brief_builder` : LLM, conversationnel, UNE question a la fois.

Il produit une liste de taches atomiques. Sa sortie est du JSON (`BriefDraft`) ;
c'est le code, jamais le modele, qui la serialise en markdown.

Deux schemas minuscules plutot qu'un gros : un petit modele repond mieux a
"pose une question" et "produis une liste" separement qu'a un objet a branches.

Point de controle humain n° 1 : la liste produite passe en `ready`, et rien ne
s'execute avant que l'humain l'ait relue (`inv tasks`) et lance `inv run`.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Callable

from engine.config import Config
from engine.providers import BaseProvider
from typing import Any

from pydantic import BaseModel, Field, create_model, model_validator

from engine.schemas import (
    Brief,
    BriefDraft,
    DecisionFrame,
    Plan,
    Task,
    TaskDraft,
    Usage,
    utcnow,
)

ASK_SYSTEM = """You help scope an investigation. You ask ONE question at a time.
You never ask two questions in the same message. You never propose tasks yet.

Ton but : reunir juste ce qu'il faut pour decouper l'enquete en taches atomiques
verifiables. Une bonne question de clarification porte sur le perimetre, la
decision a prendre, ou ce qui compterait comme preuve.
need_more=false des que tu peux decouper l'enquete sans deviner.
"""

PLAN_SYSTEM = """You produce a list of atomic investigation tasks. You do not
investigate. You do not answer the question yourself. You do not reduce the
investigation to its first step.

`title` nomme l'ENQUETE ENTIERE, pas la premiere tache.
`question` reprend la question de l'utilisateur, sans la reformuler en tache.

Chaque tache doit etre :
- atomique : une seule question, verifiable par un ou deux appels d'outil ;
- autonome : formulee pour quelqu'un qui n'a AUCUN contexte, ni le corpus en tete ;
- distincte : deux taches ne posent jamais la meme question ;
- ordonnee : `depends_on` ne cite que des ids de taches DEJA listees avant elle.
Les ids sont courts et sans espace : t1, t2, t3...
`scope` decrit le perimetre au format attendu par le toolpack (voir plus bas).

Decoupe par HYPOTHESE ou par SOURCE, jamais par etape technique.
Mauvais decoupage : "lister les fichiers", "lire les fichiers", "resumer".
Bon decoupage : une tache par cause possible, une tache par population comparee,
une tache qui cherche activement ce qui contredit les autres.

`kill_criterion` decrit ce qui rendrait l'enquete inutile a poursuivre.
`watch_outs` liste les pieges de raisonnement propres a CETTE question.
"""


def slugify(text: str, limit: int = 40) -> str:
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    text = re.sub(r"[^a-zA-Z0-9]+", "-", text).strip("-").lower()
    return (text[:limit].rstrip("-")) or "investigation"


def ask_one_question(
    question: str, transcript: list[tuple[str, str]], provider: BaseProvider, cfg: Config
) -> tuple[bool, str, Usage]:
    """Un tour de clarification. Retourne (besoin d'en savoir plus, question)."""
    from engine.schemas import Strict

    class ClarifyTurn(Strict):
        need_more: bool
        question: str = ""

    echange = "\n".join(f"Q: {q}\nR: {r}" for q, r in transcript) or "(rien encore)"
    user = (
        f"## Question initiale de l'utilisateur\n{question}\n\n"
        f"## Echange deja eu\n{echange}\n\n"
        f"Pose LA prochaine question, une seule, ou mets need_more=false."
    )
    turn, usage = provider.complete(ASK_SYSTEM, user, ClarifyTurn, cfg.brief.max_tokens)
    return bool(turn.need_more), str(turn.question or ""), usage


def bounded_draft_schema(
    cfg: Config, scope_model: type[BaseModel] | None = None
) -> type[BriefDraft]:
    """Construit le schema de sortie du role 1.

    Tout ce qu'un prompt seul n'obtient pas d'un petit modele passe par le
    SCHEMA, donc par le retry a feedback. Trois regles, chacune constatee en
    run reel avec `gemma4:e4b` :

    1. nombre de taches borne — sinon il rend "1 tache : lister les fichiers" ;
    2. `scope` type par le toolpack — un `dict` libre se serialise en
       `{"type": "object"}`, qui ne dit rien : il rendait `{}` neuf fois sur
       neuf. Type, le champ se remplit tout seul ;
    3. un id de tache n'est pas un nom de champ du brief — sinon il promeut
       `kill_criterion` en tache.

    Le moteur ne sait toujours rien du metier : `scope_model` vient du pack.
    """
    champs_du_brief = set(BriefDraft.model_fields) - {"tasks"}

    class CheckedTaskDraft(TaskDraft):
        @model_validator(mode="after")
        def _verifier(self) -> CheckedTaskDraft:
            if self.id in champs_du_brief:
                raise ValueError(
                    f"{self.id!r} est un champ du brief, pas une tache. "
                    "Renseigne-le a la racine et donne aux taches des ids t1, t2, t3..."
                )
            return self

    # `create_model` plutot qu'une annotation conditionnelle : le type vient
    # d'une variable, et `from __future__ import annotations` la transformerait
    # en simple chaine que Pydantic ne saurait pas resoudre.
    task_schema: type[TaskDraft] = (
        create_model("BoundedTaskDraft", __base__=CheckedTaskDraft, scope=(scope_model, ...))
        if scope_model is not None
        else CheckedTaskDraft
    )

    return create_model(
        "BoundedBriefDraft",
        __base__=BriefDraft,
        tasks=(
            list[task_schema],  # type: ignore[valid-type]
            Field(..., min_length=cfg.brief.min_tasks, max_length=cfg.brief.max_tasks),
        ),
    )


def draft_tasks(
    question: str,
    transcript: list[tuple[str, str]],
    toolpack_description: str,
    provider: BaseProvider,
    cfg: Config,
    scope_hint: str = "",
    scope_model: type[BaseModel] | None = None,
) -> tuple[BriefDraft, Usage]:
    echange = "\n".join(f"Q: {q}\nR: {r}" for q, r in transcript) or "(aucune clarification)"
    user = (
        f"## Question a investiguer (a reprendre telle quelle dans `question`)\n{question}\n\n"
        f"## Clarifications obtenues\n{echange}\n\n"
        f"## Outils dont disposeront les taches\n{toolpack_description}\n\n"
        f"## Format OBLIGATOIRE du champ `scope` de chaque tache\n{scope_hint}\n"
        "Une tache sans `scope` renseigne fait travailler le sous-agent a l'aveugle.\n\n"
        f"Produis entre {cfg.brief.min_tasks} et {cfg.brief.max_tasks} taches, "
        f"chacune portant sur un angle different de la question."
    )
    draft, usage = provider.complete(
        PLAN_SYSTEM, user, bounded_draft_schema(cfg, scope_model), cfg.brief.max_tokens
    )
    assert isinstance(draft, BriefDraft)
    return draft, usage


def to_plan(
    draft: BriefDraft,
    toolpack: str,
    model: str,
    owner: str = "",
    question: str | None = None,
) -> Plan:
    """Serialise la sortie du LLM en objet Plan. Le modele n'ecrit pas un
    caractere de markdown : cette fonction est le seul chemin.

    `question` (celle tapee par l'humain) fait autorite sur la reformulation
    du modele, et c'est elle qui donne le slug : un petit modele reduit
    volontiers la question a sa premiere tache, on ne le laisse pas renommer
    l'enquete pour autant.
    """
    connus: list[str] = []
    tasks: list[Task] = []
    for t in draft.tasks:
        tasks.append(
            Task(
                id=t.id,
                title=t.title,
                question=t.question,
                # le pack a peut-etre type le scope : le plan, lui, le
                # stocke en dict libre — le moteur n'en sait rien
                scope=t.scope if isinstance(t.scope, dict) else t.scope.model_dump(),
                expected_shape=t.expected_shape,
                sanity_check=t.sanity_check,
                # on elague les dependances hors-graphe plutot que de refuser
                # tout le brief pour une hallucination d'id
                depends_on=[d for d in t.depends_on if d in connus],
                state="pending",
            )
        )
        connus.append(t.id)

    question_finale = (question or draft.question).strip()
    return Plan(
        slug=slugify(question_finale),
        title=draft.title or question_finale[:80],
        created_at=utcnow(),
        toolpack=toolpack,
        status="ready",  # pret a etre relu par un humain, pas encore lance
        mode="decision" if draft.decision else "exploration",
        model=model,
        brief=Brief(
            owner=owner,
            question=question_finale,
            decision_frame=DecisionFrame(
                decision=draft.decision,
                action_threshold=draft.action_threshold,
                kill_criterion=draft.kill_criterion,
            ),
            watch_outs=draft.watch_outs,
            deliverable=draft.deliverable,
            tasks=tasks,
        ),
    )


def build_brief(
    question: str,
    toolpack_description: str,
    provider: BaseProvider,
    cfg: Config,
    ask_human: Callable[[str], str] | None = None,
    toolpack: str = "files",
    owner: str = "",
    scope_hint: str = "",
    scope_model: type[BaseModel] | None = None,
) -> tuple[Plan, Usage]:
    """Conduit l'echange puis rend un Plan en statut `ready`.

    `ask_human=None` : mode non interactif (`--yes`), on passe directement au
    decoupage. C'est ce que fait la demo du README.
    """
    transcript: list[tuple[str, str]] = []
    usage = Usage()

    if ask_human is not None:
        for _ in range(cfg.brief.max_rounds):
            need_more, q, u = ask_one_question(question, transcript, provider, cfg)
            usage = usage + u
            if not need_more or not q.strip():
                break
            transcript.append((q, ask_human(q)))

    draft, u = draft_tasks(
        question, transcript, toolpack_description, provider, cfg, scope_hint, scope_model
    )
    usage = usage + u
    plan = to_plan(draft, toolpack, cfg.llm.model, owner, question=question)
    return plan, usage
