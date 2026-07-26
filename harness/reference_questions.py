"""Lecture des 13 questions de reference de l'export Maxizoo.

Le fichier `questions_reference.md` n'est **pas** recopie dans ce depot : il vit
dans la branche orpheline `export-db` de `sales-ops-planning-poc`, avec la base
et le dictionnaire. Le choix est celui du projet d'origine — « une copie
versionnee deriverait de lui en silence ». On le lit au runtime.

Point de conception important : **les reponses attendues ne sont pas parsees**.
Le document est regulier sur trois choses (le titre `## Q<n>`, la question en
citation, le bloc SQL) et irregulier sur les reponses — gras pour un scalaire,
tableau markdown pour un resultat multiligne, prose ailleurs. On **execute donc
le SQL de reference** sur la base : c'est la definition meme de la reponse de
reference (« chaque reponse a ete produite en executant la requete sur les
fichiers de tables/ »), et c'est sans derive par construction.

Le parseur **refuse bruyamment** : ni 13 questions, ni un bloc SQL manquant, ni
une requete de reference qui ne s'execute pas ne passent en silence.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

from engine.checks.values import Expected
from engine.toolpacks.sql import _blocs_parentheses, referenced_tables

_JOINT_RE = re.compile(r"\bjoin\b", re.IGNORECASE)
_GROUPE_PAR_RE = re.compile(
    r"\bgroup\s+by\b(.*?)(?:\border\s+by\b|\bhaving\b|$)", re.IGNORECASE | re.DOTALL
)

NB_QUESTIONS_ATTENDU = 13

_SECTION_RE = re.compile(r"^## (Q\d+)\s+—\s+(.+?)\s*$", re.MULTILINE)
_CITATION_RE = re.compile(r"^> (?!\*\*Le piège|\*\*La vérification)(.*)$", re.MULTILINE)
_SQL_RE = re.compile(r"```sql\n(.*?)```", re.DOTALL)
_PIEGE_RE = re.compile(r"^> \*\*Le piège\s*:\*\*\s*(.*?)(?=\n\n|\n---|\Z)", re.MULTILINE | re.DOTALL)


@dataclass
class ReferenceQuestion:
    """Une question de reference, telle que l'export la definit."""

    id: str  # "Q5"
    kind: str  # "moyen · jointure de deux faits de grains différents"
    question: str  # la question en langage naturel
    sql: str  # le SQL verifie
    tables: set[str] = field(default_factory=set)  # perimetre, deduit du SQL
    trap: str = ""  # le piege documente, quand il y en a un
    expected: list[Expected] = field(default_factory=list)  # rempli par `resolve`
    columns: list[str] = field(default_factory=list)
    rows: list[list[Any]] = field(default_factory=list)

    @property
    def numero(self) -> int:
        return int(self.id[1:])


def derive_scope(question: ReferenceQuestion) -> dict[str, Any]:
    """Le perimetre d'une tache, deduit de la SOLUTION de reference.

    C'est une facilite du harnais, pas une capacite de production : en vrai,
    c'est le decomposeur (ou un humain a la GATE 1) qui declare le perimetre.
    Ici on le derive de la forme du SQL de reference, pour mesurer ce que le
    perimetre apporte **sans** le regler a la main question par question.

    Quand la solution de reference agrege un fait dans un CTE avant de le
    joindre, c'est que le grain compte : on le declare, et le controle
    d'agregation prealable refusera la forme naive.
    """
    scope: dict[str, Any] = {"tables": sorted(question.tables)}
    grain, faits = _grain_de_reference(question.sql)
    if grain and faits:
        scope["grain"] = grain
        scope["aggregate_before_join"] = sorted(faits)
    return scope


def _grain_de_reference(sql: str) -> tuple[list[str], set[str]]:
    """Le grain d'agregation de la solution, et les faits qu'elle agrege.

    Ne rend quelque chose que si la solution agrege ET joint : sans jointure,
    aucune mesure ne peut etre dupliquee, donc rien a contraindre.
    """
    if not _JOINT_RE.search(sql):
        return [], set()
    for bloc in _blocs_parentheses(sql):
        groupes = _GROUPE_PAR_RE.search(bloc)
        if groupes is None:
            continue
        colonnes = [c.strip() for c in groupes.group(1).split(",")]
        colonnes = [c for c in colonnes if re.fullmatch(r"[a-zA-Z_]\w*", c)]
        tables = referenced_tables(bloc)
        if len(colonnes) >= 2 and tables:
            return colonnes, tables
    return [], set()


def _nettoyer_question(bloc: str) -> str:
    """La citation qui suit le titre, sur une ou plusieurs lignes, sans le gras."""
    lignes = []
    for ligne in _CITATION_RE.findall(bloc):
        ligne = ligne.strip()
        if not ligne:
            break
        lignes.append(ligne)
        if ligne.endswith("**"):  # fin du gras : la question s'arrete la
            break
    texte = " ".join(lignes).strip()
    return texte.strip("*").strip()


def parse_questions(markdown: str) -> list[ReferenceQuestion]:
    """Decoupe le document. Ne touche ni au disque ni a la base."""
    sections = list(_SECTION_RE.finditer(markdown))
    if not sections:
        raise ValueError("aucune section `## Q<n> — ...` trouvee")

    questions: list[ReferenceQuestion] = []
    for i, entete in enumerate(sections):
        fin = sections[i + 1].start() if i + 1 < len(sections) else len(markdown)
        bloc = markdown[entete.end() : fin]

        sql = _SQL_RE.search(bloc)
        if sql is None:
            raise ValueError(f"{entete.group(1)} : aucun bloc SQL")
        question = _nettoyer_question(bloc)
        if not question:
            raise ValueError(f"{entete.group(1)} : aucune question en citation")
        piege = _PIEGE_RE.search(bloc)

        requete = sql.group(1).strip()
        questions.append(
            ReferenceQuestion(
                id=entete.group(1),
                kind=entete.group(2),
                question=question,
                sql=requete,
                tables=referenced_tables(requete),
                trap=" ".join(piege.group(1).split()).replace("> ", "") if piege else "",
            )
        )
    return questions


def resolve(questions: list[ReferenceQuestion], database: Path) -> list[ReferenceQuestion]:
    """Execute chaque SQL de reference et en fait la reponse attendue.

    C'est ici, et nulle part ailleurs, que naissent les valeurs de controle.
    Une requete qui ne s'execute pas fait echouer le chargement : une derive de
    l'export doit etre bruyante.
    """
    import duckdb

    if not database.is_file():
        raise FileNotFoundError(
            f"base introuvable : {database}. Construis-la avec "
            "`uv run python -m harness.load_base --export data/export`"
        )

    con = duckdb.connect(str(database), read_only=True)
    try:
        for q in questions:
            try:
                curseur = con.execute(q.sql)
                q.columns = [d[0] for d in curseur.description or []]
                q.rows = [list(ligne) for ligne in curseur.fetchall()]
            except Exception as exc:  # duckdb.Error et derives
                raise ValueError(
                    f"{q.id} : le SQL de reference ne s'execute pas — "
                    f"{str(exc).strip().splitlines()[0]}"
                ) from exc
            q.expected = _valeurs_de_controle(q.columns, q.rows)
    finally:
        con.close()
    return questions


MAX_LIGNES_INTEGRALES = 4


def _valeurs_de_controle(columns: list[str], rows: list[list[Any]]) -> list[Expected]:
    """Les valeurs qu'une reponse juste doit contenir.

    Exiger *tous* les nombres d'un resultat ferait echouer une reponse correcte
    mais resumee ; n'en exiger aucun ne verifierait rien. Le compromis :

    - resultat court (<= 4 lignes) : on exige tout, la reponse tient entiere ;
    - resultat long : on exige la premiere ligne **et l'extremum de chaque
      colonne numerique**.

    L'extremum n'est pas un ornement. Sur une requete triee par CA decroissant,
    la premiere ligne EST la reponse. Mais Q7 (« quel jour vend le mieux ? »)
    est triee par jour de semaine : sa premiere ligne est lundi, et exiger les
    chiffres de lundi validerait une reponse qui rate justement le samedi. Le
    maximum de la colonne rattrape ce cas sans connaitre la question.
    """
    valeurs: list[Expected] = []
    if not rows:
        return valeurs

    if len(rows) <= MAX_LIGNES_INTEGRALES:
        for ligne in rows:
            valeurs.extend(_valeurs_de_ligne(ligne))
        return _dedoublonner(valeurs)

    valeurs.extend(_valeurs_de_ligne(rows[0]))
    for index in range(len(columns)):
        colonne = [n for n in (_nombre(ligne[index]) for ligne in rows) if n is not None]
        if len(colonne) == len(rows) and len(set(colonne)) > 1:
            valeurs.append(max(colonne))
    return _dedoublonner(valeurs)


def _valeurs_de_ligne(ligne: list[Any]) -> list[Expected]:
    nombres = [n for n in (_nombre(v) for v in ligne) if n is not None]
    if nombres:
        return list(nombres)
    # Une ligne sans aucun nombre (identifiants, dates) : ce sont ses valeurs
    # textuelles qui portent la reponse.
    return [str(v) for v in ligne if v not in (None, "")]


def _dedoublonner(valeurs: list[Expected]) -> list[Expected]:
    vues: set[Any] = set()
    uniques: list[Expected] = []
    for valeur in valeurs:
        if valeur in vues:
            continue
        vues.add(valeur)
        uniques.append(valeur)
    return uniques


def _nombre(valeur: Any) -> float | None:
    """Un nombre exploitable, ou None. DuckDB rend des `Decimal` sur les
    agregats arrondis : sans cette conversion, le CA de Q1 disparaissait
    silencieusement des valeurs de controle."""
    if isinstance(valeur, bool):
        return None
    if isinstance(valeur, (int, float, Decimal)):
        return float(valeur)
    return None


def load_reference_questions(export: Path, database: Path) -> list[ReferenceQuestion]:
    """Charge, controle le compte, et resout les reponses attendues."""
    chemin = export / "questions_reference.md"
    if not chemin.is_file():
        raise FileNotFoundError(f"export incomplet : {chemin} est absent")

    questions = parse_questions(chemin.read_text(encoding="utf-8"))
    if len(questions) != NB_QUESTIONS_ATTENDU:
        raise ValueError(
            f"{len(questions)} questions trouvees, {NB_QUESTIONS_ATTENDU} attendues — "
            "l'export a derive, on ne mesure rien sur une base mouvante"
        )
    return resolve(questions, database)


# --------------------------------------------------------------------------
# Les ordres de grandeur du dictionnaire, transcrits en controles
# --------------------------------------------------------------------------
#
# Ils viennent de la table « Ordres de grandeur (pour detecter une requete
# fausse) » de `dictionnaire.md`, et de nulle part ailleurs. Deux consequences
# a assumer :
#
# 1. Ils n'utilisent AUCUNE reponse de reference. Verifier une reponse avec la
#    reponse serait circulaire, et ne prouverait rien sur une question inedite.
#    Ceux-ci sont exactement ce dont on dispose en production.
# 2. Ils ne couvrent que ce que le dictionnaire documente — 5 questions sur 13.
#    Les 8 autres ne sont pas protegees. C'est une limite du dictionnaire, pas
#    du mecanisme, et elle se comble en enrichissant le dictionnaire.
#
# L'association « ce repere contraint cette question » est faite a la main :
# c'est precisement le travail qu'un humain fait a la GATE 1 en relisant le plan.

# `column_pattern` designe la grandeur contrainte. Sans lui, un repere en
# pourcentage juge aussi les comptages de la meme reponse et bloque a tort —
# constate sur Q8, dont la reponse juste porte 3041 lignes, 1,08 % et 42 066 EUR.
ORDRES_DE_GRANDEUR: dict[str, dict[str, Any]] = {
    "Q1": {
        "label": "CA 2025 (~10,19 M EUR)",
        "column_pattern": r"ca|chiffre|revenue|montant|total",
        "low": 9.0e6,
        "high": 11.5e6,
    },
    "Q4": {
        "label": "part e-commerce (13,3 % -> 20,3 %)",
        "column_pattern": r"part|pct|percent|taux|share",
        "low": 10.0,
        "high": 25.0,
    },
    "Q5": {
        "label": "panier article (2,06 -> 2,76)",
        "column_pattern": r"panier|basket|article.*moyen|items?_per",
        "low": 1.8,
        "high": 3.2,
    },
    "Q8": {
        "label": "part des lignes en rupture (~1,1 %)",
        "column_pattern": r"pct|percent|part|taux|share|ratio",
        "low": 0.5,
        "high": 2.0,
    },
    "Q10": {
        "label": "uplift Black Friday (x1,5 a x2,0)",
        "column_pattern": r"uplift|ratio|facteur",
        "low": 1.2,
        "high": 2.5,
    },
}


# Au-dela de quoi on ne contraint plus le nombre de lignes : sur un resultat
# long, une reponse correcte mais resumee est legitime.
MAX_LIGNES_CONTRAINTES = 20


def checks_du_dictionnaire(question: ReferenceQuestion) -> list[dict[str, Any]]:
    """Les controles disponibles SANS connaitre la reponse."""
    repere = ORDRES_DE_GRANDEUR.get(question.id)
    if repere is None:
        return []
    return [{"kind": "magnitude", **repere}]


def checks_de_forme(question: ReferenceQuestion) -> list[dict[str, Any]]:
    """La FORME attendue du resultat, deduite du resultat de reference.

    Ce sont les controles qui adressent le mode de defaillance dominant mesure :
    l'agent ne rend pas un nombre aberrant, il ne rend **pas la grandeur
    demandee** — il repond a une question voisine, et sa reponse a une dimension
    de moins. Un controle d'ordre de grandeur n'y peut rien ; un controle de
    forme, si.

    On compte les colonnes, on ne les nomme pas : le modele les nomme librement,
    exiger un nom exact bloquerait du travail juste. Et la forme n'exige aucune
    VALEUR de reference : en production, c'est `expected_shape` que le
    decomposeur ou l'humain de la GATE 1 declare.

    **Les deux controles bloquent parfois du juste, et c'est assume.** Mesure a
    l'appui sur les 13 questions : `column_count` bloque a raison Q3, Q8 et Q9,
    a tort Q6 — dont le SQL de reference rend quatre colonnes alors que la
    question n'en demande que deux. `row_count` bloque a raison Q7 et Q11, a
    tort aucune (Q12 rend 128 lignes pour 12 attendues : elle n'a pas fait la
    reduction demandee). Rapport global : cinq vrais pour un faux, et `faux`
    tombe de 6 a 1.

    L'echange est retenu parce que `faux` est la metrique a conduire a zero.
    Il n'est pas gratuit pour autant : D-9 dit qu'un controle qui refuse du
    juste apprend a ignorer les blocages. C'est pourquoi le faux positif est
    nomme ici plutot que noye dans un taux.
    """
    controles: list[dict[str, Any]] = []
    if question.columns:
        controles.append(
            {
                "kind": "column_count",
                "label": f"{len(question.columns)} dimension(s) attendue(s)",
                "min_columns": len(question.columns),
            }
        )
    if question.rows and len(question.rows) <= MAX_LIGNES_CONTRAINTES:
        controles.append(
            {
                "kind": "row_count",
                "label": f"{len(question.rows)} ligne(s) attendue(s)",
                "min_rows": len(question.rows),
                "max_rows": len(question.rows),
            }
        )
    return controles
