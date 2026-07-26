"""La porte : un resultat qui echoue a son controle ne passe pas `done`.

C'est le coeur du dispositif, et il tient dans une phrase : **on ne demande pas
au modele de se verifier, on verifie**.

La distinction n'est pas theorique. Le projet `data-analyst-agent` a essaye
d'ajouter au prompt « si tu es loin de l'ordre de grandeur documente, ta requete
est fausse ». Mesure faite : meme requete naive, meme resultat, aucune
auto-correction en deux essais. La consigne a ete retiree. Un controle, lui, est
du code — il lit le resultat STRUCTURE rendu par la base, pas la prose du
modele, et il n'a pas d'avis.

Quatre familles, par ordre d'utilite decroissante en production :

- `magnitude`  : un intervalle plausible. **N'exige aucune reponse de
  reference**, donc fonctionne sur une question inedite. Le seul qui protege
  vraiment en production.
- `row_count`  : la forme attendue du resultat, devenue du code.
- `columns`    : les dimensions demandees sont-elles la ? Attrape « la question
  avait deux dimensions et la reponse en a perdu une ».
- `expect_values` : des valeurs connues d'avance. Reserve au harnais
  d'evaluation — en production, si on connaissait la reponse, on ne poserait
  pas la question.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from engine.checks.values import missing_values, table_numbers
from engine.schemas import Check, CheckOutcome, Task, TaskResult


@dataclass
class CheckReport:
    outcomes: list[CheckOutcome] = field(default_factory=list)

    @property
    def failed(self) -> bool:
        return bool(self.failures)

    @property
    def failures(self) -> list[CheckOutcome]:
        """Seuls les controles APPLICABLES peuvent echouer."""
        return [o for o in self.outcomes if o.applicable and not o.passed]

    @property
    def inapplicable(self) -> list[CheckOutcome]:
        return [o for o in self.outcomes if not o.applicable]

    def reason(self) -> str:
        """Le motif de blocage, ecrit pour un humain qui relira le plan."""
        echecs = self.failures
        if not echecs:
            return ""
        tete = f"{len(echecs)} controle(s) en echec : "
        return tete + " ; ".join(f"{o.label or o.kind} — {o.detail}" for o in echecs)


def verify(
    checks: Sequence[Check],
    *,
    prose: str = "",
    tables: Sequence[Mapping[str, Any]] = (),
) -> CheckReport:
    """Applique les controles d'une tache a son resultat.

    `tables` porte les resultats structures produits par les outils. Un controle
    numerique qui ne trouve aucun tableau **echoue** plutot que de se rabattre
    sur la prose : une affirmation chiffree sans donnee derriere est exactement
    ce qu'on cherche a bloquer.
    """
    rapport = CheckReport()
    for check in checks:
        rapport.outcomes.append(_appliquer(check, prose, tables))
    return rapport


def _appliquer(check: Check, prose: str, tables: Sequence[Mapping[str, Any]]) -> CheckOutcome:
    if check.kind == "expect_values":
        manquantes = missing_values(check.values, prose=prose, tables=tables)
        return _issue(check, not manquantes, "; ".join(manquantes) or "toutes les valeurs sont la")

    if check.kind == "magnitude":
        return _magnitude(check, tables)

    if check.kind == "row_count":
        if not tables:
            return _issue(check, False, "aucun resultat : aucune ligne produite")
        # la table la plus fournie, meme raison que ci-dessus
        lignes = max(len(t.get("rows") or ()) for t in tables)
        trop_peu = check.min_rows is not None and lignes < check.min_rows
        trop = check.max_rows is not None and lignes > check.max_rows
        attendu = f"[{check.min_rows if check.min_rows is not None else '-'}, " \
                  f"{check.max_rows if check.max_rows is not None else '-'}]"
        return _issue(
            check,
            not (trop_peu or trop),
            f"{lignes} ligne(s) produites, attendu {attendu}",
        )

    if check.kind == "column_count":
        if not tables:
            return _issue(check, False, "aucun resultat : aucune dimension produite")
        # la table la plus large : le modele peut avoir produit des resultats
        # intermediaires plus etroits avant le bon
        colonnes = max(len(t.get("columns") or ()) for t in tables)
        trop_peu = check.min_columns is not None and colonnes < check.min_columns
        trop = check.max_columns is not None and colonnes > check.max_columns
        attendu = f"[{check.min_columns if check.min_columns is not None else '-'}, " \
                  f"{check.max_columns if check.max_columns is not None else '-'}]"
        manque = " — une dimension demandee manque" if trop_peu else ""
        return _issue(
            check, not (trop_peu or trop), f"{colonnes} colonne(s), attendu {attendu}{manque}"
        )

    if check.kind == "columns":
        presentes = {str(c).lower() for t in tables for c in (t.get("columns") or ())}
        absentes = sorted(n for n in check.names if n.lower() not in presentes)
        return _issue(
            check,
            not absentes,
            f"colonnes absentes : {absentes}" if absentes else "toutes les colonnes sont la",
        )

    return _issue(check, False, f"type de controle inconnu : {check.kind!r}")  # pragma: no cover


def _magnitude(check: Check, tables: Sequence[Mapping[str, Any]]) -> CheckOutcome:
    """La valeur produite tient-elle dans l'ordre de grandeur documente ?

    Si une colonne est nommee, on ne juge qu'elle ; sinon on juge tous les
    nombres du resultat. Le second cas est volontairement severe : sans colonne
    designee, un seul nombre aberrant suffit a bloquer.
    """
    if not any(t.get("rows") for t in tables):
        # Rien n'a ete mesure : une affirmation chiffree sans donnee derriere
        # est exactement ce qu'on veut bloquer.
        return _issue(check, False, "aucun resultat chiffre : rien a verifier")

    valeurs = _valeurs_a_juger(check, tables)
    if not valeurs:
        # Des donnees existent, mais la grandeur contrainte n'y est pas
        # identifiable. Bloquer serait accuser a tort ; laisser passer en
        # silence serait mentir sur la couverture. On le dit.
        cible = check.column or check.column_pattern
        return _issue(
            check,
            True,
            f"non verifie : aucune colonne ne correspond a `{cible}` dans le resultat",
            applicable=False,
        )

    bornes = f"[{check.low if check.low is not None else '-'}, " \
             f"{check.high if check.high is not None else '-'}]"
    hors = [
        v
        for v in valeurs
        if (check.low is not None and v < check.low) or (check.high is not None and v > check.high)
    ]
    if hors:
        exemple = hors[0]
        ecart = _facteur(exemple, check)
        return _issue(
            check,
            False,
            f"{exemple:g} est hors de l'ordre de grandeur attendu {bornes}{ecart}",
        )
    return _issue(check, True, f"{len(valeurs)} valeur(s) dans {bornes}")


def _valeurs_a_juger(check: Check, tables: Sequence[Mapping[str, Any]]) -> list[float]:
    """Les nombres soumis au controle.

    Sans colonne ni motif, on juge tout — severe, et correct seulement quand le
    resultat ne porte qu'une seule grandeur.
    """
    if not check.column and not check.column_pattern:
        return table_numbers(tables)

    motif = (
        re.compile(check.column_pattern, re.IGNORECASE)
        if check.column_pattern
        else re.compile(rf"^{re.escape(check.column or '')}$", re.IGNORECASE)
    )
    valeurs: list[float] = []
    for table in tables:
        colonnes = [str(c) for c in (table.get("columns") or ())]
        index = [i for i, nom in enumerate(colonnes) if motif.search(nom)]
        for ligne in table.get("rows") or ():
            for i in index:
                if i >= len(ligne):
                    continue
                valeur = ligne[i]
                if isinstance(valeur, bool):
                    continue
                if isinstance(valeur, (int, float)):
                    valeurs.append(float(valeur))
    return valeurs


def _facteur(valeur: float, check: Check) -> str:
    """« 60 fois trop petit » en dit plus long qu'« hors bornes »."""
    if check.low is not None and 0 < valeur < check.low:
        return f" — environ {check.low / valeur:.0f} fois trop petit"
    if check.high is not None and valeur > check.high > 0:
        return f" — environ {valeur / check.high:.0f} fois trop grand"
    return ""


def _issue(check: Check, passed: bool, detail: str, applicable: bool = True) -> CheckOutcome:
    return CheckOutcome(
        kind=check.kind,
        label=check.label,
        passed=passed,
        applicable=applicable,
        detail=detail,
    )


# --------------------------------------------------------------------------


def apply_gate(
    result: TaskResult,
    task: Task,
    pack: Any,
    *,
    binding: bool = True,
    on_event: Callable[[str, dict[str, Any]], None] | None = None,
) -> TaskResult:
    """La porte, entre l'execution d'une tache et son ecriture dans le plan.

    Une tache dont un controle echoue passe `blocked` : elle ne debloque donc
    aucune dependante, et son chiffre n'atteint jamais la synthese. C'est la
    seule facon connue d'empecher un resultat faux de circuler — le demander au
    modele dans son prompt a ete essaye ailleurs, et mesure sans effet.

    `binding=False` evalue et journalise sans rien arreter : utile pour mesurer
    ce que les controles attraperaient avant de les rendre bloquants.
    """
    if result.status != "done" or not task.checks:
        return result

    rapport = verify(
        task.checks,
        prose=f"{result.summary}\n{result.insight}",
        tables=getattr(pack, "tables_produites", []),
    )
    if on_event is not None:
        on_event("checks", {"task": task, "report": rapport})
    if rapport.failed and binding:
        return result.blocked_by(rapport.outcomes, rapport.reason())
    return result.model_copy(update={"checks": rapport.outcomes})
