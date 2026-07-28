# Modèle de données

## MCD — modèle conceptuel

Étoile classique : une table de faits (`sales_daily`) au centre, entourée de ses
dimensions. Deux faits secondaires (`traffic_daily`, `sales_hourly`) partagent la
dimension magasin. La seule relation N-N est `promo_scope`, qui relie les
campagnes aux SKU qu'elles ciblent.

```mermaid
erDiagram
    STORES ||--o{ SALES_DAILY   : "réalise"
    STORES ||--o{ TRAFFIC_DAILY : "accueille"
    STORES ||--o{ SALES_HOURLY  : "détaille"
    STORES ||--o{ STORE_HOURS   : "ouvre selon"
    STORES ||--o{ WEATHER       : "subit"
    STORES |o--o{ PROMO_CALENDAR : "hôte si ouverture"

    PRODUCTS ||--o{ SALES_DAILY : "est vendu"
    PRODUCTS ||--o{ PROMO_SCOPE : "est ciblé par"

    PROMO_CALENDAR ||--o{ PROMO_SCOPE : "cible"
    PROMO_CALENDAR |o--o{ SALES_DAILY : "s'applique à"

    STORES {
        varchar store_id PK "S01..S12 ou ONLINE"
        text    store_name
        text    region
        text    store_type "grand moyen petit online"
        int     surface_m2
        int     population
        float   latitude "NULL si ONLINE"
        float   longitude "NULL si ONLINE"
        int     is_online "0 ou 1"
    }

    PRODUCTS {
        varchar sku_id PK "SKU001..SKU060"
        text    sku_label
        text    commodity_group "8 univers"
        text    brand
        text    brand_type "nationale exclusive distributeur"
        int     is_eb
        int     is_pl
        numeric base_price "hors inflation et promo"
        date    launch_date "NULL sauf 4 SKU cold start"
    }

    PROMO_CALENDAR {
        varchar promo_id PK
        text    campaign_name
        text    promo_type "6 typologies"
        text    mechanic
        numeric discount_rate "0 sauf type produits"
        date    date_start
        date    date_end
        text    perimeter "magasin online omnicanal"
        varchar store_id FK "NULL sauf ouverture_magasin"
    }

    PROMO_SCOPE {
        varchar promo_id PK, FK
        varchar sku_id PK, FK
    }

    SALES_DAILY {
        date    date PK
        varchar store_id PK, FK
        varchar sku_id PK, FK
        int     quantity "0 frequent"
        numeric unit_price "avec inflation et remise"
        numeric revenue "quantity x unit_price"
        int     is_rupture "1 = vente censuree"
        varchar promo_id FK "NULL si hors promo"
    }

    TRAFFIC_DAILY {
        date    date PK
        varchar store_id PK, FK
        int     nb_tickets "commandes si ONLINE"
    }

    SALES_HOURLY {
        date    date PK
        varchar store_id PK, FK
        int     hour PK "heures ouvrees seulement"
        numeric ca
        int     nb_tickets
    }

    STORE_HOURS {
        varchar store_id PK, FK
        int     day_of_week PK "0 = lundi"
        numeric open_hour
        numeric close_hour
        int     is_closed
    }

    WEATHER {
        date    date PK "va jusqu-au 2026-12-31"
        varchar store_id PK, FK
        numeric temp_mean_c
        numeric rain_mm
        text    source "open-meteo ou climatologie"
        numeric temp_anomaly "porte le signal meteo"
    }

    INFLATION {
        char    month PK "YYYY-MM"
        numeric inflation_mm_pct
        numeric cpi_index "base 100 = 2021-07"
        int     is_projection
    }
```

`INFLATION` n'a pas de clé étrangère : elle se joint sur le **mois** de la date
(`TO_CHAR(s.date, 'YYYY-MM') = i.month`).

---

## Schéma fonctionnel — comment la donnée est orchestrée

Lecture de gauche à droite : les référentiels décrivent le **contexte**
(qui vend, quoi, quand, sous quelle promo, par quel temps) ; ils alimentent la
génération de la **demande**, qui produit les tables de faits.

```mermaid
flowchart LR
    subgraph REF["Référentiels — le contexte"]
        ST["stores<br/>12 magasins + ONLINE"]
        PR["products<br/>60 SKU / 8 univers"]
        PC["promo_calendar<br/>56 campagnes / 6 typologies"]
        PS["promo_scope<br/>ciblage SKU N-N"]
        SH["store_hours<br/>horaires"]
        IN["inflation<br/>indice mensuel"]
        WX["weather<br/>météo réelle Open-Meteo"]
    end

    subgraph GEN["Formation de la demande"]
        D["Quantité vendue<br/>= base SKU × magasin<br/>× saison × jour de semaine<br/>× tendance × météo<br/>× promo × ruptures"]
        P["Prix pratiqué<br/>= base_price<br/>× inflation<br/>× (1 − remise)"]
    end

    subgraph FACT["Faits — le réalisé"]
        SD["sales_daily<br/>magasin × SKU × jour<br/>1,36 M lignes"]
        TD["traffic_daily<br/>tickets / jour"]
        SHY["sales_hourly<br/>CA par heure ouvrée"]
    end

    ST --> D
    PR --> D
    PC --> PS
    PS --> D
    WX --> D
    PC --> P
    PR --> P
    IN --> P
    D --> SD
    P --> SD
    SD --> TD
    SD --> SHY
    SH --> SHY
    ST --> TD
```

### Les signaux volontairement encodés

Le jeu n'est pas du bruit aléatoire : chaque mécanisme ci-dessous est **présent
et mesurable en SQL**. C'est ce qui rend les questions intéressantes — et ce qui
permet de vérifier qu'une réponse d'agent est juste plutôt que plausible.

| Mécanisme | Où le voir | Effet attendu |
|---|---|---|
| Saisonnalité hebdo | `sales_daily` × `stores` | samedi en magasin, dimanche en ligne |
| Saisonnalité annuelle | `sales_daily` × `products` | antiparasitaires l'été, oiseaux l'hiver, jouets à Noël |
| Jours fériés | `traffic_daily` | fermeture 1er mai / Noël / jour de l'an |
| Tendance | `sales_daily` par année | ONLINE ≈ +14 %/an, Dijon en léger déclin |
| Inflation | `unit_price` vs `base_price` | dérive des prix 2021-2023 |
| Promo `produits` | `promo_scope` + `promo_calendar` | uplift ≈ 1 + 2,2 × remise, puis creux |
| Promo `influence` | idem, type `influence` | pic décalé vers J+10, online |
| Promo `seuils` | `traffic_daily` | panier article dopé, pas les quantités |
| Météo | `weather.temp_anomaly` | canicule → trafic magasin −4,5 %/°C |
| Ruptures | `is_rupture` | ~1,1 % des lignes, ventes censurées |
| Cold start | `products.launch_date` | 4 SKU sans historique avant lancement |
