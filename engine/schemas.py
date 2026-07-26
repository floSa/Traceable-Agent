"""Schemas Pydantic v2 — la totalite de l'etat du systeme.

Le fichier `plan.md` est la seule memoire du moteur. Tout ce qui est
persiste passe par `Plan`, revalide integralement a chaque ecriture.

Les modeles sont separes en deux familles :

- etat persiste            : Plan / Brief / Task / Logbook / ...
- sorties structurees LLM  : BriefDraft / AgentStep / StopEvaluation / Synthesis

Aucun LLM n'ecrit de markdown : il ne produit que les modeles de la
seconde famille, et c'est le code qui serialise.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

PlanStatus = Literal["draft", "ready", "in_progress", "awaiting_review", "done", "killed"]
PlanMode = Literal["exploration", "decision"]
TaskState = Literal["pending", "done", "skipped", "blocked"]


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Strict(BaseModel):
    """Base commune : on refuse les champs inconnus pour que toute derive
    de schema explose a la lecture plutot que d'etre silencieusement perdue."""

    model_config = ConfigDict(extra="forbid")


# --------------------------------------------------------------------------
# Etat persiste
# --------------------------------------------------------------------------


class DecisionFrame(Strict):
    decision: str = ""
    action_threshold: str = ""
    kill_criterion: str = ""


class Check(Strict):
    """Un controle CONTRAIGNANT sur le resultat d'une tache.

    A ne pas confondre avec `Task.sanity_check`, qui est une consigne rendue au
    prompt. La difference est le coeur du dispositif : une consigne est
    indicative — mesure faite, un modele la lit et passe outre sans broncher —
    tandis qu'un `Check` est du code qui lit le resultat STRUCTURE produit par
    l'outil. On ne demande pas au modele de se verifier ; on verifie.
    """

    kind: Literal["expect_values", "magnitude", "row_count", "columns"]
    label: str = ""  # ce qu'on verifie, en clair, pour le plan et le rapport

    # kind="expect_values" : des valeurs de reference doivent etre presentes
    values: list[Any] = Field(default_factory=list)
    # kind="magnitude" : un intervalle plausible, tire du dictionnaire de la
    # source. Ne demande AUCUNE reponse de reference : marche donc en
    # production, sur une question inedite. C'est le controle porteur.
    column: str | None = None
    low: float | None = None
    high: float | None = None
    # kind="row_count" / "columns" : `expected_shape` devenu du code
    min_rows: int | None = None
    max_rows: int | None = None
    names: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _coherent(self) -> Check:
        if self.kind == "expect_values" and not self.values:
            raise ValueError("expect_values sans valeur ne verifie rien")
        if self.kind == "magnitude" and self.low is None and self.high is None:
            raise ValueError("magnitude exige au moins une borne (low ou high)")
        if self.kind == "row_count" and self.min_rows is None and self.max_rows is None:
            raise ValueError("row_count exige min_rows ou max_rows")
        if self.kind == "columns" and not self.names:
            raise ValueError("columns sans nom ne verifie rien")
        return self


class CheckOutcome(Strict):
    """Le resultat d'un controle, persiste dans le plan : il dit POURQUOI."""

    kind: str
    label: str = ""
    passed: bool
    detail: str = ""


class Task(Strict):
    id: str
    title: str
    question: str
    # Dict libre, interprete par le ToolPack et par lui seul.
    # sql   -> {"tables": [...]}
    # files -> {"globs": [...]}
    scope: dict[str, Any] = Field(default_factory=dict)
    expected_shape: str = ""
    sanity_check: str = ""
    state: TaskState = "pending"
    depends_on: list[str] = Field(default_factory=list)
    # Controles contraignants. Une tache dont un controle echoue ne passe pas
    # `done` : elle est `blocked`, donc elle ne debloque aucune dependante et
    # son chiffre n'atteint jamais la synthese.
    checks: list[Check] = Field(default_factory=list)

    @field_validator("id")
    @classmethod
    def _id_is_slugish(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("task id vide")
        if any(c.isspace() for c in v):
            raise ValueError(f"task id invalide (espaces): {v!r}")
        return v


class Brief(Strict):
    owner: str = ""
    question: str = ""
    decision_frame: DecisionFrame = Field(default_factory=DecisionFrame)
    watch_outs: list[str] = Field(default_factory=list)
    tasks: list[Task] = Field(default_factory=list)
    deliverable: str = ""

    @model_validator(mode="after")
    def _check_task_graph(self) -> Brief:
        ids = [t.id for t in self.tasks]
        dupes = {i for i in ids if ids.count(i) > 1}
        if dupes:
            raise ValueError(f"ids de taches dupliques: {sorted(dupes)}")
        known = set(ids)
        for t in self.tasks:
            unknown = [d for d in t.depends_on if d not in known]
            if unknown:
                raise ValueError(f"tache {t.id}: depends_on inconnus {unknown}")
            if t.id in t.depends_on:
                raise ValueError(f"tache {t.id}: depend d'elle-meme")
        return self


class Finding(Strict):
    task_id: str
    summary: str = ""
    insight: str = ""
    influence: str = ""
    artifacts: list[str] = Field(default_factory=list)
    checks: list[CheckOutcome] = Field(default_factory=list)
    # TROU 5 — artefacts visuels : hors perimetre du POC.
    # Le champ existe dans le schema et reste null. Voir README > Extensions.
    chart: str | None = None


class Adjustment(Strict):
    step: str
    change: str
    reason: str


class RunLogEntry(Strict):
    task_id: str
    status: Literal["done", "blocked", "skipped", "crashed"]
    started_utc: datetime
    duration_s: float = 0.0
    tokens: int = 0


class StopLogEntry(Strict):
    """TROU 2 — trace systematique de l'evaluation du critere d'arret."""

    after_task: str
    strategy: Literal["none", "llm", "callable"]
    fired: bool
    reason: str = ""
    at_utc: datetime = Field(default_factory=utcnow)


class Logbook(Strict):
    findings: list[Finding] = Field(default_factory=list)
    progress: list[str] = Field(default_factory=list)
    adjustments: list[Adjustment] = Field(default_factory=list)
    run_log: list[RunLogEntry] = Field(default_factory=list)
    stop_log: list[StopLogEntry] = Field(default_factory=list)


class Plan(Strict):
    """L'objet complet. Serialise en frontmatter YAML de `plan.md`."""

    # -- frontmatter "meta" (a plat, comme specifie)
    slug: str
    title: str
    created_at: datetime = Field(default_factory=utcnow)
    toolpack: str = "files"
    status: PlanStatus = "draft"
    mode: PlanMode = "exploration"
    model: str = ""
    budget_tokens_per_task: int = 20000
    budget_tokens_total: int = 200000

    # -- corps
    brief: Brief = Field(default_factory=Brief)
    logbook: Logbook = Field(default_factory=Logbook)

    # ---- helpers de lecture (aucun effet de bord) -------------------------

    def task(self, task_id: str) -> Task | None:
        return next((t for t in self.brief.tasks if t.id == task_id), None)

    def finding(self, task_id: str) -> Finding | None:
        return next((f for f in self.logbook.findings if f.task_id == task_id), None)

    def tokens_spent(self) -> int:
        return sum(e.tokens for e in self.logbook.run_log)

    def next_runnable(
        self, retry_blocked: bool = False, skip: set[str] | None = None
    ) -> Task | None:
        """Premiere tache executable : `pending` (ou `blocked` si --retry-blocked)
        dont tous les `depends_on` sont `done`.

        `skip` exclut les taches deja tentees dans le run courant : sans lui,
        une tache qui rebloque sous `--retry-blocked` reste eligible et se
        fait rejouer indefiniment.
        """
        wanted: set[str] = {"pending"} | ({"blocked"} if retry_blocked else set())
        done = {t.id for t in self.brief.tasks if t.state == "done"}
        skip = skip or set()
        for t in self.brief.tasks:
            if t.id in skip:
                continue
            if t.state in wanted and all(d in done for d in t.depends_on):
                return t
        return None

    def counts(self) -> dict[str, int]:
        out = {"pending": 0, "done": 0, "skipped": 0, "blocked": 0}
        for t in self.brief.tasks:
            out[t.state] += 1
        return out


# --------------------------------------------------------------------------
# Sorties structurees des LLM
# --------------------------------------------------------------------------


class TaskDraft(Strict):
    """Une tache proposee par le brief_builder."""

    id: str
    title: str
    question: str
    scope: dict[str, Any] = Field(default_factory=dict)
    expected_shape: str = ""
    sanity_check: str = ""
    depends_on: list[str] = Field(default_factory=list)


class BriefDraft(Strict):
    """Sortie du role 1. Serialise en markdown par le code, jamais par le LLM."""

    title: str
    question: str
    decision: str = ""
    action_threshold: str = ""
    kill_criterion: str = ""
    watch_outs: list[str] = Field(default_factory=list)
    deliverable: str = ""
    tasks: list[TaskDraft] = Field(default_factory=list)


class AgentStep(Strict):
    """Un tour du sous-agent : soit il appelle un outil, soit il repond.

    Le validateur ci-dessous est volontairement strict : ses messages
    d'erreur sont renvoyes tels quels au modele par le retry a feedback
    du provider. C'est ce qui rend un petit modele exploitable.
    """

    reasoning: str = ""
    action: Literal["call_tool", "final_answer"]
    tool: str | None = None
    args: dict[str, Any] = Field(default_factory=dict)
    summary: str | None = None
    insight: str | None = None
    influence: str | None = None

    @model_validator(mode="after")
    def _coherent(self) -> AgentStep:
        if self.action == "call_tool":
            if not self.tool:
                raise ValueError(
                    "action='call_tool' exige le champ 'tool' (nom de l'outil)"
                )
        else:
            missing = [
                k
                for k in ("summary", "insight", "influence")
                if not (getattr(self, k) or "").strip()
            ]
            if missing:
                raise ValueError(
                    f"action='final_answer' exige des champs non vides: {missing}"
                )
        return self


class TaskResult(Strict):
    """Ce que `agent.run_task` rend a la boucle. Construit par le code :
    le LLM ne choisit ni le statut final, ni les chemins d'artefacts."""

    task_id: str
    status: Literal["done", "blocked"]
    summary: str = ""
    insight: str = ""
    influence: str = ""
    artifacts: list[str] = Field(default_factory=list)
    reason: str = ""
    tokens: int = 0
    tool_calls: int = 0
    checks: list[CheckOutcome] = Field(default_factory=list)

    def to_finding(self) -> Finding:
        return Finding(
            task_id=self.task_id,
            summary=self.summary,
            insight=self.insight,
            influence=self.influence,
            artifacts=list(self.artifacts),
            checks=list(self.checks),
            chart=None,
        )

    def blocked_by(self, outcomes: list[CheckOutcome], reason: str) -> TaskResult:
        """La porte : un resultat qui echoue a son controle n'est pas `done`.

        On garde le resume et l'insight — ils documentent ce que le modele a
        cru trouver — mais l'etat dit `blocked`, et c'est l'etat qui commande
        la suite.
        """
        return self.model_copy(update={"status": "blocked", "reason": reason, "checks": outcomes})


class StopEvaluation(Strict):
    """TROU 2 — sortie de la strategie `llm`."""

    fired: bool
    reason: str = ""


class Synthesis(Strict):
    headline: str
    answer: str
    evidence: list[str] = Field(default_factory=list)
    caveats: list[str] = Field(default_factory=list)
    recommendation: str = ""


class Usage(Strict):
    prompt_tokens: int = 0
    completion_tokens: int = 0

    @property
    def total(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    def __add__(self, other: Usage) -> Usage:
        return Usage(
            prompt_tokens=self.prompt_tokens + other.prompt_tokens,
            completion_tokens=self.completion_tokens + other.completion_tokens,
        )
