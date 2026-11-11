"""Couche modele.

Contrat unique :

    provider.complete(system, user, schema, max_tokens) -> (instance, Usage)

Le retour est **toujours** une instance validee de `schema`. Le retry a
feedback (on renvoie au modele l'erreur Pydantic exacte) est implemente une
seule fois, dans `BaseProvider`, donc `FakeProvider` l'exerce a l'identique :
tester la boucle sans LLM teste aussi le mecanisme qui rend un petit modele
exploitable.

Cible : le service LLM central (`~/mes_projets/Projet-IA/LLM-Service`), endpoint
OpenAI-compatible `http://127.0.0.1:8080/v1` (ou `http://llm-central:8080/v1` depuis
un conteneur attache au reseau `llm-net`), modele `chat`.
"""

from __future__ import annotations

import json
import re
from abc import ABC, abstractmethod
from typing import Any, Callable, Iterable, Sequence

import httpx
from pydantic import BaseModel, ValidationError

from engine.config import LLMConfig
from engine.schemas import Usage

_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)

# Mesure sur gemma4:e4b (modele a raisonnement) : sans cette ligne finale, il
# termine son raisonnement, conclut en prose "I will use grep", et renvoie un
# `content` VIDE avec finish_reason=stop. Avec elle, il emet le JSON. Cout nul
# sur un modele qui n'en a pas besoin.
FINAL_DIRECTIVE = (
    "\n\nReponds MAINTENANT par l'objet JSON lui-meme, rien d'autre. "
    "Ta reponse commence par { et finit par }."
)


class ProviderError(RuntimeError):
    """Echec definitif apres tous les retries.

    Porte l'`usage` deja consomme : les tokens brules par des tentatives
    ratees doivent quand meme etre imputes au budget de la tache, sinon un
    modele qui derape depense sans laisser de trace.
    """

    def __init__(self, message: str, usage: Usage | None = None) -> None:
        super().__init__(message)
        self.usage = usage or Usage()


# --------------------------------------------------------------------------


def extract_json(text: str) -> str:
    """Isole le premier objet JSON complet d'une reponse bavarde.

    Les petits modeles encadrent volontiers leur JSON de prose ou de fences.
    On tolere les deux ; on ne tolere pas le JSON absent.
    """
    text = (text or "").strip()
    if not text:
        raise ValueError("reponse vide")
    fence = _FENCE_RE.search(text)
    if fence:
        text = fence.group(1).strip()
    start = text.find("{")
    if start == -1:
        raise ValueError("aucun objet JSON dans la reponse")
    depth, in_str, escaped = 0, False, False
    for i, ch in enumerate(text[start:], start=start):
        if in_str:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    raise ValueError("objet JSON non termine")


def schema_hint(schema: type[BaseModel]) -> str:
    return json.dumps(schema.model_json_schema(), ensure_ascii=False, indent=2)


def _flatten_errors(exc: ValidationError) -> str:
    lines = []
    for err in exc.errors():
        loc = ".".join(str(p) for p in err["loc"]) or "(racine)"
        lines.append(f"- champ `{loc}` : {err['msg']}")
    return "\n".join(lines)


# --------------------------------------------------------------------------


class BaseProvider(ABC):
    """Implemente la validation + le retry a feedback. Les sous-classes ne
    fournissent que `_raw` : du texte brut, rien d'autre."""

    def __init__(self, max_validation_retries: int = 3, final_directive: bool = True) -> None:
        self.max_validation_retries = max_validation_retries
        self.final_directive = final_directive
        self.calls = 0

    @abstractmethod
    def _raw(
        self,
        system: str,
        user: str,
        schema: type[BaseModel],
        max_tokens: int,
        native_schema: bool,
    ) -> tuple[str, Usage]:
        """Retourne (texte brut, usage) pour un tour de conversation."""

    def _supports_native_schema(self) -> bool:
        return False

    def complete(
        self,
        system: str,
        user: str,
        schema: type[BaseModel],
        max_tokens: int = 4096,
    ) -> tuple[BaseModel, Usage]:
        total = Usage()
        prompt = user
        last_error = "?"

        for attempt in range(1, self.max_validation_retries + 1):
            # relu a chaque tour : un serveur qui refuse json_schema fait
            # basculer le provider en mode prompt des le premier appel.
            native = self._supports_native_schema()
            sys_prompt = system if native else f"{system}\n\n{self._json_instructions(schema)}"
            text, usage = self._raw(
                sys_prompt,
                prompt + (FINAL_DIRECTIVE if self.final_directive else ""),
                schema,
                max_tokens,
                native,
            )
            total = total + usage
            self.calls += 1

            if not (text or "").strip():
                # Cas propre a un modele a raisonnement : il a "pense" sans rien
                # emettre. Le dire explicitement vaut mieux qu'un "JSON absent".
                last_error = (
                    "- ta reponse etait VIDE : tu n'as produit que du raisonnement "
                    "interne, aucun contenu. Le raisonnement n'est PAS une reponse. "
                    "Emets directement l'objet JSON, et sois bref."
                )
            else:
                try:
                    payload = json.loads(extract_json(text))
                    return schema.model_validate(payload), total
                except ValidationError as exc:
                    last_error = _flatten_errors(exc)
                except (ValueError, json.JSONDecodeError) as exc:
                    last_error = f"- la reponse n'etait pas du JSON exploitable : {exc}"

            # --- retry a feedback : le modele recoit son erreur, pas un reset
            prompt = (
                f"{user}\n\n"
                f"--- CORRECTION (tentative {attempt + 1}/{self.max_validation_retries}) ---\n"
                f"Ta reponse precedente a ete REJETEE :\n{last_error}\n\n"
                f"Reponds a nouveau, uniquement avec un objet JSON valide "
                f"conforme au schema. Aucun texte autour, aucun commentaire."
            )

        raise ProviderError(
            f"{schema.__name__}: {self.max_validation_retries} tentatives echouees. "
            f"Derniere erreur:\n{last_error}",
            usage=total,
        )

    @staticmethod
    def _json_instructions(schema: type[BaseModel]) -> str:
        return (
            "Tu reponds EXCLUSIVEMENT par un objet JSON valide, sans texte "
            "autour, sans bloc de code, conforme a ce JSON Schema :\n"
            f"{schema_hint(schema)}"
        )


# --------------------------------------------------------------------------


class OpenAICompatibleProvider(BaseProvider):
    """Client `/v1/chat/completions`.

    Sortie structuree : `response_format: json_schema` si le serveur
    l'accepte, sinon bascule definitive (pour la duree du process) sur
    l'injection du schema dans le prompt.
    """

    def __init__(self, cfg: LLMConfig, client: httpx.Client | None = None) -> None:
        super().__init__(cfg.max_validation_retries, cfg.final_directive)
        self.cfg = cfg
        self.model = cfg.model
        self._client = client or httpx.Client(
            base_url=cfg.base_url.rstrip("/"),
            timeout=cfg.timeout_s,
            headers={"Authorization": f"Bearer {cfg.api_key}"},
        )
        self._native = cfg.structured_output in ("auto", "json_schema")

    def _supports_native_schema(self) -> bool:
        return self._native

    def _raw(
        self,
        system: str,
        user: str,
        schema: type[BaseModel],
        max_tokens: int,
        native_schema: bool,
    ) -> tuple[str, Usage]:
        body: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "max_tokens": max_tokens,
            "temperature": self.cfg.temperature,
        }
        if native_schema:
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": schema.__name__,
                    "strict": True,
                    "schema": schema.model_json_schema(),
                },
            }
        if not self.cfg.enable_thinking:
            # Desactive le raisonnement d'un modele qui pense par defaut
            # (ex. "chat" sur le service central) : sans ca, il consomme son
            # budget de tokens en reflexion avant d'emettre le JSON attendu.
            body["chat_template_kwargs"] = {"enable_thinking": False}

        try:
            resp = self._client.post("/chat/completions", json=body)
        except httpx.HTTPError as exc:  # reseau, timeout
            raise ProviderError(f"appel LLM impossible ({self._client.base_url}): {exc}") from exc

        if resp.status_code >= 400:
            detail = resp.text[:400]
            if native_schema and self.cfg.structured_output == "auto":
                # Le serveur ne gere pas json_schema : on bascule et on relance
                # ce meme tour en mode prompt.
                self._native = False
                return self._raw(
                    f"{system}\n\n{self._json_instructions(schema)}",
                    user,
                    schema,
                    max_tokens,
                    native_schema=False,
                )
            raise ProviderError(f"HTTP {resp.status_code} du serveur LLM: {detail}")

        data = resp.json()
        message = (data.get("choices") or [{}])[0].get("message", {}) or {}
        content = message.get("content") or ""
        # Le service central rend le raisonnement dans `reasoning_content`
        # (certains backends plus anciens utilisaient `reasoning`) : on
        # accepte les deux noms de champ.
        reasoning = message.get("reasoning_content") or message.get("reasoning") or ""
        finish = (data.get("choices") or [{}])[0].get("finish_reason")

        raw_usage = data.get("usage") or {}
        completion = int(raw_usage.get("completion_tokens", 0))
        if self.cfg.count_reasoning_tokens and reasoning:
            # Certains backends (ex. l'ancien Ollama/gemma4:e4b) n'incluent pas
            # le raisonnement dans ce compteur : on le re-estime alors.
            completion += len(reasoning) // 4
        usage = Usage(
            prompt_tokens=int(raw_usage.get("prompt_tokens", 0)),
            completion_tokens=completion,
        )

        if not content.strip() and finish == "length":
            # Budget epuise dans le raisonnement. On rend une chaine vide : la
            # branche "reponse vide" de `complete` produit le bon feedback.
            content = ""
        return content, usage


# --------------------------------------------------------------------------


ScriptItem = str | dict[str, Any] | BaseException
Handler = Callable[[str, str, type[BaseModel], int], ScriptItem]


class FakeProvider(BaseProvider):
    """Provider scripte : permet de tester la boucle entiere sans LLM.

    - `script`   : items consommes dans l'ordre (str brut, dict, ou exception).
      Un `str` mal forme declenche exactement le meme retry a feedback qu'un
      vrai modele qui derape.
    - `handler`  : callable (system, user, schema, n_appel) -> item, pour les
      scenarios ou la reponse depend du schema demande.
    """

    def __init__(
        self,
        script: Sequence[ScriptItem] | None = None,
        handler: Handler | None = None,
        max_validation_retries: int = 3,
        tokens_per_call: int = 100,
        final_directive: bool = True,
    ) -> None:
        super().__init__(max_validation_retries, final_directive)
        self.script: list[ScriptItem] = list(script or [])
        self.handler = handler
        self.tokens_per_call = tokens_per_call
        self.prompts: list[tuple[str, str, str]] = []  # (schema, system, user)

    def feed(self, items: Iterable[ScriptItem]) -> None:
        self.script.extend(items)

    def _raw(
        self,
        system: str,
        user: str,
        schema: type[BaseModel],
        max_tokens: int,
        native_schema: bool,
    ) -> tuple[str, Usage]:
        self.prompts.append((schema.__name__, system, user))
        if self.handler is not None:
            item = self.handler(system, user, schema, self.calls)
        elif self.script:
            item = self.script.pop(0)
        else:
            raise ProviderError(
                f"FakeProvider a court de script (schema demande: {schema.__name__})"
            )
        if isinstance(item, BaseException):
            raise item
        text = item if isinstance(item, str) else json.dumps(item, ensure_ascii=False)
        return text, Usage(prompt_tokens=len(system + user) // 4, completion_tokens=self.tokens_per_call)


# --------------------------------------------------------------------------


def build_provider(cfg: LLMConfig) -> BaseProvider:
    return OpenAICompatibleProvider(cfg)
