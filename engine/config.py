"""Chargement de `config.yml`.

Regle du projet : **les six trous du pattern sont des parametres visibles
ici**, jamais des constantes enfouies dans le code. Si une valeur influe sur
le comportement du moteur, elle a une ligne dans `config.yml`.

Les secrets et l'adressage machine restent dans `.env` et sont interpolés
dans le YAML par `${VAR}` / `${VAR:-defaut}`.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from ruamel.yaml import YAML

_yaml = YAML(typ="safe")
_ENV_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


class Section(BaseModel):
    model_config = ConfigDict(extra="forbid")


class LLMConfig(Section):
    base_url: str = "http://127.0.0.1:8080/v1"
    model: str = "chat"
    api_key: str = ""  # cle obligatoire du service central, fournie par .env
    timeout_s: float = 600.0
    # auto      : tente response_format=json_schema, retombe sur le prompt
    # json_schema / prompt : force l'un des deux
    structured_output: Literal["auto", "json_schema", "prompt"] = "auto"
    # TROU non negociable : retry a feedback sur erreur de validation Pydantic
    max_validation_retries: int = 3
    temperature: float = 0.2
    # Mesure sur le service central (modele "chat", Qwen3.5) : le raisonnement
    # est deja compte dans usage.completion_tokens (il partage le flux de
    # tokens avec le contenu). Laisser a True recompterait ces tokens en
    # double ; utile seulement pour un backend qui les exclut (ex. l'ancien
    # Ollama/gemma4:e4b).
    count_reasoning_tokens: bool = False
    # Ajoute une derniere ligne imperative au prompt. Indispensable avec un
    # modele a raisonnement, qui sinon "pense" et renvoie un contenu vide.
    final_directive: bool = True


class ContextConfig(Section):
    """TROU 1 — ce qui entre dans le contexte VIERGE du sous-agent."""

    max_chars: int = 4000
    include_dependencies: bool = True
    include_watch_outs: bool = True
    include_question: bool = True
    # Nombre max de dependances remontees (la plus recente d'abord).
    max_dependencies: int = 10


class StopConfig(Section):
    """TROU 2 — evaluation du critere d'arret."""

    strategy: Literal["none", "llm", "callable"] = "none"
    # pour strategy=callable : "mon_module:ma_fonction"
    callable: str | None = None
    max_tokens: int = 2000


class AgentConfig(Section):
    """TROU 4 — budget d'iteration du sous-agent."""

    max_tool_calls: int = 5
    max_tokens: int = 20000
    # plafond d'UN appel (le budget ci-dessus couvre toute la tache).
    # Doit rester large : gemma4:e4b consomme du budget en raisonnement.
    max_tokens_per_call: int = 4096


class ChecksConfig(Section):
    """La verification contraignante des resultats.

    `sanity_check` reste une consigne rendue au prompt ; ce sont les `checks`
    d'une tache qui decident. Mesure faite ailleurs : une consigne de relecture
    dans le prompt ne change rien, un modele ne se corrige pas lui-meme.
    """

    # Une tache dont un controle echoue passe `blocked` au lieu de `done`.
    # A false, les controles sont evalues et journalises mais n'arretent rien.
    binding: bool = True


class LoopConfig(Section):
    max_consecutive_blocked: int = 2
    retry_blocked: bool = False


class BriefConfig(Section):
    """Role 1 — le constructeur de brief, conversationnel."""

    # nombre max de questions de clarification, UNE a la fois
    max_rounds: int = 4
    max_tokens: int = 4096
    # Bornes imposees VIA LE SCHEMA : un brief hors bornes est rejete par
    # Pydantic, donc renvoye au modele par le retry a feedback. Sans plancher,
    # un petit modele repond volontiers "1 tache : lister les fichiers".
    min_tasks: int = 3
    max_tasks: int = 10


class SynthesisConfig(Section):
    max_tokens: int = 8192


class FilesPackConfig(Section):
    root: str = "./corpus"
    max_results: int = 50
    max_bytes: int = 20000
    max_file_bytes: int = 200000


class SqlPackConfig(Section):
    database: str = "./data/warehouse.duckdb"
    default_limit: int = 1000
    max_rows: int = 5000
    read_only: bool = True
    # Markdown decrivant la base : le DDL dit les types, le dictionnaire dit ce
    # que les donnees VEULENT DIRE (pieges de modelisation, ordres de grandeur).
    # Injecte dans la description du pack, donc AVANT la question.
    dictionary: str | None = None
    max_dictionary_chars: int = 12000


class ToolpackConfig(Section):
    default: str = "files"
    files: FilesPackConfig = Field(default_factory=FilesPackConfig)
    sql: SqlPackConfig = Field(default_factory=SqlPackConfig)


class PathsConfig(Section):
    investigations: str = "./investigations"


class Config(Section):
    paths: PathsConfig = Field(default_factory=PathsConfig)
    llm: LLMConfig = Field(default_factory=LLMConfig)
    context: ContextConfig = Field(default_factory=ContextConfig)
    stop: StopConfig = Field(default_factory=StopConfig)
    agent: AgentConfig = Field(default_factory=AgentConfig)
    loop: LoopConfig = Field(default_factory=LoopConfig)
    checks: ChecksConfig = Field(default_factory=ChecksConfig)
    brief: BriefConfig = Field(default_factory=BriefConfig)
    synthesis: SynthesisConfig = Field(default_factory=SynthesisConfig)
    toolpack: ToolpackConfig = Field(default_factory=ToolpackConfig)

    # -- chemins resolus relativement au fichier de config
    root: Path = Field(default_factory=Path.cwd, exclude=True)

    def path(self, value: str) -> Path:
        p = Path(value).expanduser()
        return p if p.is_absolute() else (self.root / p).resolve()

    @property
    def investigations_dir(self) -> Path:
        return self.path(self.paths.investigations)


# --------------------------------------------------------------------------


def load_dotenv(path: Path) -> None:
    """Mini-loader `.env` (pas de dependance supplementaire).
    N'ecrase jamais une variable deja presente dans l'environnement."""
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key, val = key.strip(), val.strip().strip('"').strip("'")
        os.environ.setdefault(key, val)


def _interpolate(node: Any) -> Any:
    if isinstance(node, str):
        def sub(m: re.Match[str]) -> str:
            return os.environ.get(m.group(1), m.group(2) if m.group(2) is not None else "")

        return _ENV_RE.sub(sub, node)
    if isinstance(node, dict):
        return {k: _interpolate(v) for k, v in node.items()}
    if isinstance(node, list):
        return [_interpolate(v) for v in node]
    return node


def load_config(path: Path | str = "config.yml") -> Config:
    path = Path(path)
    root = path.parent.resolve() if path.parent != Path("") else Path.cwd()
    load_dotenv(root / ".env")
    raw: dict[str, Any] = {}
    if path.is_file():
        raw = _yaml.load(path.read_text(encoding="utf-8")) or {}
    cfg = Config.model_validate(_interpolate(raw))
    cfg.root = root
    return cfg
