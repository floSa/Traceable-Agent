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

### 5.1 Premières mesures, à `temperature: 0.2`

Une exécution par configuration. **Ces chiffres n'ont pas permis de conclure**, et la
raison vaut d'être gardée.

| Configuration | juste | bloqué | **faux** |
|---|---|---|---|
| Moteur nu (base entière, sans dictionnaire) | 3 | 1 | **9** |
| + périmètre + dictionnaire | 5 | 2 | **6** |
| + contrôles de grandeur (run 1, avec le défaut D-9) | 6 | 2 | **5** |
| + contrôles de grandeur (run 2, corrigé) | 5 | 1 | **7** |
| *Référence : `data-analyst-agent`, chiffre de son README* | *7* | *—* | *—* |

**Quatre questions sur treize changeaient de verdict entre des exécutions quasi
identiques**, Q13 en prenant trois valeurs différentes. La variance était du même ordre
que l'écart entre les configurations : à n=1, aucune conclusion ne tenait.

Un seul enseignement était solide, parce qu'il se répétait sur trois exécutions :
**le dictionnaire et le périmètre apportent un gain réel** — Q4 et Q12 passent de fausses
à justes et le restent, et `faux` tombe de 9 à 6.

### 5.2 Le diagnostic qui a débloqué la suite

En lisant les motifs d'échec — Q3 « 37,2 absente », Q5 « 2,76 absente », Q9, Q11, Q13 —
le mode de défaillance dominant apparaît : l'agent **ne produit pas un nombre aberrant**,
il **ne produit pas du tout la grandeur demandée**. Il répond à une question voisine.

Or `magnitude` protège contre « le chiffre est absurde ». Sur une grandeur absente, il
n'échoue pas : il est **non vérifiable**. C'est ce constat qui a motivé les contrôles de
forme — `column_count` (les dimensions demandées sont-elles là ?) et `row_count`.

### 5.3 Mesures concluantes, à `temperature: 0`

Quatre exécutions, deux par configuration, contrôles de forme branchés.

| Configuration | juste | bloqué | **faux** |
|---|---|---|---|
| périmètre + dictionnaire, sans contrôles | 5 | 2 | **6** |
| + contrôles de forme | 3 | 8–9 | **1–2** |

**`faux` passe de 6 à 1.** Cinq réponses fausses deviennent des blocages explicites. C'est
la revendication du projet — transformer du faux en absent — et elle est ici mesurée.

Le détail par contrôle, obtenu en retirant `column_count` puis en le remettant :

| Contrôle | Bloque à raison | Bloque à tort |
|---|---|---|
| `column_count` | Q3, Q8, Q9 | **Q6** |
| `row_count` | Q7, Q11 | aucune |

### Le faux positif est nommé, pas noyé

Sur **Q6** (« les 10 SKU les plus vendus en CA »), le SQL de référence rend quatre
colonnes dont des descriptifs de confort ; l'agent en rend deux, le SKU et le CA, et sa
réponse est juste. Le contrôle la bloque.

C'est la faute D-9 sous une autre forme : **déduire une contrainte d'un artefact qui n'en
porte pas**. Les colonnes de la requête de référence ne sont pas une exigence de la
question. Le contrôle est malgré tout conservé — cinq blocages à raison pour un à tort,
et `faux` est la métrique à conduire à zéro — mais l'échange est nommé ici pour ce qu'il
est : un contrôle qui refuse du juste apprend à ignorer les blocages, et ce coût-là ne se
lit dans aucun taux.

**Q12 n'est pas un faux positif**, contrairement à une première lecture : l'agent rend
128 lignes là où la référence en a 12. Il n'a pas fait la réduction demandée.

### Ce que le passage à `temperature: 0` a réglé, et ce qu'il n'a pas réglé

Deux exécutions sans contrôles rendent **le même verdict question par question**. La
variance qui rendait tout ininterprétable a disparu.

Elle n'a pas disparu totalement : une troisième exécution diverge sur **Q7**, qui produit
un SQL différent — 159 s et 21 k tokens contre 60 s et 12 k. Les contrôles ne touchent pas
au prompt (vérifié : `Task.checks` n'apparaît ni dans `build_context` ni dans l'agent),
donc c'est du non-déterminisme du service d'inférence. **La reproductibilité est bonne,
pas totale** — une conclusion tirée de deux exécutions identiques reste à confirmer.

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

- **La variance domine hors `temperature: 0`.** À 0,2, quatre questions sur treize
  changeaient de verdict d'une exécution à l'autre. À 0, deux exécutions concordent
  question par question — mais une troisième diverge sur Q7 : le déterminisme n'est pas
  garanti par le service d'inférence. Compter deux exécutions minimum par configuration,
  soit ~25 min de GPU chacune.
- **Les contrôles de forme se dérivent du SQL de référence**, ce qui est une facilité du
  banc. Le nombre de colonnes de la référence n'est pas une exigence de la question :
  c'est ce qui bloque Q6 à tort. En production, la forme attendue est déclarée par le
  décomposeur ou par l'humain de la GATE 1, et ce faux positif n'a pas lieu d'être.
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
| 1 | Faire remonter `juste` sans relâcher `faux` | Le dispositif sait dire « je ne sais pas » ; il ne sait pas encore mieux répondre. Q5 reste fausse dans toutes les configurations |
| 2 | Mesurer le mode décomposé complet | Le banc ne teste aujourd'hui qu'une tâche par question — ni décomposition, ni dépendances, ni les deux gates |
| 3 | Enrichir le dictionnaire de la source | Fait passer la couverture des bornes de grandeur au-delà de 5/13 |
| 4 | Rejouer sur un modèle plus grand | Sépare ce qui tient à l'architecture de ce qui tient à `gemma4:e4b` |

Fait : contrôles de forme branchés (§5.3), `temperature: 0` (§5.3).
