# Cadrage — traceable-agent

Le **POURQUOI**. Pour le **COMMENT** (composants, flux, persistance, décisions
techniques), voir [ARCHITECTURE.md](ARCHITECTURE.md).

---

## 1. Pitch

Un moteur d'investigation qui répond à une question complexe en la décomposant en tâches
atomiques, exécutées une par une, chacune dans un contexte LLM vierge, avec un fichier
markdown pour seule mémoire.

1. **Décomposer** — une question complexe devient une liste ordonnée de tâches simples,
   relue et amendable par un humain avant toute exécution.
2. **Exécuter sans mémoire partagée** — chaque tâche est traitée par un appel LLM qui ne
   reçoit qu'elle-même et un extrait minimal du plan. Aucun état ne persiste entre deux
   tâches.
3. **Rendre compte** — chaque appel d'outil laisse une trace auditable sur disque, chaque
   affirmation de la synthèse remonte à une tâche identifiée, et ce qui n'a pas été
   établi est dit explicitement.

**Cas d'usage type** : « Pourquoi le churn des clients Entreprise a-t-il augmenté au
premier semestre ? » sur un dossier de notes internes. Le système découpe par hypothèse
(produit, support, concurrence, tarifs), fouille chaque piste séparément, et rend une
synthèse qui distingue ce qui est établi de ce qui ne l'est pas.

---

## 2. Objectifs & périmètre

**Dans le périmètre (V1)**

- Un moteur **générique** : il ne connaît aucun domaine métier. La seule chose qui varie
  d'un usage à l'autre est l'ensemble d'outils exposé aux sous-agents.
- Deux `ToolPack` de démonstration pour prouver cette généricité : `files` (dossier de
  fichiers texte, sans dépendance) et `sql` (base duckdb).
- Six points laissés implicites par le pattern d'origine, rendus **explicites et
  configurables** — voir la section 6.
- Deux points de contrôle humain **obligatoires** : validation de la liste de tâches
  avant exécution, validation des résultats bruts avant synthèse.
- Redémarrabilité prouvée : le process peut être tué en pleine boucle et relancé.
- Une suite de tests qui tourne **sans aucun LLM**.
- Une **vérification contraignante** des résultats : un contrôle est du code qui lit le
  résultat structuré d'un outil, et une tâche qui échoue à son contrôle ne passe pas
  `done`. Voir [ARCHITECTURE.md §7 bis](ARCHITECTURE.md).
- Un **banc de mesure falsifiable** sur un jeu de 13 questions dont les réponses ont été
  vérifiées hors de tout agent. Voir [EVALUATION.md](EVALUATION.md).

**Hors périmètre (V1)**

- **Les artefacts visuels.** Le champ `logbook.findings[].chart` existe dans le schéma et
  reste `null`. Le template HTML l'affiche déjà s'il est rempli. Ce n'est pas un oubli :
  voir la décision correspondante en section 6.
- **Tout framework d'agents.** La boucle est écrite à la main, contrainte à rester
  lisible d'un trait (185 lignes).
- **L'exécution parallèle de tâches.** Une tâche à la fois, séquentiellement.
- **Une interface web.** La sortie est une CLI, un fichier markdown et un rapport HTML
  statique.
- **L'hébergement d'un modèle.** Le projet consomme un service LLM externe, il n'en
  fournit aucun.

---

## 3. Contraintes (fermes)

| Contrainte | Détail |
|---|---|
| Langage | Python 3.12, géré par `uv` (jamais `venv` + `pip`) |
| Modèle | Le service central [`llm-service`](../../llm-service/) — **aucun Ollama propre au projet** |
| Dépendances | pydantic v2, httpx, python-frontmatter, ruamel.yaml, typer, rich, jinja2 ; duckdb uniquement pour le pack `sql` |
| Framework d'agents | Aucun. La boucle doit tenir sous 200 lignes lisibles |
| Matériel | GPU 16 Go partagé — d'où un modèle « E4B » et un budget de tokens explicite |
| Testabilité | Toute la boucle doit être testable sans appeler un LLM |

---

## 4. Hypothèses

Les hypothèses qui ne se lisent pas dans le code, avec ce qui les remettrait en cause.

- **Un contexte vierge produit de meilleurs résultats qu'un contexte cumulé.**
  L'hypothèse de fond du système : un sous-agent qui ne voit que sa tâche ne peut pas
  dériver sur les précédentes, ni recopier une conclusion antérieure au lieu de la
  vérifier. *Ce qui la remettrait en cause* : des tâches qui échouent faute d'un contexte
  que `depends_on` ne transporte pas — le symptôme serait des insights « Indéterminé »
  systématiques sur les tâches tardives.

- **Un `insight` d'une phrase suffit à transmettre ce qui compte d'une tâche à l'autre.**
  C'est le seul canal entre deux tâches. *Ce qui la remettrait en cause* : des synthèses
  qui perdent des nuances présentes dans les `summary` — auquel cas il faudrait élargir
  `build_context`, ce que la config permet déjà.

- **Un petit modèle est exploitable si on lui renvoie ses erreurs de validation.**
  C'est le pari de toute la couche modèle. *Vérifié en run réel* : c'est ce mécanisme qui
  a corrigé les briefs trop maigres. *Sa limite est aussi mesurée* : il n'a pas suffi à
  faire remplir un champ typé `dict` libre, ce qui a mené à la décision D-6 de
  [ARCHITECTURE.md](ARCHITECTURE.md#9-décisions-darchitecture).

- **Un modèle ne se vérifie pas lui-même.** Ce n'est plus une hypothèse mais un constat
  repris d'une mesure : ajouter au prompt une consigne de relecture d'ordre de grandeur
  a été essayé sur ce jeu de données, et n'a rien changé — même requête naïve, aucune
  auto-correction en deux essais. Toute vérification qui compte doit donc être du code.
  *Ce qui la remettrait en cause* : un modèle nettement plus fort, sur lequel la consigne
  prendrait. À re-tester avant de désactiver quoi que ce soit.

- **Le corpus investigué est de confiance.** Son contenu remonte tel quel au modèle. Une
  injection de prompt via un fichier hostile n'est pas traitée.

- **Le volume reste modeste** : quelques dizaines de tâches par investigation. La
  réécriture complète du plan à chaque commit ne tiendrait pas à l'échelle du millier.

---

## 5. Stack technique

| Brique | Choix | Licence |
|---|---|---|
| Langage | Python 3.12 | PSF-2.0 |
| Paquets | uv | MIT / Apache-2.0 |
| Schémas & validation | pydantic 2.13.4 | MIT |
| Client HTTP | httpx 0.28.1 | BSD-3-Clause |
| Frontmatter | python-frontmatter 1.3.0 | MIT |
| YAML round-trip | ruamel.yaml 0.19.1 | MIT |
| CLI | typer 0.27.0 | MIT |
| Terminal | rich 15.0.0 | MIT |
| Templates | jinja2 3.1.6 | BSD-3-Clause |
| Base analytique (extra) | duckdb 1.5.5 | MIT |
| Tests | pytest 9.1.1 | MIT |
| Serveur LLM | Ollama (service central) | MIT |
| Modèle | `gemma4:e4b` | Gemma Terms of Use (Google) — pas une licence OSI |

---

## 6. Décisions

### Décisions figées

- **Le fichier est la seule mémoire** : `plan.md` plutôt qu'une base ou un état en
  mémoire, **parce que** c'est ce qui rend le système redémarrable, inspectable et
  amendable à la main. *Contrepartie* : tout passe par des lectures/écritures disque.

- **Le LLM n'écrit jamais de markdown** : il rend du JSON validé par Pydantic, et c'est
  le code qui sérialise. **Parce qu'**un format libre écrit par un modèle finit toujours
  par casser le parsing. *Contrepartie* : chaque sortie doit avoir son schéma.

- **L'orchestrateur ne fait aucun appel de modèle**, ce qui le rend entièrement testable
  avec un provider factice. Voir D-3 dans [ARCHITECTURE.md](ARCHITECTURE.md#9-décisions-darchitecture).

- **Les deux gates humaines sont des vérifications de code, pas des conventions** :
  `inv synthesize` lève si `inv approve` n'a pas été passé, et aucune commande ne met le
  statut à `done` en dehors de `inv done`.

- **`sanity_check` ne décide de rien** : il reste une consigne rendue au prompt, parce
  qu'elle est lisible et coûte zéro. Ce sont les `checks` d'une tâche qui bloquent,
  **parce qu'**une consigne est indicative et un contrôle est exécutable.
  *Contrepartie* : il faut écrire les contrôles, et ils ne couvrent que ce qu'on sait
  formuler.

- **Un contrôle a trois issues** : réussi, échoué, **non vérifiable**. Confondre la
  troisième avec l'une des deux autres ment — soit on accuse à tort, soit on prétend une
  couverture qu'on n'a pas. Cette décision vient d'un incident réel où un contrôle a
  bloqué une réponse juste.

- **Les artefacts sont write-only** : la trace d'un appel d'outil (la commande, le SQL)
  part sur disque et n'est jamais relue par un modèle. **Parce que** son rôle est de
  permettre à un humain de rejouer ou de contester, pas d'enrichir un prompt.

- **Le budget d'outils livré est à 10**, alors que le défaut du moteur est 5.
  Mesuré nécessaire avec `gemma4:e4b` sur des tâches à périmètre multi-fichiers.
  *Contrepartie* : c'est un coût direct en tokens ; avec un modèle plus synthétique,
  redescendre.

### À trancher

- **Qui produit les artefacts visuels ?** Le champ `chart` reste `null` parce que la
  question n'est pas technique mais de responsabilité : soit le **sous-agent** génère le
  graphique, et il doit alors écrire et exécuter du code, ce qui ouvre une surface
  d'exécution que le projet évite ; soit le **ToolPack** le génère, et il doit alors
  connaître la sémantique des données, ce qui casse sa neutralité métier.
  *Recommandation par défaut* : un troisième pack, dédié, qui expose un outil
  `chart(spec)` déclaratif — le modèle décrit le graphique, il ne le code pas.

- **Faut-il persister le compteur de blocages consécutifs ?** Aujourd'hui il est propre
  au run. *Recommandation par défaut* : le laisser ainsi tant qu'une relance manuelle
  reste l'usage normal.

- **Faut-il une purge des artefacts ?** `artifacts/` croît sans limite.
  *Recommandation par défaut* : attendre que ça gêne.

---

## 7. Roadmap

L'ordre de construction a été imposé et respecté : chaque étape devait avoir ses tests
verts avant de passer à la suivante.

0. `schemas.py` + `plan.py` + les tests de robustesse d'écriture
1. `providers.py` + `FakeProvider`
2. `toolpacks/base.py` + `files.py`
3. `context.py` + `agent.py`
4. `loop.py`
5. `synthesize.py` + `reporter.py` + `cli.py`
6. `toolpacks/sql.py` **en dernier**, pour prouver qu'il se branche sans toucher au moteur

### Fait depuis (branche `investigation-maxizoo`)

Le pack `sql` a été confronté à un vrai LLM sur une vraie base — ce qui était le point 2
de la suite envisagée. Trois étapes livrées et testées :

| Étape | Contenu | Mesuré |
|---|---|---|
| 0 | Banc de mesure : vérificateur chiffré, lecture des 13 questions, trois compteurs | Ligne de base établie |
| 1 | Périmètre par tâche : dictionnaire injecté avant la question, contrôle de grain | **Gain réel** : `faux` de 9 à 6 |
| 2 | Vérification contraignante : la porte entre exécution et écriture | Non concluant à n=1 |

Détail et réserves : [EVALUATION.md](EVALUATION.md).

### Suite envisagée

| Priorité | Sujet | Pourquoi |
|---|---|---|
| 1 | Brancher les contrôles `columns` / `row_count` | Mesuré : le mode de défaillance dominant est « la grandeur demandée est absente », pas « le chiffre est absurde » |
| 2 | 3 exécutions par configuration, `temperature: 0` | 4 questions sur 13 changent de verdict entre exécutions identiques ; sans ça, aucun chiffre ne conclut |
| 3 | Éprouver le rôle 1 en mode conversationnel sur le vrai modèle | Seul chemin de code non validé en réel (tous les runs ont utilisé `--yes`) |
| 4 | Tester avec un modèle plus gros | Seul levier réel sur le taux de blocage |
| 5 | Trancher les artefacts visuels | Voir section 6 |

---

## 8. Stratégie de tests

**254 tests, aucun appel de modèle.** `FakeProvider` rend du JSON scripté et hérite du
même mécanisme de retry que le vrai provider : tester la boucle sans LLM teste aussi ce
qui rend un petit modèle exploitable.

| Fichier | Tests | Ce qu'il prouve |
|---|---|---|
| [`test_plan.py`](../tests/test_plan.py) | 16 | Round-trip sans perte, 100 commits successifs, refus d'un fichier corrompu, verrou exclusif |
| [`test_providers.py`](../tests/test_providers.py) | 21 | Extraction de JSON bavard, retry à feedback, bascule `json_schema` → prompt, comptage du raisonnement |
| [`test_toolpack_files.py`](../tests/test_toolpack_files.py) | 30 | Refus hors périmètre, traversée de répertoire, symlinks, troncature, traces rejouables |
| [`test_toolpack_sql.py`](../tests/test_toolpack_sql.py) | 27 | Lecture seule, tables déclarées, `EXPLAIN`, `LIMIT` injecté, **et l'absence de duckdb sur le pack `files`** |
| [`test_context.py`](../tests/test_context.py) | 11 | Le logbook complet ne fuite jamais ; la fonction est pure |
| [`test_agent.py`](../tests/test_agent.py) | 13 | Boucle d'outils bornée, refus non exécuté, `blocked` sans crash, anti-boucle |
| [`test_loop.py`](../tests/test_loop.py) | 18 | Circuit breaker, arrêts, reprise, **et l'absence d'appel LLM vérifiée sur l'AST** |
| [`test_synthesize_report.py`](../tests/test_synthesize_report.py) | 18 | Gate humaine, snapshots immuables, garde-fous du rôle 1 |
| [`test_cli.py`](../tests/test_cli.py) | 12 | Parcours complet, gates non contournables |
| [`test_crash_recovery.py`](../tests/test_crash_recovery.py) | 5 | `SIGKILL` sur un vrai sous-process, reprise au bon endroit |
| [`test_checks_values.py`](../tests/test_checks_values.py) | 28 | Le vérificateur chiffré, et surtout ce qu'il doit **rejeter** |
| [`test_reference_questions.py`](../tests/test_reference_questions.py) | 19 | Lecture des 13 questions, refus bruyants, choix des valeurs de contrôle |
| [`test_toolpack_sql_grain.py`](../tests/test_toolpack_sql_grain.py) | 17 | Dictionnaire, grain, agrégation préalable — et le refus d'une requête correcte |
| [`test_q5_grain.py`](../tests/test_q5_grain.py) | 19 | La thèse entière : le chiffre faux n'atteint ni le plan, ni les dépendantes, ni la synthèse |

Trois tests méritent d'être signalés parce qu'ils vérifient une **propriété de
conception**, pas un comportement :

- `test_la_boucle_ne_contient_aucun_appel_llm` parcourt l'AST de `loop.py`.
- `test_le_moteur_tourne_sur_files_sans_importer_duckdb` inspecte `sys.modules` dans un
  process neuf.
- `test_kill_en_pleine_boucle_puis_relance_reprend_au_bon_endroit` tue réellement un
  process au `SIGKILL`, pas au `SIGTERM` : aucun `finally` n'a la moindre chance de
  s'exécuter.

---

## 9. Références

- **Service LLM central** : `~/mes_projets/llm-service` — un Ollama mutualisé pour tout
  le parc, et son guide [`docs/brancher-un-projet.md`](../../llm-service/docs/brancher-un-projet.md).
  Ce projet relève du cas « application `uv` locale », qui tape l'endpoint
  OpenAI-compatible publié sur l'hôte.
- **Modèle** : `gemma4:e4b` — modèle à raisonnement, contexte 128K, function-calling
  natif, distribué sous *Gemma Terms of Use*.
- **Trois comportements de ce modèle**, mesurés en run réel et intégrés au code, sont
  documentés dans le [README](../README.md#ce-que-gemma4e4b-a-imposé-mesuré-pas-supposé).
