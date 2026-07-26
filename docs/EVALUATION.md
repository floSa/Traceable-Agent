# Évaluation — le banc de mesure Maxizoo

Ce document décrit **comment le moteur est mesuré**, et ce que les mesures disent —
y compris quand elles ne disent rien. Pour le **pourquoi** voir [CADRAGE.md](CADRAGE.md),
pour le **comment** du moteur voir [ARCHITECTURE.md](ARCHITECTURE.md).

Le banc vit dans [`harness/`](../harness/), **hors du moteur** : `engine/` ne l'importe
jamais.

---

## 1. Pourquoi ce banc

Un moteur d'investigation qui prétend empêcher les réponses fausses doit être
**falsifiable**. Le projet `data-analyst-agent` fournit exactement ce qu'il faut pour ça :

- une base retail de **1 659 181 lignes** sur 10 tables, avec des pièges de modélisation
  documentés ;
- **13 questions de référence** dont le SQL a été vérifié hors de tout agent ;
- un score publié sur ces questions : **7/13** au contrôle strict ;
- et surtout un constat mesuré : ajouter au prompt « si tu es loin de l'ordre de grandeur
  documenté, ta requête est fausse » **n'a rien changé**. La consigne a été retirée.

Ce dernier point est le fondement de tout le dispositif de vérification : on ne peut pas
demander à un modèle de se vérifier lui-même.

### La limite méthodologique, à énoncer avec chaque chiffre

Ce banc ne mesure **pas** « le mode investigation améliore data-analyst-agent ». Il
compare **deux architectures distinctes** sur le même jeu d'épreuve : l'agent
conversationnel PydanticAI de `data-analyst-agent` d'un côté, le moteur à tâches
atomiques de l'autre. Le modèle, la base, les questions et le vérificateur sont
identiques ; **l'agent SQL ne l'est pas**.

C'est une comparaison honnête entre deux conceptions, pas une mesure d'amélioration d'un
système unique.

---

## 2. Les trois compteurs

Un score unique ment. Le banc en rend trois :

| Compteur | Sens |
|---|---|
| `juste` | la réponse porte les valeurs de référence |
| `bloqué` | la tâche s'est arrêtée — budget, périmètre, ou contrôle en échec |
| **`faux`** | la réponse a l'air d'une réponse, et ses chiffres sont faux |

**`faux` est la métrique à conduire à zéro.** Maximiser `juste` viendrait après, et un
`juste` obtenu par accident ne vaut rien. Une conception qui transforme des réponses
fausses en réponses absentes a fait son travail : « je n'ai pas su » est une réponse
acceptable, « voici un chiffre inventé » ne l'est pas.

Un quatrième nombre est rapporté à part : les contrôles **non vérifiables**, dont la
grandeur contrainte n'était pas identifiable dans le résultat. Ni un succès, ni un échec.

---

## 3. Les trois modules

| Module | Rôle |
|---|---|
| [`harness/load_base.py`](../harness/load_base.py) | Construit `data/maxizoo.duckdb` depuis l'export, et **refuse d'écrire** si un volume dérive |
| [`harness/reference_questions.py`](../harness/reference_questions.py) | Lit les 13 questions, en dérive périmètre, valeurs attendues et contrôles |
| [`harness/battery.py`](../harness/battery.py) | Passe les questions à une cible et rend les compteurs |

### Rien n'est recopié

`questions_reference.md`, `dictionnaire.md` et les tables vivent dans la branche
orpheline `export-db` du dépôt `sales-ops-planning-poc`. Le choix est celui du projet
d'origine et il est repris tel quel : *« une copie versionnée dériverait de lui en
silence »*. Le banc les lit au runtime.

### Les réponses attendues ne sont pas parsées

Le document de référence est régulier sur trois choses (le titre `## Q<n>`, la question
en citation, le bloc SQL) et **irrégulier sur les réponses** — gras pour un scalaire,
tableau markdown pour un résultat multiligne, prose ailleurs. Les parser serait fragile.

Le banc **exécute le SQL de référence** sur la base. C'est la définition même de la
réponse de référence (*« chaque réponse a été produite en exécutant la requête sur les
fichiers de `tables/` »*), et c'est sans dérive par construction.

### Quelles valeurs sont exigées

Exiger *tous* les nombres d'un résultat ferait échouer une réponse correcte mais
résumée ; n'en exiger aucun ne vérifierait rien. Le compromis :

- résultat court (≤ 4 lignes) : on exige tout ;
- résultat long : la première ligne **et l'extremum de chaque colonne numérique**.

L'extremum n'est pas un ornement. Q7 (« quel jour vend le mieux ? ») est triée par jour
de semaine : sa première ligne est lundi. Exiger les chiffres de lundi validerait une
réponse qui rate justement le samedi. Le maximum rattrape ce cas **sans connaître la
question**.

---

## 4. Utilisation

Prérequis, une fois :

```bash
git -C ../sales-ops-planning-poc archive origin/export-db | tar -x -C data/export
```

```bash
uv run python -m harness.load_base --export data/export
```

Attendu : `data/maxizoo.duckdb` (~180 Mo, 1 659 181 lignes) et
`data/maxizoo_dictionnaire.md`. Le chargeur refuse d'écrire si un volume ne tombe pas
juste. Le dossier `data/` est gitignoré.

Mesurer :

```bash
uv run python -m harness.battery --target agent
```

```bash
uv run python -m harness.battery --target agent --scope --checks
```

| Option | Effet |
|---|---|
| `--target agent` | Le moteur de ce dépôt : une tâche atomique par question |
| `--target daa-api --base-url URL` | `data-analyst-agent` interrogé en HTTP, **sans qu'un de ses fichiers bouge** |
| `--scope` | Périmètre (tables, grain) dérivé du SQL de référence |
| `--checks` | Contrôles contraignants issus des ordres de grandeur du dictionnaire |
| `--only Q5 Q10` | Ne passer que certaines questions |

Le code de sortie vaut 1 dès qu'une réponse est `faux`.

---

## 5. Résultats

Quatre exécutions, modèle `gemma4:e4b`, base complète.

| Configuration | juste | bloqué | **faux** |
|---|---|---|---|
| Moteur nu (base entière, sans dictionnaire) | 3 | 1 | **9** |
| + périmètre + dictionnaire | 5 | 2 | **6** |
| + contrôles (run 1, avec le défaut D-9) | 6 | 2 | **5** |
| + contrôles (run 2, corrigé) | 5 | 1 | **7** |
| *Référence : `data-analyst-agent`, chiffre de son README* | *7* | *—* | *—* |

### Ce qui est solide

**Le dictionnaire et le périmètre apportent un gain réel.** Q4 et Q12 passent de fausses
à justes, et le restent sur **trois exécutions indépendantes**. Le `faux` tombe de 9 à 6.

### Ce qui n'est pas concluant

**L'apport des contrôles n'est pas mesurable à une exécution par configuration.**

| Question | nu | scope+dico | +checks (1) | +checks (2) |
|---|---|---|---|---|
| Q4, Q12 | faux | **juste** | juste | juste |
| Q7 | faux | faux | faux | **bloqué** |
| Q8 | faux | faux | **bloqué** | faux |
| Q10 | bloqué | bloqué | bloqué | **faux** |
| Q13 | faux | **bloqué** | **juste** | **faux** |

**Quatre questions sur treize changent de verdict entre des exécutions quasi
identiques.** Q13 a pris trois valeurs différentes sur trois exécutions. La variance
d'une exécution à l'autre est du même ordre que l'écart entre les configurations :
aucune conclusion sur les contrôles ne tient à n=1.

### Pourquoi les contrôles n'ont presque rien attrapé

C'est le résultat le plus instructif, et il oriente la suite.

En lisant les motifs d'échec — Q3 « 37,2 absente », Q5 « 2,76 absente », Q9, Q11, Q13 —
le mode de défaillance dominant apparaît : l'agent **ne produit pas un nombre aberrant**,
il **ne produit pas du tout la grandeur demandée**. Il répond à une question voisine.

Or `magnitude` protège contre « le chiffre est absurde ». Sur une grandeur absente, il
n'échoue pas : il est **non vérifiable**. Ce sont les contrôles de forme — `columns`
(les dimensions demandées sont-elles là ?) et `row_count` — qui adresseraient ce
mode-là, et ils ne sont pas encore branchés dans le banc.

### Le coût, mesuré

| | sans dictionnaire | avec dictionnaire |
|---|---|---|
| Tokens par question | ~3 200 | ~11 000 |
| Q7 | 110 s | 291 s |

L'ancrage n'est pas gratuit. Le chiffre est mesuré, pas estimé.

---

## 6. Ce que le banc a trouvé dans le moteur

Brancher le banc sur de vraies requêtes a révélé quatre défauts qu'aucun test synthétique
n'avait vus. Ils sont corrigés et gardés par des tests.

| Défaut | Conséquence | Où |
|---|---|---|
| `EXTRACT(YEAR FROM s.date)` compté comme une table `s.date` | Une requête légitime refusée par l'allowlist | `referenced_tables` |
| Les `Decimal` de duckdb non vus comme des nombres | Le CA de Q1 disparaissait **silencieusement** des contrôles | `_json_sur` |
| Seul le rendu texte des résultats était conservé | Un contrôle chiffré aurait porté sur la prose du modèle | `tables_produites` |
| Un contrôle sans colonne désignée jugeait tous les nombres | **A bloqué une réponse juste** (Q8) | D-9, `column_pattern` |

Le dernier est le plus grave : un contrôle qui refuse le juste apprend à ignorer les
blocages, et rend l'ensemble du dispositif inutile.

### Un constat qui a tranché une décision de conception

Sur Q5 — le panier article, dont le piège est documenté — deux agents différents tombent
dans le piège de **deux façons opposées** :

| Agent | Valeur rendue | Écart |
|---|---|---|
| `data-analyst-agent` | 0,047 | 60 fois trop petit |
| Ce moteur | 44,5 | 20 fois trop grand |
| *Attendu* | *2,76* | |

Un contrôle qui vérifierait la **forme de la requête** n'en attraperait qu'une. Un
contrôle qui vérifie la **valeur** les attrape toutes les deux. C'est pourquoi le
contrôle de grain est traité comme un fil-piège, et le contrôle de valeur comme la
ligne de défense.

---

## 7. Limites du banc

- **La variance domine à n=1.** Toute comparaison de configurations demande 3 à 5
  exécutions chacune, soit environ une heure de GPU par configuration.
- **Les bornes ne couvrent que 5 questions sur 13** — celles que le dictionnaire
  documente. Les huit autres ne sont pas protégées. C'est une limite du dictionnaire,
  pas du mécanisme.
- **Le périmètre est dérivé du SQL de référence**, ce qui est une facilité du banc et
  **pas** une capacité de production : en usage réel, c'est le décomposeur, ou un humain
  à la GATE 1, qui déclare le périmètre.
- **`expect_values` suppose de connaître la réponse** : ce contrôle n'existe que pour le
  banc. En production, si on connaissait la réponse, on ne poserait pas la question.
- **Le banc mesure une tâche isolée par question**, pas une investigation décomposée.
  Le mode complet — décomposition, dépendances, deux gates — n'est pas encore mesuré.

## 8. Suite

| Priorité | Action | Pourquoi |
|---|---|---|
| 1 | Brancher `columns` / `row_count` sur les 13 questions | C'est là qu'est le gisement d'après les motifs d'échec ; dérivable sans connaître la réponse |
| 2 | 3 exécutions par configuration, `temperature: 0` | Sans ça, tout chiffre reste indicatif |
| 3 | Enrichir le dictionnaire de la source | Fait passer la couverture des bornes au-delà de 5/13 |
| 4 | Mesurer le mode décomposé complet | Le banc ne teste aujourd'hui qu'une tâche par question |
