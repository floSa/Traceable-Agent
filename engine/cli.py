"""CLI `inv` — les deux points de controle humain sont ici, et nulle part ailleurs.

    inv new "<question>" [--toolpack files]   role 1, brief_builder
    inv tasks <slug> [--edit]                 GATE 1 : relire/editer la liste
    inv run <slug> [--retry-blocked]          role 2, la boucle
    inv step <slug>                           une seule tache, pour debug
    inv status <slug>
    inv review <slug>                         GATE 2 : findings brutes
    inv approve <slug>
    inv synthesize <slug>                     role 4
    inv report <slug>                         role 5
    inv done <slug> / inv kill <slug>         cloture, humaine et explicite

Aucune de ces commandes ne fait passer une investigation a `done` toute seule.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

import typer
from pydantic import ValidationError
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from engine import loop as loop_module
from engine import reporter
from engine.brief import build_brief
from engine.config import Config, load_config
from engine.plan import PlanStore, find_investigation
from engine.providers import BaseProvider, ProviderError, build_provider
from engine.schemas import Plan
from engine.synthesize import APPROVAL_STEP, is_approved, synthesize as run_synthesis
from engine.toolpacks.base import AVAILABLE, build_toolpack

app = typer.Typer(add_completion=False, help="Moteur d'investigation par taches atomiques.")
console = Console()

ETATS = {"done": "green", "pending": "yellow", "blocked": "red", "skipped": "dim"}


def _ctx(config: str) -> Config:
    return load_config(config)


def _store(cfg: Config, slug: str) -> PlanStore:
    try:
        return find_investigation(cfg.investigations_dir, slug)
    except FileNotFoundError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1)


def _provider(cfg: Config) -> BaseProvider:
    return build_provider(cfg.llm)


def _table_taches(plan: Plan) -> Table:
    table = Table(show_lines=False, header_style="bold")
    for col in ("#", "id", "etat", "titre", "depend de"):
        table.add_column(col)
    for i, t in enumerate(plan.brief.tasks, 1):
        table.add_row(
            str(i),
            t.id,
            f"[{ETATS.get(t.state, 'white')}]{t.state}[/]",
            t.title,
            ", ".join(t.depends_on) or "—",
        )
    return table


def _entete(plan: Plan) -> None:
    console.print(
        Panel(
            f"[bold]{plan.title}[/bold]\n"
            f"slug [cyan]{plan.slug}[/cyan] · statut [bold]{plan.status}[/bold] · "
            f"mode {plan.mode} · toolpack [cyan]{plan.toolpack}[/cyan] · "
            f"modele [cyan]{plan.model or '?'}[/cyan]\n"
            f"{plan.brief.question}",
            expand=False,
        )
    )


# --------------------------------------------------------------------------


@app.command()
def new(
    question: str = typer.Argument(..., help="la question a investiguer"),
    toolpack: str = typer.Option("", "--toolpack", help=f"un de : {', '.join(AVAILABLE)}"),
    owner: str = typer.Option("", "--owner"),
    yes: bool = typer.Option(False, "--yes", "-y", help="sauter les questions de clarification"),
    config: str = typer.Option("config.yml", "--config"),
) -> None:
    """Role 1 — construit la liste de taches. Sortie en statut `ready`."""
    cfg = _ctx(config)
    pack_name = toolpack or cfg.toolpack.default
    if pack_name not in AVAILABLE:
        console.print(f"[red]toolpack inconnu: {pack_name} (connus: {', '.join(AVAILABLE)})[/red]")
        raise typer.Exit(1)

    pack = build_toolpack(pack_name, cfg, {})

    def demander(q: str) -> str:
        console.print(f"\n[bold cyan]?[/bold cyan] {q}")
        return typer.prompt("  votre reponse")

    try:
        plan, usage = build_brief(
            question,
            pack.describe(),
            _provider(cfg),
            cfg,
            ask_human=None if yes else demander,
            toolpack=pack_name,
            owner=owner,
            scope_hint=pack.scope_hint(),
            scope_model=pack.scope_model(),
        )
    except ProviderError as exc:
        console.print(f"[red]le modele n'a pas produit de brief exploitable :[/red]\n{exc}")
        raise typer.Exit(1)

    store = PlanStore.create(cfg.investigations_dir, plan)
    _registre(cfg, plan, store)

    console.print()
    _entete(plan)
    console.print(_table_taches(plan))
    console.print(f"\n[dim]{usage.total} tokens · {store.plan_path}[/dim]")
    console.print(
        f"\n[bold yellow]GATE 1[/bold yellow] — relis la liste : "
        f"[cyan]inv tasks {plan.slug} --edit[/cyan], puis [cyan]inv run {plan.slug}[/cyan]"
    )


def _registre(cfg: Config, plan: Plan, store: PlanStore) -> None:
    """Tient a jour investigations/README.md, le registre humain."""
    readme = cfg.investigations_dir / "README.md"
    ligne = (
        f"| {plan.created_at.date()} | [`{plan.slug}`]({store.dir.name}/plan.md) | "
        f"{plan.title} | {plan.toolpack} | {plan.status} |\n"
    )
    if not readme.exists():
        readme.write_text(
            "# Registre des investigations\n\n"
            "Une ligne par enquete. La verite reste dans chaque `plan.md`.\n\n"
            "| date | slug | titre | toolpack | statut a la creation |\n"
            "|---|---|---|---|---|\n",
            encoding="utf-8",
        )
    with readme.open("a", encoding="utf-8") as fh:
        fh.write(ligne)


@app.command()
def tasks(
    slug: str,
    edit: bool = typer.Option(False, "--edit", help="ouvrir plan.md dans $EDITOR"),
    config: str = typer.Option("config.yml", "--config"),
) -> None:
    """GATE 1 — affiche (et permet d'editer) la liste avant execution."""
    store = _store(_ctx(config), slug)
    if edit:
        editeur = os.environ.get("EDITOR", "nano")
        subprocess.call([*editeur.split(), str(store.plan_path)])
        try:
            store.load()
        except ValidationError as exc:
            console.print(f"[red]plan.md invalide apres edition — corrige-le :[/red]\n{exc}")
            raise typer.Exit(1)
        console.print("[green]plan.md relu et valide.[/green]")

    plan = store.load()
    _entete(plan)
    console.print(_table_taches(plan))
    for t in plan.brief.tasks:
        console.print(f"\n[bold]{t.id}[/bold] — {t.question}")
        console.print(f"  perimetre : [cyan]{t.scope}[/cyan]")
        console.print(f"  attendu   : {t.expected_shape or '—'}")
        console.print(f"  sanity    : {t.sanity_check or '—'}")


@app.command()
def run(
    slug: str,
    retry_blocked: bool = typer.Option(False, "--retry-blocked", help="rejouer les taches bloquees"),
    config: str = typer.Option("config.yml", "--config"),
) -> None:
    """Role 2 — la boucle. Reprend toujours a la premiere tache `pending`."""
    _executer(slug, config, single_step=False, retry_blocked=retry_blocked)


@app.command()
def step(slug: str, config: str = typer.Option("config.yml", "--config")) -> None:
    """Execute UNE tache et s'arrete. Pour deboguer un prompt ou un toolpack."""
    _executer(slug, config, single_step=True, retry_blocked=False)


def _executer(slug: str, config: str, single_step: bool, retry_blocked: bool) -> None:
    cfg = _ctx(config)
    store = _store(cfg, slug)
    plan = store.load()

    if plan.status == "draft":
        console.print("[red]cette investigation est encore en `draft` : passe par `inv tasks`[/red]")
        raise typer.Exit(1)
    if plan.status in ("done", "killed"):
        console.print(f"[red]investigation close (statut `{plan.status}`)[/red]")
        raise typer.Exit(1)
    if not plan.brief.tasks:
        console.print("[red]aucune tache dans le plan[/red]")
        raise typer.Exit(1)

    _entete(plan)

    def evenement(kind: str, payload: dict[str, Any]) -> None:
        if kind == "task_start":
            console.print(f"\n[bold]▶ {payload['task'].id}[/bold] {payload['task'].title}")
        elif kind == "task_end":
            r = payload["result"]
            couleur = "green" if r.status == "done" else "red"
            console.print(
                f"  [{couleur}]{r.status}[/{couleur}] · {r.tool_calls} outil(s) · "
                f"{r.tokens} tokens · {payload['duration']:.1f}s"
            )
            console.print(f"  [dim]{r.insight or r.reason}[/dim]")
        elif kind == "stop_eval" and payload["entry"].fired:
            console.print(f"  [yellow]critere d'arret : {payload['entry'].reason}[/yellow]")

    outcome = loop_module.run(
        store, cfg, _provider(cfg), retry_blocked=retry_blocked,
        single_step=single_step, on_event=evenement,
    )

    console.print(
        f"\n[bold]Arret :[/bold] {outcome.reason} — "
        f"{outcome.tasks_run} tache(s), {outcome.tokens} tokens"
    )
    if not single_step:
        console.print(
            f"\n[bold yellow]GATE 2[/bold yellow] — relis les findings brutes : "
            f"[cyan]inv review {slug}[/cyan]"
        )


@app.command()
def status(slug: str, config: str = typer.Option("config.yml", "--config")) -> None:
    """Etat courant : taches, budget, arrets evalues."""
    plan = _store(_ctx(config), slug).load()
    _entete(plan)
    console.print(_table_taches(plan))

    c = plan.counts()
    console.print(
        f"\n[bold]{c['done']} done[/bold] · {c['pending']} pending · "
        f"{c['blocked']} blocked · {c['skipped']} skipped"
    )
    console.print(
        f"tokens : {plan.tokens_spent()} / {plan.budget_tokens_total} "
        f"({len(plan.logbook.run_log)} entrees de run_log)"
    )
    for s in plan.logbook.stop_log:
        console.print(
            f"[dim]arret apres {s.after_task} · {s.strategy} · "
            f"fired={s.fired} · {s.reason}[/dim]"
        )
    if is_approved(plan):
        console.print("[green]findings approuvees (GATE 2 passee)[/green]")


@app.command()
def review(slug: str, config: str = typer.Option("config.yml", "--config")) -> None:
    """GATE 2 — les findings BRUTES, avant toute synthese."""
    store = _store(_ctx(config), slug)
    plan = store.load()
    _entete(plan)

    if not plan.logbook.findings:
        console.print("[yellow]aucune finding : lance `inv run` d'abord.[/yellow]")
        raise typer.Exit(1)

    for f in plan.logbook.findings:
        task = plan.task(f.task_id)
        etat = task.state if task else "?"
        console.print(
            Panel(
                f"[bold]question[/bold] {task.question if task else '—'}\n\n"
                f"[bold]resume[/bold]    {f.summary or '—'}\n"
                f"[bold]insight[/bold]   {f.insight or '—'}\n"
                f"[bold]influence[/bold] {f.influence or '—'}\n"
                f"[dim]traces : {', '.join(f.artifacts) or 'aucune'}[/dim]",
                title=f"{f.task_id} — [{ETATS.get(etat, 'white')}]{etat}[/]",
                expand=False,
            )
        )

    console.print(
        "\nSi ces resultats bruts tiennent la route : "
        f"[cyan]inv approve {slug}[/cyan]. Sinon, corrige le plan et relance."
    )


@app.command()
def approve(
    slug: str,
    note: str = typer.Option("", "--note", help="pourquoi tu approuves"),
    config: str = typer.Option("config.yml", "--config"),
) -> None:
    """GATE 2 — validation humaine explicite des findings brutes."""
    store = _store(_ctx(config), slug)
    if is_approved(store.load()):
        console.print("[yellow]deja approuvee.[/yellow]")
        raise typer.Exit(0)
    store.add_adjustment(
        step=APPROVAL_STEP,
        change="findings brutes approuvees par un humain",
        reason=note or "revue manuelle via `inv review`",
    )
    console.print(f"[green]approuvee.[/green] Tu peux lancer [cyan]inv synthesize {slug}[/cyan].")


@app.command()
def synthesize(slug: str, config: str = typer.Option("config.yml", "--config")) -> None:
    """Role 4 — un appel LLM, snapshot date immuable dans results/."""
    cfg = _ctx(config)
    store = _store(cfg, slug)
    try:
        result, path, usage = run_synthesis(store, cfg, _provider(cfg))
    except PermissionError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1)
    except ProviderError as exc:
        console.print(f"[red]synthese impossible :[/red]\n{exc}")
        raise typer.Exit(1)

    console.print(Panel(result.headline, title="synthese", expand=False))
    console.print(result.answer)
    if result.evidence:
        console.print("\n[bold]Etabli[/bold]")
        for e in result.evidence:
            console.print(f"  · {e}")
    if result.caveats:
        console.print("\n[bold]Non etabli[/bold]")
        for c in result.caveats:
            console.print(f"  · {c}")
    if result.recommendation:
        console.print(f"\n[bold]Recommandation[/bold]\n{result.recommendation}")
    console.print(f"\n[dim]{usage.total} tokens · snapshot {path}[/dim]")


@app.command()
def report(slug: str, config: str = typer.Option("config.yml", "--config")) -> None:
    """Role 5 — AUCUN LLM, juste Jinja2."""
    store = _store(_ctx(config), slug)
    path = reporter.build_report(store)
    console.print(f"[green]rapport ecrit :[/green] {path}")


@app.command()
def done(
    slug: str,
    note: str = typer.Option("", "--note"),
    config: str = typer.Option("config.yml", "--config"),
) -> None:
    """Clot l'investigation. Seule commande qui fait passer a `done`, et elle
    est humaine : ni la boucle ni la synthese n'y touchent."""
    store = _store(_ctx(config), slug)
    plan = store.load()
    if not is_approved(plan):
        console.print("[red]non approuvee : passe par `inv review` puis `inv approve`.[/red]")
        raise typer.Exit(1)
    store.add_adjustment("done", "investigation cloturee", note or "decision humaine")
    store.set_status("done")
    console.print(f"[green]{slug} est `done`.[/green]")


@app.command()
def kill(
    slug: str,
    reason: str = typer.Option(..., "--reason", help="pourquoi on arrete"),
    config: str = typer.Option("config.yml", "--config"),
) -> None:
    """Abandonne l'investigation (statut `killed`), en gardant la trace du pourquoi."""
    store = _store(_ctx(config), slug)
    store.add_adjustment("kill", "investigation abandonnee", reason)
    store.set_status("killed")
    console.print(f"[yellow]{slug} est `killed` : {reason}[/yellow]")


@app.command("list")
def lister(config: str = typer.Option("config.yml", "--config")) -> None:
    """Toutes les investigations connues."""
    cfg = _ctx(config)
    table = Table(header_style="bold")
    for col in ("slug", "statut", "taches", "tokens", "titre"):
        table.add_column(col)
    for d in sorted(cfg.investigations_dir.glob("*/")):
        store = PlanStore(d)
        if not store.exists():
            continue
        try:
            plan = store.load()
        except ValidationError:
            table.add_row(d.name, "[red]illisible[/red]", "—", "—", "—")
            continue
        c = plan.counts()
        table.add_row(
            plan.slug,
            plan.status,
            f"{c['done']}/{len(plan.brief.tasks)}",
            str(plan.tokens_spent()),
            plan.title,
        )
    console.print(table)


def main() -> None:  # pragma: no cover
    app()


if __name__ == "__main__":  # pragma: no cover
    main()
