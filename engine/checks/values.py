"""Verification chiffree : la valeur produite est-elle la valeur attendue ?

Ce module est **repris de `scripts/live_scenarios.py` du projet
`data-analyst-agent`** (meme auteur, MIT), decouple de son `Turn` et du payload
HTTP de `/chat`. Le raisonnement sur les tolerances y est le plus travaille de
ce projet-la : on le deplace, on ne le retouche pas.

Ce qu'il faut retenir avant de toucher a quoi que ce soit ici : **la prose et
les tableaux ne se jugent pas pareil**. Une reponse redigee arrondit (« environ
63 % » pour 62,96) ; un tableau porte des valeurs brutes et n'arrondit pas. Les
deux tolerances ci-dessous existent pour cette seule raison, et les confondre
laisse passer des coincidences.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

# Une valeur attendue peut etre un nombre, un texte, ou un tuple d'alternatives
# (un taux est aussi juste rendu en fraction 0.742 qu'en pourcentage 74.2).
Expected = float | str | tuple[float | str, ...]

_NOMBRE_RE = re.compile(r"-?\d[\d   ]*(?:[.,]\d+)?")
_ESPACES_RE = re.compile(r"[   ]")


def numbers_in(text: str) -> list[tuple[float, int]]:
    """Les nombres d'un texte, avec leur nombre de decimales AFFICHEES.

    La precision affichee est ce qui permet de juger : « 63 » est un arrondi
    acceptable de 62.96, « 62.9 » non, et « 887 » n'est pas 891. Virgule
    decimale et separateurs de milliers (espaces) admis.
    """
    trouves: list[tuple[float, int]] = []
    for brut in _NOMBRE_RE.findall(text or ""):
        nettoye = _ESPACES_RE.sub("", brut).replace(",", ".")
        try:
            valeur = float(nettoye)
        except ValueError:
            continue
        trouves.append((valeur, len(nettoye.partition(".")[2])))
    return trouves


def decimals_of(value: float) -> int:
    """Decimales significatives d'une valeur de reference (62.96 -> 2, 891 -> 0)."""
    texte = repr(float(value))
    if "e" in texte or "E" in texte:  # notation scientifique : on ne tronque pas
        return 12
    return len(texte.partition(".")[2].rstrip("0"))


def matches_in_prose(produced: float, decimals: int, expected: float) -> bool:
    """Le nombre ECRIT dans une reponse redigee est-il un arrondi juste ?

    Tolerance : un demi-ulp a la precision du moins precis des deux — le modele
    redige « environ 63 % » pour 62.96, ou « 5,84 » pour 5.8433.

    Sauf si cet arrondi est si grossier qu'il ne discrimine plus rien : « 1 »
    est formellement un arrondi de 0.9681, mais avec +/-0.5 — la moitie de la
    valeur — il ne prouve rien. Au-dela de 5 % de la valeur attendue, on exige
    donc la precision de la reference.

    Une tolerance RELATIVE partout serait l'erreur inverse : 0,5 % de 891 vaut
    +/-4,5 et accepterait « 887 », une erreur de comptage prise pour un arrondi.
    """
    ulp = 0.5 * 10 ** (-min(decimals, decimals_of(expected)))
    if ulp > 0.05 * abs(expected):
        ulp = 0.5 * 10 ** (-decimals_of(expected))
    return abs(produced - expected) <= ulp + 1e-9


def matches_in_table(produced: float, expected: float) -> bool:
    """Un tableau porte des valeurs BRUTES : on compare a la precision de l'attendu.

    Indispensable contre les coincidences : un tableau de 1000 lignes contient
    des milliers de nombres, et la regle indulgente de la prose (+/-0.5 pour un
    attendu a 0 decimale) y ferait passer n'importe quel identifiant valant 97
    pour un taux de 96,81. Un tableau n'arrondit pas — 62.96296296 vaut toujours
    62,96 a sa precision, mais 97 ne vaut pas 96,81.
    """
    ulp = 0.5 * 10 ** (-decimals_of(expected))
    return abs(produced - expected) <= ulp + 1e-9


def table_numbers(tables: Sequence[Mapping[str, Any]]) -> list[float]:
    """Tous les nombres des lignes d'un ou plusieurs tableaux.

    Accepte la forme `{"columns": [...], "rows": [[...], ...]}` — c'est celle
    de `QueryResult` cote data-analyst-agent comme celle que le pack SQL de ce
    depot place dans `ToolResult.meta`.
    """
    nombres: list[float] = []
    for table in tables or ():
        for ligne in table.get("rows") or ():
            for valeur in ligne:
                if isinstance(valeur, bool):
                    continue
                if isinstance(valeur, (int, float)):
                    nombres.append(float(valeur))
    return nombres


def table_text(tables: Sequence[Mapping[str, Any]]) -> str:
    """Le contenu textuel des tableaux, pour retrouver une valeur attendue
    non numerique (un nom de magasin, un jour de la semaine)."""
    morceaux: list[str] = []
    for table in tables or ():
        morceaux.extend(str(c) for c in table.get("columns") or ())
        for ligne in table.get("rows") or ():
            morceaux.extend(str(v) for v in ligne)
    return " ".join(morceaux)


def _present(
    expected: float | str,
    haystack: str,
    prose_numbers: list[tuple[float, int]],
    table_values: list[float],
) -> bool:
    if isinstance(expected, str):
        return expected.lower() in haystack.lower()
    if any(matches_in_prose(n, d, expected) for n, d in prose_numbers):
        return True
    return any(matches_in_table(n, expected) for n in table_values)


def missing_values(
    expected: Sequence[Expected],
    *,
    prose: str = "",
    tables: Sequence[Mapping[str, Any]] = (),
) -> list[str]:
    """Les valeurs de reference sont-elles bien celles produites ?

    Rend la liste des manquantes (vide = tout va bien). `prose` et `tables` sont
    passes SEPAREMENT : c'est ce qui permet d'appliquer a chacun sa tolerance.

    Une entree peut etre un tuple d'alternatives : un taux est aussi juste rendu
    en fraction (0.742) qu'en pourcentage (74.2), et le modele choisit
    librement — l'invariant porte sur la valeur, pas sur cette convention.
    """
    if not expected:
        return []
    haystack = f"{prose} {table_text(tables)}"
    prose_numbers = numbers_in(prose)
    table_values = table_numbers(tables)

    manquantes: list[str] = []
    for attendue in expected:
        variantes = attendue if isinstance(attendue, tuple) else (attendue,)
        if any(_present(v, haystack, prose_numbers, table_values) for v in variantes):
            continue
        libelle = " ou ".join(repr(v) for v in variantes)
        manquantes.append(f"valeur {libelle} absente ou fausse")
    return manquantes
