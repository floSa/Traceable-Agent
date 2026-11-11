"""Etape 2 — couche modele : extraction, validation, retry a feedback."""

from __future__ import annotations

import json

import httpx
import pytest
from pydantic import BaseModel

from engine.config import LLMConfig
from engine.providers import (
    FakeProvider,
    OpenAICompatibleProvider,
    ProviderError,
    extract_json,
)


class Answer(BaseModel):
    insight: str
    score: int


# ---- extraction ----------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        '{"insight": "a", "score": 1}',
        'Voici ma reponse :\n{"insight": "a", "score": 1}\nVoila.',
        '```json\n{"insight": "a", "score": 1}\n```',
        '{"insight": "accolade } dans une chaine", "score": 1}',
    ],
)
def test_extract_json_tolere_la_prose(raw: str) -> None:
    assert json.loads(extract_json(raw))["score"] == 1


@pytest.mark.parametrize("raw", ["", "pas de json ici", '{"a": 1'])
def test_extract_json_refuse_l_absent(raw: str) -> None:
    with pytest.raises(ValueError):
        extract_json(raw)


# ---- retry a feedback ----------------------------------------------------


def test_json_invalide_deux_fois_puis_valide() -> None:
    p = FakeProvider(
        script=[
            "je ne sais pas repondre",  # pas de JSON
            '{"insight": "a"}',  # champ manquant
            {"insight": "a", "score": 3},  # bon
        ]
    )
    out, usage = p.complete("sys", "fais X", Answer)
    assert out.score == 3
    assert p.calls == 3
    assert usage.total > 0


def test_le_retry_renvoie_l_erreur_pydantic_au_modele() -> None:
    p = FakeProvider(script=['{"insight": "a"}', {"insight": "a", "score": 1}])
    p.complete("sys", "fais X", Answer)
    _, _, second_prompt = p.prompts[1]
    assert "REJETEE" in second_prompt
    assert "score" in second_prompt  # le champ fautif est nomme
    assert "fais X" in second_prompt  # la demande initiale est conservee


def test_echec_definitif_apres_n_tentatives() -> None:
    p = FakeProvider(script=["nope"] * 3, max_validation_retries=3)
    with pytest.raises(ProviderError, match="3 tentatives"):
        p.complete("sys", "u", Answer)
    assert p.calls == 3


def test_le_schema_est_injecte_quand_le_provider_n_est_pas_natif() -> None:
    p = FakeProvider(script=[{"insight": "a", "score": 1}])
    p.complete("sys", "u", Answer)
    _, system, _ = p.prompts[0]
    assert "JSON Schema" in system and "insight" in system


# ---- client OpenAI-compatible -------------------------------------------


def _client(handler) -> httpx.Client:
    return httpx.Client(base_url="http://test/v1", transport=httpx.MockTransport(handler))


def _reply(content: str, *, reasoning: str = "", finish: str = "stop", usage=None) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "choices": [
                {"message": {"content": content, "reasoning": reasoning}, "finish_reason": finish}
            ],
            "usage": usage or {"prompt_tokens": 10, "completion_tokens": 20},
        },
    )


def test_json_schema_natif_est_envoye() -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        return _reply('{"insight": "ok", "score": 2}')

    p = OpenAICompatibleProvider(LLMConfig(model="gemma4:e4b"), client=_client(handler))
    out, _ = p.complete("sys", "u", Answer)
    assert out.score == 2
    assert seen["response_format"]["json_schema"]["name"] == "Answer"
    assert seen["model"] == "gemma4:e4b"


def test_fallback_prompt_si_le_serveur_refuse_json_schema() -> None:
    bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        bodies.append(body)
        if "response_format" in body:
            return httpx.Response(400, text="response_format not supported")
        return _reply('{"insight": "ok", "score": 5}')

    p = OpenAICompatibleProvider(LLMConfig(structured_output="auto"), client=_client(handler))
    out, _ = p.complete("sys", "u", Answer)
    assert out.score == 5
    assert "response_format" in bodies[0] and "response_format" not in bodies[1]
    assert "JSON Schema" in bodies[1]["messages"][0]["content"]
    # la bascule est definitive : l'appel suivant part directement en mode prompt
    p.complete("sys", "u", Answer)
    assert "response_format" not in bodies[2]


def test_erreur_http_sans_fallback_remonte() -> None:
    p = OpenAICompatibleProvider(
        LLMConfig(structured_output="prompt"),
        client=_client(lambda r: httpx.Response(500, text="boom")),
    )
    with pytest.raises(ProviderError, match="HTTP 500"):
        p.complete("sys", "u", Answer)


def test_les_tokens_de_raisonnement_sont_comptes() -> None:
    # count_reasoning_tokens=False par defaut depuis le service central (son
    # usage.completion_tokens inclut deja le raisonnement) : ce test verifie
    # le mecanisme d'estimation, encore utile pour un backend qui l'exclut.
    p = OpenAICompatibleProvider(
        LLMConfig(count_reasoning_tokens=True),
        client=_client(lambda r: _reply('{"insight":"a","score":1}', reasoning="x" * 400)),
    )
    _, usage = p.complete("sys", "u", Answer)
    assert usage.completion_tokens == 20 + 100  # 20 rapportes + 400/4 estimes


def test_reponse_vide_par_epuisement_du_budget_declenche_le_retry() -> None:
    replies = [
        _reply("", reasoning="je reflechis..." * 50, finish="length"),
        _reply('{"insight": "court", "score": 1}'),
    ]
    p = OpenAICompatibleProvider(LLMConfig(), client=_client(lambda r: replies.pop(0)))
    out, _ = p.complete("sys", "u", Answer, max_tokens=50)
    assert out.insight == "court"


def test_reseau_injoignable_donne_une_erreur_lisible() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    p = OpenAICompatibleProvider(LLMConfig(), client=_client(handler))
    with pytest.raises(ProviderError, match="appel LLM impossible"):
        p.complete("sys", "u", Answer)


# ---- comportements de modele a raisonnement, mesures en reel -------------


def test_une_directive_finale_est_ajoutee_au_prompt() -> None:
    """gemma4:e4b termine son raisonnement et renvoie un contenu VIDE sans elle
    (mesure : 0 caractere de contenu, finish_reason=stop, 746 de raisonnement)."""
    p = FakeProvider(script=[{"insight": "a", "score": 1}])
    p.complete("sys", "fais X", Answer)
    _, _, user = p.prompts[0]
    assert user.startswith("fais X")
    assert user.rstrip().endswith("Ta reponse commence par { et finit par }.")


def test_la_directive_finale_est_desactivable() -> None:
    p = FakeProvider(script=[{"insight": "a", "score": 1}], final_directive=False)
    p.complete("sys", "fais X", Answer)
    assert p.prompts[0][2] == "fais X"


def test_une_reponse_vide_est_nommee_telle_quelle_dans_le_retry() -> None:
    p = FakeProvider(script=["", {"insight": "a", "score": 1}])
    out, _ = p.complete("sys", "u", Answer)
    assert out.score == 1
    _, _, second = p.prompts[1]
    assert "ta reponse etait VIDE" in second
    assert "raisonnement n'est PAS une reponse" in second


def test_contenu_vide_par_troncature_donne_le_meme_feedback() -> None:
    replies = [
        _reply("", reasoning="je reflechis..." * 50, finish="length"),
        _reply('{"insight": "court", "score": 1}'),
    ]
    p = OpenAICompatibleProvider(LLMConfig(), client=_client(lambda r: replies.pop(0)))
    out, _ = p.complete("sys", "u", Answer, max_tokens=50)
    assert out.insight == "court"
