"""L'abstraction `ToolPack` — le seul point de variation du moteur.

Le moteur ne connait AUCUN domaine metier. Il sait seulement qu'un pack :

- se decrit en texte (`describe`) — ce texte est injecte dans le prompt ;
- expose des outils (`tools`) — nom, schema d'arguments, doc ;
- refuse (`validate`) AVANT toute execution ;
- execute (`call`) ;
- produit une trace auditable (`artifact`) : le SQL, la commande, l'URL.
  Cette trace part sur disque en write-only, elle n'est JAMAIS relue par un LLM.

`build_toolpack` importe le module du pack a la demande : lancer le moteur sur
`files` ne charge pas une ligne du pack sql (ni duckdb).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel

from engine.config import Config


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    args_schema: dict[str, Any]  # JSON Schema des arguments

    def render(self) -> str:
        props = self.args_schema.get("properties", {})
        required = set(self.args_schema.get("required", []))
        args = ", ".join(
            f"{n}: {p.get('type', 'any')}" + ("" if n in required else " (optionnel)")
            for n, p in props.items()
        )
        return f"- `{self.name}({args})` — {self.description}"


@dataclass
class ToolResult:
    ok: bool
    content: str = ""  # ce que voit le sous-agent
    error: str = ""
    meta: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def failure(cls, error: str, **meta: Any) -> ToolResult:
        return cls(ok=False, error=error, meta=meta)


@dataclass(frozen=True)
class Ok:
    """Autorisation. `args` peut etre une version normalisee des arguments."""

    args: dict[str, Any] | None = None


@dataclass(frozen=True)
class Refus:
    reason: str


Verdict = Ok | Refus


@runtime_checkable
class ToolPack(Protocol):
    name: str

    def describe(self) -> str: ...
    def tools(self) -> list[ToolSpec]: ...
    def call(self, name: str, args: dict[str, Any]) -> ToolResult: ...
    def validate(self, name: str, args: dict[str, Any]) -> Verdict: ...
    def artifact(self, name: str, args: dict[str, Any], res: ToolResult) -> str | None: ...


# --------------------------------------------------------------------------


class BasePack:
    """Mutualise ce qui ne depend d'aucun metier : le catalogue d'outils,
    le rendu de la description, la troncature des resultats."""

    name = "base"

    def __init__(self, scope: dict[str, Any] | None = None, max_bytes: int = 20000) -> None:
        self.scope = dict(scope or {})
        self.max_bytes = max_bytes

    def tools(self) -> list[ToolSpec]:  # pragma: no cover - surcharge attendue
        return []

    def scope_hint(self) -> str:
        """Format attendu du champ `scope` d'une tache, pour le brief_builder.

        Sans ca, un petit modele laisse `scope` vide et le sous-agent devine
        son perimetre — on l'a vu inventer des chemins qui n'existent pas.
        """
        return "{}"

    def scope_model(self) -> type[BaseModel] | None:
        """Le schema du champ `scope` d'une tache, pour le brief_builder.

        Le moteur ne connait pas ce contenu : il se contente de brancher ce
        modele dans le schema de sortie du role 1, puis de reserialiser en
        dict libre pour le plan. C'est le pack, et lui seul, qui sait.

        Pourquoi un modele et pas une consigne en prose : un `dict` libre
        donne `{"type": "object"}` dans le JSON Schema, qui ne dit RIEN de ce
        qu'il attend. Constate avec gemma4:e4b — il rend `{}` sur 9 taches sur
        9, meme apres trois retries a feedback. Type le champ, et l'information
        est portee par la generation elle-meme, a chaque tache.
        """
        return None

    def tool(self, name: str) -> ToolSpec | None:
        return next((t for t in self.tools() if t.name == name), None)

    def render_tools(self) -> str:
        return "\n".join(t.render() for t in self.tools())

    def check_known(self, name: str) -> Refus | None:
        if self.tool(name) is None:
            known = ", ".join(t.name for t in self.tools())
            return Refus(f"outil inconnu: {name!r}. Outils disponibles: {known}")
        return None

    def check_required(self, name: str, args: dict[str, Any]) -> Refus | None:
        spec = self.tool(name)
        assert spec is not None
        missing = [a for a in spec.args_schema.get("required", []) if not args.get(a)]
        if missing:
            return Refus(f"{name}: arguments obligatoires manquants: {missing}")
        return None

    def truncate(self, text: str) -> str:
        if len(text) <= self.max_bytes:
            return text
        return text[: self.max_bytes] + f"\n... [tronque a {self.max_bytes} caracteres]"


# --------------------------------------------------------------------------


def glob_to_regex(pattern: str) -> re.Pattern[str]:
    """Traduit un glob POSIX en regex, `**` traversant les dossiers.

    Utilise pour verifier une autorisation SANS toucher au disque
    (`validate` ne fait pas d'I/O).
    """
    out: list[str] = []
    i, n = 0, len(pattern)
    while i < n:
        c = pattern[i]
        if c == "*":
            if pattern.startswith("**/", i):
                out.append("(?:.*/)?")
                i += 3
                continue
            if pattern.startswith("**", i):
                out.append(".*")
                i += 2
                continue
            out.append("[^/]*")
        elif c == "?":
            out.append("[^/]")
        else:
            out.append(re.escape(c))
        i += 1
    return re.compile("^" + "".join(out) + "$")


def matches_any(path: str, patterns: list[str]) -> bool:
    return any(glob_to_regex(p).match(path) for p in patterns)


# --------------------------------------------------------------------------


def build_toolpack(name: str, cfg: Config, scope: dict[str, Any] | None = None) -> ToolPack:
    """Fabrique un pack. Import paresseux : `files` ne charge jamais `sql`."""
    if name == "files":
        from engine.toolpacks.files import FilesToolPack

        return FilesToolPack(cfg, scope)
    if name == "sql":
        from engine.toolpacks.sql import SqlToolPack  # importe duckdb, ici seulement

        return SqlToolPack(cfg, scope)
    raise KeyError(f"toolpack inconnu: {name!r} (connus: files, sql)")


AVAILABLE = ("files", "sql")
