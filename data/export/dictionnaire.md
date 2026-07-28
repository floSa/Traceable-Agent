# Dictionnaire de données

Base **retail animalerie** : 13 magasins (12 physiques + 1 canal e-commerce),
60 SKU, 5 ans d'historique quotidien (2021-07-01 → 2026-06-30).

> Ce document est conçu pour être **chargé en contexte par un agent text-to-SQL**.
> Il décrit chaque colonne, les valeurs autorisées, les jointures usuelles et
> — surtout — les **pièges de modélisation** qui font écrire du SQL faux.

⚠️ **Données 100 % synthétiques.** Aucune donnée réelle d'enseigne. Seule
exception : `weather` contient de la vraie météo historique (API Open-Meteo).
Les montants n'ont aucune valeur métier.

---

## Vue d'ensemble

| Table | Rôle | Grain (= clé primaire) | Lignes |
|---|---|---|---:|
| `stores` | référentiel | `store_id` | 13 |
| `products` | référentiel | `sku_id` | 60 |
| `promo_calendar` | référentiel | `promo_id` | 56 |
| `promo_scope` | jointure N-N | `promo_id` + `sku_id` | 349 |
| `sales_daily` | **fait principal** | `date` + `store_id` + `sku_id` | 1 363 726 |
| `traffic_daily` | fait | `date` + `store_id` | 23 738 |
| `sales_hourly` | fait | `date` + `store_id` + `hour` | 244 952 |
| `store_hours` | référentiel | `store_id` + `day_of_week` | 91 |
| `inflation` | référentiel | `month` | 66 |
| `weather` | référentiel | `date` + `store_id` | 26 130 |

Les six premières suffisent à la majorité des questions. `store_hours`,
`inflation` et `weather` sont des tables d'enrichissement.

---

## Les 6 pièges à connaître

Ils sont la principale source de SQL faux sur cette base.

**1. Le e-commerce est un magasin.** Le canal en ligne est la ligne
`store_id = 'ONLINE'` de `stores`. Toute requête « par magasin » **l'inclut donc
par défaut** et il pèse ~20 % du CA. Pour ne garder que le physique :
`WHERE is_online = 0`.

**2. `quantity = 0` est une vraie ligne.** ~46 % des lignes de `sales_daily` ont
`quantity = 0` : c'est un jour sans vente pour ce SKU dans ce magasin, pas une
donnée manquante. Conséquence : `AVG(quantity)` inclut ces zéros (c'est en
général ce qu'on veut), et `COUNT(*)` compte des jours, pas des ventes.

**3. Absence de ligne ≠ zéro.** Une ligne absente signifie que le SKU **n'était
pas encore lancé** (voir `products.launch_date`). Les 4 SKU en « cold start »
n'ont aucune ligne avant leur lancement.

**4. `revenue` est le CA *réalisé*, pas la demande.** Quand `is_rupture = 1`
(~1,1 % des lignes), le stock était épuisé : la vente est **censurée** et
sous-estime la demande réelle. Une question sur « ce qu'on aurait pu vendre »
ne se répond pas en sommant `revenue`.

**5. Une campagne sans lignes dans `promo_scope` porte sur TOUT le catalogue.**
C'est le cas des 13 campagnes de type `seuils`, `cadeau_seuil` et
`ouverture_magasin` : elles n'ont pas de ciblage SKU. Un `JOIN promo_scope` les
fait donc silencieusement disparaître — utiliser un `LEFT JOIN` si on veut les
garder.

**6. La base contient du futur.** L'historique des ventes s'arrête au
**2026-06-30**, mais `promo_calendar`, `weather` et `inflation` vont jusqu'au
**2026-12-31** (c'est le scénario de prévision du POC). Une jointure entre
`weather` et `sales_daily` sur le S2 2026 ne renverra donc rien côté ventes.

---

## `stores` — magasins

Grain : un magasin. 12 villes françaises réelles + 1 canal e-commerce.
La taille de la ville dimensionne le magasin (Paris 1 400 m² → Brive 450 m²).

| Colonne | Type | Description |
|---|---|---|
| `store_id` | VARCHAR **PK** | `S01`…`S12`, ou `ONLINE` |
| `store_name` | TEXT | nom de la ville, ou `Canal Online` |
| `region` | TEXT | `Île-de-France & Est`, `Ouest`, `Sud` (4 magasins chacune), `ONLINE` |
| `store_type` | TEXT | `grand` (4), `moyen` (4), `petit` (4), `online` (1) |
| `surface_m2` | INTEGER | 450 → 1 400. **0 pour ONLINE** |
| `population` | INTEGER | population de la ville. **0 pour ONLINE** |
| `latitude`, `longitude` | DOUBLE | **NULL pour ONLINE** |
| `is_online` | SMALLINT | `1` pour la ligne ONLINE, `0` sinon |

## `products` — catalogue

Grain : un SKU. 60 SKU sur 8 univers.

| Colonne | Type | Description |
|---|---|---|
| `sku_id` | VARCHAR **PK** | `SKU001`…`SKU060` |
| `sku_label` | TEXT | libellé commercial |
| `commodity_group` | TEXT | univers — voir répartition ci-dessous |
| `brand` | TEXT | `MaxiZoo Sélection` (18), `Ligne Prestige` (12), puis 7 marques nationales |
| `brand_type` | TEXT | `distributeur` (18), `exclusive` (12), `nationale` (30) |
| `is_eb` | SMALLINT | exclusivité enseigne — équivaut à `brand_type = 'exclusive'` |
| `is_pl` | SMALLINT | marque de distributeur — équivaut à `brand_type = 'distributeur'` |
| `base_price` | NUMERIC | prix catalogue de référence, **hors inflation et hors promo** (3,49 € → 189 €) |
| `launch_date` | DATE | **NULL = présent depuis le début** (56 SKU). Non NULL = cold start (4 SKU) |

`commodity_group` : `Chien` (14), `Chat` (14), `Aquariophilie` (7), `Oiseau` (6),
`Rongeur` (6), `Hygiène & Soins` (5), `Reptile` (4), `Accessoires & Jouets` (4).

Les 4 SKU en cold start : `SKU003` (2023-03-15), `SKU026` (2024-09-01),
`SKU049` (2025-02-15), `SKU060` (2025-10-01).

## `promo_calendar` — campagnes

Grain : une campagne. 56 campagnes, 6 typologies aux effets **volontairement
distincts** (c'est le cœur du jeu de données).

| Colonne | Type | Description |
|---|---|---|
| `promo_id` | VARCHAR **PK** | `P001`… |
| `campaign_name` | TEXT | `Black Friday 2025`, `Cat Days 2024`… |
| `promo_type` | TEXT | voir tableau des typologies |
| `mechanic` | TEXT | libellé métier : `-20 % univers chat`, `Peluche offerte dès 40 €` |
| `discount_rate` | NUMERIC | 0.15 → 0.30 pour `produits`, **0 pour toutes les autres typologies** |
| `date_start`, `date_end` | DATE | bornes **incluses** |
| `perimeter` | TEXT | `omnicanal` (44), `online` (10), `magasin` (2) |
| `store_id` | VARCHAR | **NULL sauf pour `ouverture_magasin`** (campagne d'un seul magasin) |

| `promo_type` | Nb | Remise ? | Ciblage SKU ? | Effet encodé dans les données |
|---|---:|---|---|---|
| `produits` | 27 | oui (15-30 %) | oui | uplift ≈ `1 + 2,2 × remise`, puis **creux la semaine suivante** (report d'achat) |
| `influence` | 10 | non | oui | uplift **décalé**, pic vers J+10, étalé sur 28 j, online seulement |
| `mise_en_avant` | 6 | non | oui | uplift modéré ×1,25 (tête de gondole) |
| `cadeau_seuil` | 6 | non | **non** | +8 % de quantité, dope le **panier** |
| `seuils` | 5 | non | **non** | +10 % de quantité, dope le **panier** |
| `ouverture_magasin` | 2 | non | **non** | pic ×1,6 sur **un seul magasin** |

## `promo_scope` — périmètre SKU des campagnes (N-N)

Grain : couple campagne × SKU. **Une campagne absente de cette table cible tout
le catalogue** (voir piège n°5).

| Colonne | Type | Description |
|---|---|---|
| `promo_id` | VARCHAR **PK/FK** | → `promo_calendar.promo_id` |
| `sku_id` | VARCHAR **PK/FK** | → `products.sku_id` |

## `sales_daily` — ventes (table de faits principale)

Grain : **magasin × SKU × jour**. 1 363 726 lignes.

| Colonne | Type | Description |
|---|---|---|
| `date` | DATE **PK** | 2021-07-01 → 2026-06-30 |
| `store_id` | VARCHAR **PK/FK** | → `stores` (inclut `ONLINE`) |
| `sku_id` | VARCHAR **PK/FK** | → `products` |
| `quantity` | INTEGER | unités vendues. **0 possible et fréquent** (~46 %) |
| `unit_price` | NUMERIC | prix réellement pratiqué = `base_price` × indice d'inflation du mois × (1 − remise) |
| `revenue` | NUMERIC | = `quantity × unit_price` (vérifié à l'euro près) |
| `is_rupture` | SMALLINT | `1` = rupture de stock, **vente censurée** (~1,1 %) |
| `promo_id` | VARCHAR FK | campagne active ce jour-là sur ce SKU, **NULL si aucune**. En cas de campagnes concurrentes, la plus prioritaire est retenue |

## `traffic_daily` — fréquentation

Grain : magasin × jour.

| Colonne | Type | Description |
|---|---|---|
| `date` | DATE **PK** | |
| `store_id` | VARCHAR **PK/FK** | |
| `nb_tickets` | INTEGER | tickets de caisse ; **= nombre de commandes pour ONLINE**. `0` les jours de fermeture |

Panier article (PA) = `SUM(sales_daily.quantity) / nb_tickets` → ~2,1 (petit
magasin) à ~2,8 (online).

## `sales_hourly` — ventes horaires

Grain : magasin × jour × **heure ouvrée**. Seules les heures d'ouverture ont une
ligne (pas de ligne à 3 h du matin pour un magasin physique) — le magasin
`ONLINE` a les 24 heures.

| Colonne | Type | Description |
|---|---|---|
| `date` | DATE **PK** | |
| `store_id` | VARCHAR **PK/FK** | |
| `hour` | SMALLINT **PK** | 0 → 23 |
| `ca` | NUMERIC | CA de l'heure |
| `nb_tickets` | INTEGER | tickets de l'heure |

Par construction : `SUM(ca)` par magasin/jour = `SUM(sales_daily.revenue)` du
même magasin/jour (écart max constaté : 0,05 €).

## `store_hours` — horaires théoriques

Grain : magasin × jour de semaine. Sert au dimensionnement des effectifs.

| Colonne | Type | Description |
|---|---|---|
| `store_id` | VARCHAR **PK/FK** | |
| `day_of_week` | SMALLINT **PK** | **0 = lundi … 6 = dimanche** |
| `open_hour` | NUMERIC | `9.0` = 9 h ; `0` si fermé ; `0` pour ONLINE |
| `close_hour` | NUMERIC | `19.5` = **19 h 30** ; `24.0` pour ONLINE |
| `is_closed` | SMALLINT | `1` = fermé ce jour-là |

⚠️ `day_of_week` est en convention **0 = lundi**, alors que `EXTRACT(DOW)` de
PostgreSQL renvoie **0 = dimanche**. Utiliser `EXTRACT(ISODOW) - 1` pour joindre.

## `inflation` — indice des prix mensuel

Grain : un mois. Base 100 = 2021-07. Paramètre central du POC : c'est lui qui
fait dériver `unit_price` de `base_price`.

| Colonne | Type | Description |
|---|---|---|
| `month` | CHAR(7) **PK** | `YYYY-MM`, de `2021-07` à `2026-12` |
| `inflation_mm_pct` | NUMERIC | inflation mois/mois en % |
| `cpi_index` | NUMERIC | indice cumulé (100 → ~106) |
| `is_projection` | SMALLINT | `1` pour les 6 mois du S2 2026 (projection, pas du réalisé) |

Jointure : `TO_CHAR(s.date, 'YYYY-MM') = i.month` (PostgreSQL) ou
`STRFTIME(s.date, '%Y-%m') = i.month` (DuckDB).

## `weather` — météo quotidienne

Grain : magasin × jour. **Vraie météo historique** (Open-Meteo) des 12 villes ;
le magasin `ONLINE` porte une moyenne nationale.

| Colonne | Type | Description |
|---|---|---|
| `date` | DATE **PK** | 2021-07-01 → **2026-12-31** (dépasse l'historique des ventes) |
| `store_id` | VARCHAR **PK/FK** | |
| `temp_mean_c` | NUMERIC | température moyenne du jour |
| `rain_mm` | NUMERIC | pluie du jour |
| `source` | TEXT | `open-meteo` (23 738 = réalisé) ou `climatologie` (2 392 = normales, pour le futur) |
| `temp_anomaly` | NUMERIC | **écart à la normale du jour** |

⚠️ Le signal météo est porté par **`temp_anomaly`, pas `temp_mean_c`** : 25 °C
en août est normal, 25 °C en avril ne l'est pas. Effets encodés : anomalie
chaude → trafic magasin en **baisse** (jusqu'à −4,5 %/°C) et online en hausse,
univers `Hygiène & Soins` / `Reptile` dopés ; anomalie froide → `Oiseau` dopé.

---

## Jointures usuelles

```sql
-- Le squelette de 90 % des requêtes
FROM sales_daily s
JOIN stores   st ON st.store_id = s.store_id
JOIN products p  ON p.sku_id   = s.sku_id

-- Promo appliquée (directe)
LEFT JOIN promo_calendar pc ON pc.promo_id = s.promo_id

-- SKU ciblés par une campagne (N-N)
JOIN promo_scope ps ON ps.promo_id = pc.promo_id AND ps.sku_id = s.sku_id

-- Trafic / panier
JOIN traffic_daily t ON t.store_id = s.store_id AND t.date = s.date

-- Météo
JOIN weather w ON w.store_id = s.store_id AND w.date = s.date

-- Inflation (PostgreSQL)
JOIN inflation i ON i.month = TO_CHAR(s.date, 'YYYY-MM')
```

## Ordres de grandeur (pour détecter une requête fausse)

| Repère | Valeur |
|---|---|
| CA total 5 ans | 45,7 M€ |
| CA année 2025 | 10,19 M€ |
| Part du e-commerce | 13,3 % (2021) → 20,3 % (2025), en croissance ~+14 %/an |
| Univers dominants | Chat 37 % + Chien 36 % du CA |
| Meilleur jour en magasin | **samedi** (1,82 M€ en 2025) |
| Meilleur jour en ligne | **dimanche** |
| Panier article moyen | 2,06 (petit) → 2,76 (online) |
| Lignes en rupture | ~1,1 % |
| Uplift Black Friday (−30 %) | ×1,5 à ×2,0 selon l'année |
