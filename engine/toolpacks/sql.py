"""`SqlToolPack` — investigation sur une base duckdb.

Existe pour prouver la genericite : il se branche sans qu'une ligne de
`loop.py`, `agent.py`, `plan.py` ou `context.py` ne change. Il est aussi le
seul module du projet a importer duckdb, et cet import n'a lieu que si le pack
est demande (`build_toolpack("sql", ...)`).

Outils : `schema()`, `query(sql)`.

`validate` refuse AVANT execution :
- tout ce qui n'est pas un SELECT ou un WITH ;
- toute table non declaree dans `task.scope["tables"]` ;
- les fonctions qui liraient des fichiers hors de la base (read_csv, ...) ;
- les requetes que duckdb lui-meme rejette (un EXPLAIN est passe avant).
Et il injecte un LIMIT quand la requete n'en a pas.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from engine.config import Config
from engine.toolpacks.base import BasePack, Ok, Refus, ToolResult, ToolSpec, Verdict

# Verbes autorises en tete de requete.
_LECTURE = re.compile(r"^\s*(select|with)\b", re.IGNORECASE)

# Tout ce qui ecrit, attache, installe ou lit hors de la base.
_INTERDITS = (
    "insert", "update", "delete", "drop", "create", "alter", "truncate",
    "attach", "detach", "copy", "install", "load", "pragma", "export",
    "import", "call", "set", "reset", "vacuum", "checkpoint", "grant", "revoke",
)
_FONCTIONS_FICHIER = (
    "read_csv", "read_parquet", "read_json", "read_text", "read_blob",
    "glob", "parquet_scan", "csv_scan", "sniff_csv",
)

_TABLES = re.compile(r"\b(?:from|join)\s+([a-zA-Z_][\w$]*(?:\.[a-zA-Z_][\w$]*)*)", re.IGNORECASE)
_CTES = re.compile(r"(?:\bwith\b|,)\s*([a-zA-Z_]\w*)\s+as\s*\(", re.IGNORECASE)
_LIMIT = re.compile(r"\blimit\s+\d+", re.IGNORECASE)
_COMMENTAIRES = re.compile(r"--[^\n]*|/\*.*?\*/", re.DOTALL)

# `EXTRACT(YEAR FROM s.date)`, `SUBSTRING(x FROM 1 FOR 3)`, `TRIM(BOTH ' ' FROM x)` :
# le mot-cle FROM y sert la syntaxe d'une fonction, pas a designer une table.
_FROM_NON_TABLE = re.compile(
    r"\b(?:extract|substring|trim|overlay|position)\s*\([^()]*\)", re.IGNORECASE
)


class SqlScope(BaseModel):
    """Le perimetre d'une tache, cote base. Schema vu par le brief_builder."""

    tables: list[str] = Field(
        ...,
        min_length=1,
        description="Noms des tables que cette tache a le droit d'interroger.",
    )
    grain: list[str] = Field(
        default_factory=list,
        description=(
            "Colonnes du grain de la tache, ex: ['store_id', 'date']. Sert au "
            "controle d'agregation prealable ci-dessous."
        ),
    )
    aggregate_before_join: list[str] = Field(
        default_factory=list,
        description=(
            "Tables de faits a agreger au grain declare AVANT toute jointure, "
            "ex: ['sales_daily']. Joindre le detail directement duplique la "
            "mesure de l'autre table une fois par ligne de detail."
        ),
    )


# Le dictionnaire passe AVANT la question, et non en reponse a un outil : les
# pieges qu'il decrit doivent etre connus au moment d'ecrire la requete, pas
# apres. Un modele qui apprend au 3e tour que le e-commerce est une ligne de
# `stores` a deja rendu son top magasins. (Mecanisme repris de
# data-analyst-agent, ou il fait gagner les pieges qu'il ENONCE.)
DICTIONNAIRE_PROMPT = """
--- Dictionnaire de la source ---
Ce document decrit la base que tu interroges : le sens de chaque colonne, les
valeurs admises, et les pieges de modelisation. Il fait autorite sur le schema
brut. Lis-le avant d'ecrire ta requete, et respecte ses mises en garde meme
quand le SQL « evident » semble marcher : c'est justement la qu'il est faux.

{dictionnaire}
--- fin du dictionnaire ---"""


def _json_sur(valeur: Any) -> Any:
    """Rend une valeur duckdb comparable : Decimal -> float, date -> ISO.
    Sans ca, un `Decimal('10186669.50')` n'est pas un nombre pour un controle."""
    if isinstance(valeur, Decimal):
        return float(valeur)
    if isinstance(valeur, (date, datetime)):
        return valeur.isoformat()
    return valeur


def strip_sql(sql: str) -> str:
    """Retire commentaires et point-virgule final : on raisonne sur le sens,
    pas sur la mise en forme."""
    return _COMMENTAIRES.sub(" ", sql or "").strip().rstrip(";").strip()


def referenced_tables(sql: str) -> set[str]:
    """Les tables reellement interrogees, en minuscules.

    Deux pieges, tous deux constates sur de vraies requetes :

    - les CTE definies par la requete sont soustraites, sinon un
      `WITH q AS (...) SELECT ... FROM q` declencherait un faux « hors
      perimetre » sur `q` ;
    - `EXTRACT(YEAR FROM s.date)` et `SUBSTRING(x FROM 1 FOR 3)` utilisent le
      mot-cle FROM sans designer de table. Sans neutralisation, `s.date` est
      pris pour une table et une requete parfaitement legitime se fait refuser.
    """
    propre = _FROM_NON_TABLE.sub(" ", strip_sql(sql))
    ctes = {c.lower() for c in _CTES.findall(propre)}
    return {t.lower() for t in _TABLES.findall(propre)} - ctes


_GROUP_BY_RE = re.compile(r"\bgroup\s+by\b(.*?)(?:\border\s+by\b|\bhaving\b|$)",
                          re.IGNORECASE | re.DOTALL)
_JOIN_RE = re.compile(r"\bjoin\b", re.IGNORECASE)


def _blocs_parentheses(sql: str) -> list[str]:
    """Le contenu de chaque groupe de parentheses de premier niveau.

    Les CTE et les sous-requetes vivent la. On les extrait pour pouvoir
    distinguer ce qui est agrege de ce qui ne l'est pas.
    """
    blocs: list[str] = []
    profondeur = 0
    debut = 0
    for i, c in enumerate(sql):
        if c == "(":
            if profondeur == 0:
                debut = i + 1
            profondeur += 1
        elif c == ")":
            profondeur -= 1
            if profondeur == 0:
                blocs.append(sql[debut:i])
            elif profondeur < 0:
                profondeur = 0
    return blocs


def agrege_au_grain(bloc: str, grain: list[str]) -> bool:
    """Ce bloc agrege-t-il sur toutes les colonnes du grain ?"""
    if not grain:
        return False
    groupes = _GROUP_BY_RE.search(bloc)
    if groupes is None:
        return False
    clauses = groupes.group(1).lower()
    return all(colonne.lower() in clauses for colonne in grain)


def hors_agregation(sql: str, grain: list[str]) -> str:
    """Le SQL prive de ses blocs deja agreges au grain.

    Ce qui reste est ce qui est joint « en detail ». C'est la-dessus que porte
    le controle : une table de faits qui subsiste ici est jointe sans avoir ete
    agregee, et sa mesure sera dupliquee.
    """
    reste = sql
    for bloc in _blocs_parentheses(sql):
        if agrege_au_grain(bloc, grain):
            reste = reste.replace(f"({bloc})", " ", 1)
    return reste


class SqlToolPack(BasePack):
    name = "sql"

    def __init__(self, cfg: Config, scope: dict[str, Any] | None = None) -> None:
        pack_cfg = cfg.toolpack.sql
        super().__init__(scope, max_bytes=cfg.toolpack.files.max_bytes)
        self.cfg = pack_cfg
        self.database = cfg.path(pack_cfg.database)
        self.tables: list[str] = [str(t) for t in (self.scope.get("tables") or [])]
        # Resultats structures des requetes de CETTE tache, dans l'ordre. Ce
        # sont eux, et jamais la prose du modele, que verifie un controle.
        self.tables_produites: list[dict[str, Any]] = []
        self.grain: list[str] = [str(g) for g in (self.scope.get("grain") or [])]
        self.aggregate_before_join: list[str] = [
            str(t).lower() for t in (self.scope.get("aggregate_before_join") or [])
        ]
        self.dictionary = self._lire_dictionnaire(cfg)

    def _lire_dictionnaire(self, cfg: Config) -> str:
        """Le dictionnaire de la source, s'il est configure.

        Il decrit ce que les donnees VEULENT DIRE : pieges de modelisation,
        ordres de grandeur. Il fait autorite sur le schema brut — le DDL dit les
        types, lui dit le sens.
        """
        if not self.cfg.dictionary:
            return ""
        chemin = cfg.path(self.cfg.dictionary)
        if not chemin.is_file():
            return ""
        texte = chemin.read_text(encoding="utf-8")
        plafond = self.cfg.max_dictionary_chars
        if plafond and len(texte) > plafond:
            texte = texte[:plafond] + "\n[... dictionnaire tronque]"
        return texte

    # ---- description injectee au prompt ---------------------------------

    def describe(self) -> str:
        tables = ", ".join(f"`{t}`" for t in self.tables) or "(aucune — tout sera refuse)"
        blocs = [
            "Tu interroges une base duckdb en lecture seule.",
            f"Tables autorisees pour CETTE tache : {tables}",
            "Toute requete qui n'est pas un SELECT/WITH, ou qui touche une autre table, "
            "est refusee avant execution.",
            f"Un LIMIT {self.cfg.default_limit} est ajoute si tu n'en mets pas.",
            "Commence par `schema()` si tu ne connais pas les colonnes.",
        ]
        if self.grain:
            blocs.append(
                f"Grain impose a cette tache : {', '.join(f'`{g}`' for g in self.grain)}."
            )
        if self.aggregate_before_join:
            cibles = ", ".join(f"`{t}`" for t in self.aggregate_before_join)
            blocs.append(
                f"AVANT de joindre {cibles}, agrege les faits au grain ci-dessus dans un "
                "CTE. Joindre directement duplique la mesure une fois par ligne de detail."
            )
        if self.dictionary:
            blocs.append(DICTIONNAIRE_PROMPT.format(dictionnaire=self.dictionary))
        blocs.append("Outils :\n" + self.render_tools())
        return "\n".join(blocs)

    def scope_hint(self) -> str:
        return (
            '{"tables": [...]} — la liste des tables que la tache a le droit '
            "d'interroger. Toute autre table est refusee avant execution."
        )

    def scope_model(self) -> type[BaseModel]:
        return SqlScope

    def tools(self) -> list[ToolSpec]:
        return [
            ToolSpec(
                name="schema",
                description="colonnes et types des tables autorisees",
                args_schema={"type": "object", "properties": {}, "required": []},
            ),
            ToolSpec(
                name="query",
                description="execute un SELECT et renvoie les lignes",
                args_schema={
                    "type": "object",
                    "properties": {"sql": {"type": "string", "description": "un SELECT ou WITH"}},
                    "required": ["sql"],
                },
            ),
        ]

    # ---- garde-fou -------------------------------------------------------

    def validate(self, name: str, args: dict[str, Any]) -> Verdict:
        refus = self.check_known(name) or self.check_required(name, args)
        if refus:
            return refus
        if not self.tables:
            return Refus(
                "aucune table declaree dans le perimetre de la tache "
                "(scope.tables) : rien n'est interrogeable"
            )
        if name == "schema":
            return Ok()

        sql = strip_sql(str(args["sql"]))
        if not sql:
            return Refus("requete vide")
        if ";" in sql:
            return Refus("une seule instruction par appel (point-virgule interdit au milieu)")
        if not _LECTURE.match(sql):
            return Refus(
                f"lecture seule : la requete doit commencer par SELECT ou WITH, "
                f"pas par {sql.split()[0].upper()!r}"
            )

        mots = set(re.findall(r"[a-zA-Z_]\w*", sql.lower()))
        if interdits := sorted(mots & set(_INTERDITS)):
            return Refus(f"mots-cles interdits en lecture seule : {interdits}")
        if fichiers := sorted(mots & set(_FONCTIONS_FICHIER)):
            return Refus(
                f"fonctions de lecture de fichiers interdites : {fichiers} "
                "(elles contourneraient les tables declarees)"
            )

        referencees = referenced_tables(sql)
        autorisees = {t.lower() for t in self.tables}
        if hors_perimetre := sorted(referencees - autorisees):
            return Refus(
                f"tables hors perimetre : {hors_perimetre}. "
                f"Cette tache n'autorise que {sorted(autorisees)}"
            )

        if refus_grain := self._controler_le_grain(sql):
            return refus_grain

        sql = self.inject_limit(sql)

        # EXPLAIN : duckdb valide la syntaxe et les colonnes avant qu'on execute
        if erreur := self.explain(sql):
            return Refus(f"requete rejetee par duckdb (EXPLAIN) : {erreur}")

        return Ok(args={"sql": sql})

    def _controler_le_grain(self, sql: str) -> Refus | None:
        """Une table de faits jointe sans agregation prealable duplique sa mesure.

        C'est le piege qui a fait rendre un panier article 60 fois trop petit a
        un agent, et 20 fois trop grand a un autre : l'allowlist ne l'attrape
        pas, puisque la requete naive n'utilise que des tables autorisees.

        Analyse STRUCTURELLE au niveau regex : elle reconnait une forme de SQL,
        pas une semantique. Elle attrape la forme naive ; une reecriture
        equivalente lui echappe. C'est un fil-piege, pas un theoreme.
        """
        if not self.aggregate_before_join or not self.grain:
            return None
        reste = hors_agregation(sql, self.grain)
        if not _JOIN_RE.search(reste):
            return None  # pas de jointure : rien a dupliquer
        fautives = sorted(t for t in self.aggregate_before_join if t in referenced_tables(reste))
        if not fautives:
            return None
        grain = ", ".join(self.grain)
        return Refus(
            f"grain : {fautives} est jointe sans avoir ete agregee. Sa mesure serait "
            f"dupliquee une fois par ligne de detail.\n"
            f"Agrege d'abord dans un CTE : "
            f"`WITH q AS (SELECT {grain}, SUM(...) FROM {fautives[0]} GROUP BY {grain})`, "
            f"puis joins `q`."
        )

    def inject_limit(self, sql: str) -> str:
        if _LIMIT.search(sql):
            return sql
        return f"{sql}\nLIMIT {self.cfg.default_limit}"

    def explain(self, sql: str) -> str | None:
        """Retourne le message d'erreur duckdb, ou None si la requete passe."""
        try:
            with self.connect() as con:
                con.execute(f"EXPLAIN {sql}")
        except FileNotFoundError as exc:
            return str(exc)
        except Exception as exc:  # duckdb.Error et derives
            return str(exc).strip().splitlines()[0]
        return None

    # ---- execution -------------------------------------------------------

    def connect(self):
        import duckdb  # import local : le pack `files` ne le charge jamais

        if not self.database.is_file():
            raise FileNotFoundError(f"base introuvable : {self.database}")
        return duckdb.connect(str(self.database), read_only=self.cfg.read_only)

    def call(self, name: str, args: dict[str, Any]) -> ToolResult:
        try:
            if name == "schema":
                return self._schema()
            if name == "query":
                return self._query(str(args["sql"]))
        except FileNotFoundError as exc:
            return ToolResult.failure(str(exc))
        except Exception as exc:  # duckdb.Error : on rend l'erreur, on ne crashe pas
            return ToolResult.failure(str(exc).strip().splitlines()[0])
        return ToolResult.failure(f"outil non implemente: {name}")

    def _schema(self) -> ToolResult:
        blocs = []
        with self.connect() as con:
            for table in self.tables:
                rows = con.execute(
                    "SELECT column_name, data_type FROM information_schema.columns "
                    "WHERE lower(table_name) = lower(?) ORDER BY ordinal_position",
                    [table],
                ).fetchall()
                if not rows:
                    blocs.append(f"{table} : table absente de la base")
                    continue
                colonnes = ", ".join(f"{c} {t}" for c, t in rows)
                blocs.append(f"{table}({colonnes})")
        return ToolResult(ok=True, content=self.truncate("\n".join(blocs)), meta={"tables": len(self.tables)})

    def _query(self, sql: str) -> ToolResult:
        with self.connect() as con:
            cur = con.execute(sql)
            colonnes = [d[0] for d in cur.description or []]
            lignes = cur.fetchmany(self.cfg.max_rows)

        # Le resultat STRUCTURE est conserve, pas seulement son rendu texte :
        # c'est sur lui que porte toute verification chiffree. Verifier la prose
        # du modele reviendrait a lui demander de se juger lui-meme.
        table = {
            "columns": list(colonnes),
            "rows": [[_json_sur(v) for v in ligne] for ligne in lignes],
        }
        self.tables_produites.append(table)

        rendu = [" | ".join(colonnes), "-|-".join("-" * len(c) for c in colonnes)]
        rendu += [" | ".join("NULL" if v is None else str(v) for v in ligne) for ligne in lignes]
        corps = "\n".join(rendu) if colonnes else "(aucune colonne)"
        if len(lignes) >= self.cfg.max_rows:
            corps += f"\n... tronque a {self.cfg.max_rows} lignes"
        return ToolResult(
            ok=True,
            content=self.truncate(corps),
            meta={"rows": len(lignes), "columns": len(colonnes)},
        )

    # ---- trace auditable (write-only) ------------------------------------

    def artifact(self, name: str, args: dict[str, Any], res: ToolResult) -> str | None:
        """Le SQL reellement execute, celui d'apres injection du LIMIT."""
        if name == "schema":
            corps = f"-- schema des tables : {', '.join(self.tables)}"
        elif name == "query":
            corps = str(args.get("sql", ""))
        else:
            return None
        status = "OK" if res.ok else f"ERREUR: {res.error}"
        meta = " ".join(f"{k}={v}" for k, v in res.meta.items())
        return f"-- base: {self.database}\n-- {status}\n-- {meta}\n{corps};\n"


def create_demo_database(path: Path) -> Path:
    """Petite base de demonstration (utilisee par les tests et le README)."""
    import duckdb

    path.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(path))
    con.execute("CREATE OR REPLACE TABLE ventes (id INTEGER, segment TEXT, montant DOUBLE, trimestre TEXT)")
    con.execute(
        "INSERT INTO ventes VALUES "
        "(1,'Entreprise',1200.0,'T3'),(2,'PME',300.0,'T3'),"
        "(3,'Entreprise',900.0,'T2'),(4,'PME',450.0,'T2')"
    )
    con.execute("CREATE OR REPLACE TABLE secrets (id INTEGER, valeur TEXT)")
    con.execute("INSERT INTO secrets VALUES (1,'ne doit jamais sortir')")
    con.close()
    return path
