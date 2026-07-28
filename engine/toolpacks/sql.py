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


class SqlScope(BaseModel):
    """Le perimetre d'une tache, cote base. Schema vu par le brief_builder."""

    tables: list[str] = Field(
        ...,
        min_length=1,
        description="Noms des tables que cette tache a le droit d'interroger.",
    )


def strip_sql(sql: str) -> str:
    """Retire commentaires et point-virgule final : on raisonne sur le sens,
    pas sur la mise en forme."""
    return _COMMENTAIRES.sub(" ", sql or "").strip().rstrip(";").strip()


class SqlToolPack(BasePack):
    name = "sql"

    def __init__(self, cfg: Config, scope: dict[str, Any] | None = None) -> None:
        pack_cfg = cfg.toolpack.sql
        super().__init__(scope, max_bytes=cfg.toolpack.files.max_bytes)
        self.cfg = pack_cfg
        self.database = cfg.path(pack_cfg.database)
        self.tables: list[str] = [str(t) for t in (self.scope.get("tables") or [])]

    # ---- description injectee au prompt ---------------------------------

    def describe(self) -> str:
        tables = ", ".join(f"`{t}`" for t in self.tables) or "(aucune — tout sera refuse)"
        return (
            "Tu interroges une base duckdb en lecture seule.\n"
            f"Tables autorisees pour CETTE tache : {tables}\n"
            "Toute requete qui n'est pas un SELECT/WITH, ou qui touche une autre table, "
            "est refusee avant execution.\n"
            f"Un LIMIT {self.cfg.default_limit} est ajoute si tu n'en mets pas.\n"
            "Commence par `schema()` si tu ne connais pas les colonnes.\n\n"
            "Outils :\n" + self.render_tools()
        )

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

        # tables reellement referencees, moins les CTE definies dans la requete
        ctes = {c.lower() for c in _CTES.findall(sql)}
        referencees = {t.lower() for t in _TABLES.findall(sql)} - ctes
        autorisees = {t.lower() for t in self.tables}
        if hors_perimetre := sorted(referencees - autorisees):
            return Refus(
                f"tables hors perimetre : {hors_perimetre}. "
                f"Cette tache n'autorise que {sorted(autorisees)}"
            )

        sql = self.inject_limit(sql)

        # EXPLAIN : duckdb valide la syntaxe et les colonnes avant qu'on execute
        if erreur := self.explain(sql):
            return Refus(f"requete rejetee par duckdb (EXPLAIN) : {erreur}")

        return Ok(args={"sql": sql})

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
