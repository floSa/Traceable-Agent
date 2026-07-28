# Base de démonstration — retail animalerie

> **Branche `export-db` — jeu de données uniquement.**
> Cette branche est *orpheline* : elle ne contient ni le code du POC, ni son
> historique, seulement la base et sa documentation. Tout est à la racine,
> il n'y a rien d'autre à trier.
> Le code qui produit ces fichiers vit sur la branche `main` du même dépôt.

Export autonome du jeu de données du POC de prévision des ventes, prêt à charger
dans n'importe quel moteur SQL. Conçu pour servir de terrain de jeu à un **agent
text-to-SQL** : jointures non triviales, signaux métier réels, et un jeu de
questions dont on connaît les réponses.

> ⚠️ **Données 100 % synthétiques.** Aucune donnée réelle d'enseigne. Les
> montants n'ont aucune valeur métier : ils démontrent une mécanique.
> Seule exception : `weather` contient de la vraie météo historique (Open-Meteo).

## Ce qu'il y a dans le dossier

| Fichier | À quoi ça sert |
|---|---|
| **`dictionnaire.md`** | Description de chaque table et colonne, valeurs autorisées, jointures, **et les 6 pièges de modélisation**. À charger en contexte de l'agent. |
| **`mcd.md`** | MCD (Mermaid) + schéma fonctionnel + liste des signaux encodés. |
| **`schema.sql`** | DDL PostgreSQL : tables, clés, contraintes `CHECK`, index. |
| **`questions_reference.md`** | 13 questions en langage naturel + SQL + **réponse attendue**. Le jeu d'évaluation. |
| **`chargement/`** | Scripts de chargement PostgreSQL / DuckDB / SQLite. |
| **`MANIFEST.md`** | Volumes, tailles, empreintes MD5, contrôles d'intégrité. Généré. |
| **`tables/`** | Les données : un fichier par table. |

## Le modèle en une phrase

Un schéma en étoile : **`sales_daily`** (magasin × SKU × jour, 1,36 M lignes) au
centre, entourée de `stores`, `products` et `promo_calendar` ; `promo_scope`
est la relation N-N qui relie les campagnes aux SKU ciblés ; `traffic_daily`
et `sales_hourly` ajoutent la fréquentation et le grain horaire ;
`weather`, `inflation` et `store_hours` enrichissent le contexte.

```
stores ──┬── sales_daily ──┬── products ── promo_scope ── promo_calendar
         │                 └── promo_calendar (promo appliquée)
         ├── traffic_daily
         ├── sales_hourly
         ├── store_hours
         └── weather                         inflation (jointure par mois)
```

Voir `mcd.md` pour le diagramme complet.

## Chargement

**PostgreSQL**
```bash
createdb poc_retail
psql -d poc_retail -f schema.sql
psql -d poc_retail -f chargement/postgres.sql   # à lancer depuis export_db/
```

**DuckDB**
```bash
duckdb poc_retail.duckdb -c ".read schema.sql" -c ".read chargement/duckdb.sql"
```

**SQLite**
```bash
bash chargement/sqlite.sh
```

**BigQuery / Snowflake** — charger les CSV de `tables/` avec le schéma de
`schema.sql`, en retirant les `PRIMARY KEY` / `FOREIGN KEY` / `CHECK` (non
contraignants sur ces moteurs) et en partitionnant `sales_daily` sur `date`.

### Ce qui a été vérifié

- `schema.sql` **et** les 1,66 M de lignes ont été chargés dans **DuckDB** avec
  toutes les contraintes actives (PK, FK, CHECK) : aucun rejet.
- Le DDL a été validé sur **SQLite** (après retrait de `CASCADE`, ce que fait le
  script).
- Les 13 requêtes de `questions_reference.md` ont été **exécutées** ; les
  réponses publiées sont celles obtenues.
- Le script PostgreSQL n'a pas pu être exécuté ici (pas de serveur disponible) —
  il utilise `\copy` et la syntaxe standard, mais c'est le seul du lot qui n'a
  pas été testé de bout en bout.

## Que dire à votre agent text-to-SQL

Une fois la base chargée, le contexte à lui donner :

```
Tu interroges une base de démonstration retail (animalerie) : 13 magasins dont
un canal e-commerce, 60 SKU, 5 ans d'historique quotidien (2021-07 → 2026-06).
Le dictionnaire de données complet est fourni ci-dessous — lis en particulier
la section « Les 6 pièges à connaître » avant d'écrire du SQL.

[coller ici le contenu de dictionnaire.md]

Règles :
- Réponds en SQL <PostgreSQL|DuckDB|…>, puis commente le résultat en français.
- Si une question est ambiguë sur le périmètre (avec ou sans e-commerce ?
  année civile ou année glissante ?), pose la question avant de requêter.
- Signale toujours quand un résultat porte sur une année partielle (2021 et
  2026 le sont).
```

Puis évaluez-le avec `questions_reference.md` : posez les questions en langage
naturel, comparez à la référence. Q2, Q5, Q8, Q10 et Q11 sont celles qui
discriminent vraiment (chacune a un piège documenté).

## Régénérer l'export

Le script `src/export_db.py` n'est pas sur cette branche : il vit sur `main`.

```bash
git checkout main
python -m src.export_db --format both    # CSV + Parquet
python -m src.export_db --light          # variante 12 mois (345 k lignes, 13 Mo)
```

Le script relit `data/`, nettoie (arrondis, vrais NULL, dates ISO), vérifie
l'intégrité référentielle et refuse d'écrire si un contrôle échoue. Le résultat
atterrit dans `export_db/`, dont le contenu est recopié à la racine de cette
branche.

Les données sources sont elles-mêmes reproductibles (graine fixe 42) via
`python -m src.generate_data` sur `main`.

## Périmètre et volumétrie

- **13 magasins** : 12 villes françaises réelles (la taille de la ville
  dimensionne le magasin, Paris 1 400 m² → Brive 450 m²) + 1 canal `ONLINE`.
- **60 SKU** sur 8 univers, dont **4 lancés en cours d'historique** (cold start).
- **56 campagnes promo** sur **6 typologies aux effets distincts**.
- **5 ans** de quotidien, du 2021-07-01 au 2026-06-30. 45,7 M€ de CA cumulé.
- **1,66 M de lignes** au total, 60 Mo en CSV (6 Mo en Parquet).

⚠️ `promo_calendar`, `weather` et `inflation` vont jusqu'au **2026-12-31**, soit
au-delà de l'historique des ventes : c'est le scénario de prévision du POC.
