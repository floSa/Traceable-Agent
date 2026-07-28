# investigator — moteur d'investigation par tâches atomiques

Vous posez une question complexe. Le système la découpe en tâches simples,
écrites dans un fichier markdown. Puis une boucle les exécute **une par une** :
pour chaque tâche, un appel LLM avec un **contexte vierge**, qui ne reçoit que
cette tâche, rend son résultat, et disparaît.

**Le fichier markdown est la seule mémoire du système.** Aucun état ne survit
dans le contexte d'un LLM entre deux tâches. Conséquence directe : le moteur est
redémarrable par construction — on peut le tuer en plein vol, `inv run` reprend
à la première tâche `pending`.

Deux points de contrôle humain sont obligatoires et non contournables :
la liste de tâches avant exécution, les résultats bruts avant synthèse.

```
inv new ──▶ GATE 1 ──▶ inv run ──▶ GATE 2 ──▶ inv synthesize ──▶ inv report ──▶ inv done
  LLM      humain      boucle      humain          LLM             Jinja2       humain
                    (0 appel LLM)                                (0 appel LLM)
```

---

## Installation

Python 3.12, `uv`. Aucune base de données requise pour la démo.

```bash
uv sync
cp .env.example .env
```

### Le modèle : service LLM central, pas d'Ollama local

Ce projet **ne lance pas son propre Ollama**. Il tape le service mutualisé
`~/mes_projets/llm-service` (conteneur `ollama-central`), qui expose un endpoint
OpenAI-compatible. C'est le cas « app `uv` locale » de
`llm-service/docs/brancher-un-projet.md`.

```bash
cd ~/mes_projets/llm-service && make up
```

```bash
cd ~/mes_projets/llm-service && make models
```

Puis dans le `.env` de ce projet :

```bash
INV_LLM_BASE_URL=http://localhost:11434/v1
INV_LLM_MODEL=gemma4:e4b
```

Si ce projet est un jour conteneurisé et attaché au réseau `llm-net`, seule l'URL
change : `http://ollama-central:11434/v1`.

---

## Exemple de bout en bout (pack `files`, zéro base de données)

Le dépôt contient un corpus de démonstration dans `corpus/` : six notes markdown
sur une hausse de churn (comité produit, post-mortem de migration, entretiens de
sortie, support, veille concurrentielle, tickets critiques).

```bash
uv run inv new "Pourquoi le churn des clients Entreprise a-t-il augmente au premier semestre 2026 ?" --yes
```

Le rôle `brief_builder` produit une liste de tâches — découpée **par hypothèse**,
chacune avec son périmètre de fichiers. Le plan est en statut `ready` : rien ne
s'exécute encore.

**GATE 1** — vous relisez, et vous éditez si besoin :

```bash
uv run inv tasks <slug> --edit
```

`--edit` ouvre `plan.md` dans `$EDITOR`. Le frontmatter YAML est la source de
vérité : vous pouvez y supprimer une tâche hors sujet, resserrer un périmètre,
réordonner. À la fermeture, le fichier est **revalidé intégralement** — une
édition incohérente (dépendance vers une tâche inexistante, id dupliqué) est
refusée immédiatement, pas trois tâches plus tard.

```bash
uv run inv run <slug>
```

```bash
uv run inv status <slug>
```

**GATE 2** — les résultats bruts, avant toute synthèse :

```bash
uv run inv review <slug>
```

```bash
uv run inv approve <slug> --note "pourquoi vous validez"
```

Sans `approve`, `synthesize` refuse de tourner. C'est une vérification de code,
pas une convention.

```bash
uv run inv synthesize <slug>
```

```bash
uv run inv report <slug>
```

```bash
uv run inv done <slug>
```

### Ce que ça donne réellement

Extrait d'un run réel sur ce corpus avec `gemma4:e4b`, chiffres non maquillés :

```
▶ t4 Analyser les tickets critiques pour identifier les défaillances systémiques
  done · 2 outil(s) · 4585 tokens · 28.3s
```

La finding correspondante cite de vrais numéros de tickets du corpus. Et la
synthèse liste explicitement ce qui **n'a pas** été établi :

> **Non établi**
> · L'impact concurrentiel n'a pu être évalué faute d'informations (t3).
> · Les tentatives d'identifier les problèmes récurrents (t1) ont échoué.

Une tâche bloquée reste visible partout — dans le plan, dans le rapport, dans
les *caveats* de la synthèse. Le système ne masque jamais ce qu'il n'a pas su
faire.

---

## Ce qui est générique, et ce qui ne l'est pas

Le moteur ne connaît **aucun domaine métier**. La seule chose qui varie d'un
usage à l'autre est le `ToolPack` :

```python
class ToolPack(Protocol):
    name: str
    def describe(self) -> str                     # injecté au prompt
    def tools(self) -> list[ToolSpec]             # nom, schéma, doc
    def call(self, name, args) -> ToolResult
    def validate(self, name, args) -> Ok | Refus  # AVANT toute exécution
    def artifact(self, name, args, res) -> str | None   # trace auditable
```

`validate` est appelé avant chaque exécution et peut refuser. `artifact` rend le
contenu écrit sur disque en trace auditable — le SQL exact, la commande shell
équivalente. **Ces traces ne sont jamais relues par un LLM** : elles servent à un
humain qui veut rejouer ou contester un résultat.

Deux packs sont livrés :

| pack | outils | périmètre (`task.scope`) | dépendances |
|---|---|---|---|
| `files` | `grep`, `read`, `list` | `{"globs": [...]}` | aucune |
| `sql` | `schema`, `query` | `{"tables": [...]}` | `duckdb` |

`SqlToolPack.validate` refuse tout ce qui n'est pas `SELECT`/`WITH`, refuse les
tables non déclarées dans la tâche, refuse les fonctions qui liraient des
fichiers hors base (`read_csv`…), passe un `EXPLAIN` avant d'exécuter, et injecte
un `LIMIT`.

Le pack `sql` se branche **sans qu'une ligne du moteur ne change**, et lancer le
moteur sur `files` ne charge pas une ligne de code SQL :

```python
def test_le_moteur_tourne_sur_files_sans_importer_duckdb():
    # dans un process neuf, après une investigation complète sur `files` :
    assert [m for m in sys.modules if "duckdb" in m] == []
```

Pour utiliser le pack sql :

```bash
uv sync --extra sql
```

---

## Les six trous, rendus explicites

Le pattern d'origine ne documente pas ces points. Chacun est un **paramètre
visible dans `config.yml`**, jamais une constante enfouie.

### 1. Contexte passé au sous-agent

`engine/context.py` — `build_context(plan, task) -> str` est une **fonction
pure** : ni disque, ni réseau, ni horloge. Elle assemble, et rien d'autre :

1. la question globale, réduite à une phrase ;
2. les `watch_outs` ;
3. pour chaque tâche de `depends_on`, **uniquement son `insight`**.

Jamais le logbook complet, jamais les `summary`, jamais les artefacts, jamais les
autres tâches. C'est vérifié par un test qui plante un `summary` marqué
CONFIDENTIEL dans le logbook et exige qu'il ne fuite pas.

Réglages : `context.max_chars`, `context.include_dependencies`,
`context.include_watch_outs`, `context.include_question`,
`context.max_dependencies`.

### 2. Évaluation du critère d'arrêt

`stop.strategy` :

- `none` — jamais évalué (défaut du POC) ;
- `llm` — un appel dédié minimal : le critère + les insights accumulés, sortie
  `{fired, reason}`. Il vit dans `agent.py`, pas dans `loop.py`, pour que
  l'orchestrateur reste sans le moindre appel de modèle ;
- `callable` — votre fonction Python, `"mon_module:ma_fonction"`.

**L'évaluation est journalisée systématiquement** dans `logbook.stop_log`, y
compris en `none` : le plan porte alors la trace explicite que le critère n'a
jamais été évalué. Un critère `callable` qui lève une exception ne casse pas
l'enquête — il est journalisé comme non concluant.

### 3. Écriture dans le fichier

Le LLM n'écrit **jamais** de markdown. Il rend du JSON validé par Pydantic.
`plan.commit(result)` relit le fichier, parse, modifie l'objet, **revalide le
schéma entier**, réécrit.

L'écriture est atomique (fichier temporaire + `os.replace` + `fsync` du
répertoire) et prise sous verrou `flock`. `commit` écrit le finding, l'état de la
tâche et la ligne de run_log **dans la même transaction** — sinon un kill entre
les deux laisse une tâche `done` sans tokens comptés. C'est un vrai bug, attrapé
par le test de crash.

Format retenu : le **frontmatter YAML est la source de vérité**, le corps
markdown est une vue régénérée à chaque écriture (Jinja2) et jamais parsée. Le
round-trip est donc sans perte, et un humain peut éditer le YAML à la main.

### 4. Budget d'itération du sous-agent

`agent.max_tool_calls`, `agent.max_tokens`, `agent.max_tokens_per_call`.
Dépassement → tâche marquée `blocked` avec sa raison. **Jamais de crash** : un
provider injoignable, un modèle qui ne produit pas de JSON exploitable, un budget
épuisé — tout se solde par un `blocked` documenté.

Les tokens brûlés par des tentatives ratées sont **quand même imputés** au
budget : un modèle qui dérape ne dépense pas en douce.

> Le défaut du moteur est 5 appels d'outils (`engine/config.py`). Le `config.yml`
> livré monte à **10**, mesuré nécessaire avec `gemma4:e4b` sur des tâches à
> périmètre multi-fichiers. Avec un modèle plus synthétique, redescendez : c'est
> un coût direct en tokens.

### 5. Artefacts visuels — hors périmètre

Le champ `logbook.findings[].chart` existe dans le schéma et **reste `null`**.
Un test l'affirme. Le template HTML l'affiche déjà s'il est rempli.

Pour l'implémenter plus tard sans toucher au moteur : faire retourner à
`ToolPack.artifact()` un chemin d'image en plus de la trace texte, et le poser
dans `chart` au moment du commit. Le point dur n'est pas technique, c'est de
décider **qui** produit le graphique — le sous-agent, qui devrait alors écrire du
code, ou le pack, qui devrait alors connaître la sémantique des données. Tant que
ce n'est pas tranché, le champ reste `null`.

### 6. Reprise après échec

`plan.md` étant la seule source de vérité et étant relu au début de **chaque**
tour de boucle, le moteur est redémarrable par construction. Relancer `inv run`
reprend à la première tâche `pending`. Une tâche `blocked` est ignorée par
défaut, rejouable avec `--retry-blocked` — et une tâche n'est tentée qu'une fois
par run, sans quoi une tâche qui rebloque se ferait rejouer en boucle.

C'est prouvé, pas affirmé : `tests/test_crash_recovery.py` lance la boucle dans
un vrai sous-process, le tue au `SIGKILL` en plein vol, vérifie que `plan.md` est
toujours valide et cohérent, relance, et exige qu'aucune tâche ne soit rejouée.
Un second test tue le process pendant une rafale de centaines de réécritures pour
éprouver l'atomicité.

---

## Les rôles

| # | rôle | LLM ? | ce qu'il fait |
|---|---|---|---|
| 1 | `brief_builder` | oui | conversationnel, **une question à la fois**, produit la liste de tâches en JSON → statut `ready` |
| 2 | `loop` | **non** | relit le plan, prend la première tâche exécutable, appelle l'agent, commit, vérifie les arrêts → `awaiting_review` |
| 3 | `agent` | oui | une tâche, un contexte vierge, boucle d'outils bornée, rend un `TaskResult` |
| 4 | `synthesize` | oui, une fois | relit le logbook du disque, snapshot daté **immuable** dans `results/` |
| 5 | `reporter` | **non** | Jinja2 → HTML |

Le system prompt du sous-agent est littéral :
*« You execute exactly one task. You do not summarize. You do not comment on
other tasks. »*

Que la boucle ne contienne aucun appel de modèle n'est pas une intention, c'est
un test : il parcourt l'AST de `loop.py` et refuse tout appel `.complete(...)`,
tout import de client HTTP, toute constante de prompt. Un autre test exige
qu'elle tienne sous 200 lignes.

Le passage à `done` exige une commande humaine explicite (`inv done`). Ni la
boucle, ni la synthèse, ni le rapport n'y touchent — un test le vérifie en
enchaînant tout le parcours et en exigeant que le statut reste `awaiting_review`.

---

## Couche modèle

`engine/providers.py`. Contrat unique :

```python
provider.complete(system, user, schema, max_tokens) -> (instance_validée, Usage)
```

Sortie structurée : `response_format: json_schema` si le serveur l'accepte (c'est
le cas du central), sinon bascule automatique sur le JSON Schema injecté dans le
prompt.

**Le retry à feedback est le cœur du dispositif.** Sur erreur de validation, on
renvoie au modèle son erreur Pydantic exacte, pas un reset. Il est implémenté une
seule fois, dans `BaseProvider`, donc `FakeProvider` l'exerce à l'identique :
tester la boucle sans LLM teste aussi le mécanisme qui rend un petit modèle
exploitable.

`FakeProvider` rend du JSON scripté — toute la suite de tests tourne sans modèle.

### Ce que `gemma4:e4b` a imposé (mesuré, pas supposé)

Trois comportements constatés en run réel ont changé le code. Ils sont documentés
parce qu'ils se reproduiront avec tout modèle à raisonnement :

1. **Les tokens de raisonnement ne sont pas comptés** dans
   `usage.completion_tokens`. Sans correction, le budget mentait d'un facteur ~4.
   → `llm.count_reasoning_tokens` les ré-estime.

2. **Le modèle « pense » et renvoie un contenu vide** (`finish_reason: stop`,
   746 caractères de raisonnement, 0 de contenu). Mesuré : une dernière ligne
   impérative dans le prompt le ramène à émettre le JSON.
   → `llm.final_directive`. Sans effet sur un modèle qui n'en a pas besoin.

3. **Un `dict` libre dans un schéma ne se remplit pas.** Le champ `scope` d'une
   tâche était un objet libre : `{"type": "object"}` dans le JSON Schema ne dit
   rien de ce qu'il attend, et le modèle rendait `{}` sur 9 tâches sur 9, même
   après trois retries à feedback. La correction n'est pas un meilleur prompt :
   **c'est le pack qui déclare un modèle typé** (`ToolPack.scope_model()`), et
   l'information est alors portée par la génération elle-même.

Corollaire de conception, valable partout : tout ce qu'un prompt seul n'obtient
pas d'un petit modèle doit passer par le **schéma**, où le retry à feedback peut
le corriger. Le nombre minimum de tâches, le format du périmètre, et le fait
qu'un champ du brief n'est pas une tâche sont trois règles portées par le schéma
pour cette raison — chacune ajoutée après un échec observé.

---

## Arborescence

```
engine/
  schemas.py      tout l'état persisté + les sorties structurées des LLM
  plan.py         lecture/écriture de plan.md — atomique, verrouillée, revalidée
  providers.py    couche modèle + retry à feedback + FakeProvider
  config.py       config.yml typée
  context.py      build_context — la fonction critique, pure
  brief.py        rôle 1
  loop.py         rôle 2 — l'orchestrateur, ZÉRO appel LLM
  agent.py        rôle 3 — un sous-agent = une tâche
  synthesize.py   rôle 4
  reporter.py     rôle 5 — Jinja2
  cli.py          les deux gates humaines
  toolpacks/      base.py  files.py  sql.py
  templates/      plan.md.j2  report.html.j2
investigations/
  README.md       le registre
  <YYYY-MM-DD_slug>/
    plan.md       la seule mémoire
    artifacts/    traces write-only, jamais relues par un LLM
    results/      snapshots datés immuables
corpus/           le corpus de démonstration
config.yml        les six trous, visibles
tests/
```

> `brief.py` et `cli.py` ne figuraient pas dans l'arborescence demandée : le rôle
> 1 et la CLI ont besoin d'un fichier. Rien d'autre n'a été ajouté.

## Tests

```bash
uv run pytest
```

171 tests, sans aucun LLM. Ceux exigés par le cahier des charges :

| exigence | test |
|---|---|
| 100 commits successifs → plan valide | `test_cent_commits_successifs_gardent_le_plan_valide` |
| JSON invalide 2× puis valide → la tâche aboutit | `test_json_invalide_deux_fois_puis_valide` |
| kill du process → relance au bon endroit | `test_kill_en_pleine_boucle_puis_relance_reprend_au_bon_endroit` |
| 2 blocked consécutifs → circuit breaker | `test_deux_blocked_consecutifs_ouvrent_le_circuit` |
| `validate` refuse → l'outil n'est pas exécuté | `test_un_refus_de_validate_n_execute_pas_l_outil` |
| `build_context` sans le logbook complet | `test_build_context_ne_contient_jamais_le_logbook_complet` |
| `files` tourne sans importer duckdb | `test_le_moteur_tourne_sur_files_sans_importer_duckdb` |

## Limites connues

- **`gemma4:e4b` bloque environ une tâche sur trois** sur ce corpus. Le moteur
  dégrade proprement — tâche `blocked`, raison inscrite, circuit breaker, reprise
  possible avec `--retry-blocked` — mais une enquête complète du premier coup
  demande un modèle plus gros. C'est une limite du modèle, pas du moteur : la
  même boucle avec `FakeProvider` est déterministe et verte.
- Une tâche `blocked` bloque définitivement ses dépendantes dans le run courant.
  C'est voulu : mieux vaut trois tâches non exécutées qu'une chaîne de
  raisonnement bâtie sur un maillon manquant. `inv status` les montre en
  `pending`.
- Le compteur de `blocked` consécutifs est propre au run courant, pas cumulé
  entre deux relances.
- `artifacts/` n'est jamais purgé.
