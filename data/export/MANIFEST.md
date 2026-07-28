# MANIFEST — export de la base POC

Fichier généré par `python -m src.export_db` — ne pas éditer à la main.

- Variante : **complète (5 ans)**
- Période des faits : **2021-07-01 -> 2026-06-30**
- Format : **both**
- Graine du générateur : **42** (jeu reproductible)
- Total : **1 659 181 lignes** sur 10 tables

## Tables

| Table | Rôle | Lignes | Colonnes | Taille CSV | MD5 (CSV) |
|---|---|---:|---:|---:|---|
| `stores` | référentiel (cœur) | 13 | 9 | 0.0 Mo | `29baf74a0d5f…` |
| `store_hours` | enrichissement | 91 | 5 | 0.0 Mo | `b596ebdf10ff…` |
| `products` | référentiel (cœur) | 60 | 9 | 0.0 Mo | `7b54d2b53c57…` |
| `promo_calendar` | référentiel (cœur) | 56 | 9 | 0.0 Mo | `54b79889b67a…` |
| `promo_scope` | jointure N-N (cœur) | 349 | 2 | 0.0 Mo | `f742e6728f2b…` |
| `inflation` | enrichissement | 66 | 4 | 0.0 Mo | `662c6a8362e9…` |
| `weather` | enrichissement | 26 130 | 6 | 1.1 Mo | `1ff637db3133…` |
| `traffic_daily` | fait (cœur) | 23 738 | 3 | 0.4 Mo | `bac47e27bed2…` |
| `sales_daily` | FAIT principal | 1 363 726 | 8 | 52.3 Mo | `f1f8e4f659ca…` |
| `sales_hourly` | fait (grain horaire) | 244 952 | 5 | 6.7 Mo | `07519110376d…` |

## Contrôles d'intégrité

Exécutés à chaque export ; l'export échoue si un contrôle est rouge.

```
[OK ] FK store_hours.store_id -> stores.store_id
[OK ] FK promo_calendar.store_id -> stores.store_id
[OK ] FK promo_scope.promo_id -> promo_calendar.promo_id
[OK ] FK promo_scope.sku_id -> products.sku_id
[OK ] FK weather.store_id -> stores.store_id
[OK ] FK traffic_daily.store_id -> stores.store_id
[OK ] FK sales_hourly.store_id -> stores.store_id
[OK ] FK sales_daily.store_id -> stores.store_id
[OK ] FK sales_daily.sku_id -> products.sku_id
[OK ] FK sales_daily.promo_id -> promo_calendar.promo_id
[OK ] revenue == quantity x unit_price — écart max 0.0000 €
[OK ] aucun prix / quantité / CA négatif
[OK ] aucune vente avant launch_date (4 SKU en cold start)
```
