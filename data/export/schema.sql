-- =========================================================================
-- POC Prévision des ventes — schéma relationnel (PostgreSQL)
--
-- Données 100 % SYNTHÉTIQUES (voir README.md). Aucune donnée réelle.
-- Seule exception : la météo est de la vraie météo historique Open-Meteo.
--
-- Ordre de création = ordre des dépendances. Charger les tables dans le même
-- ordre (voir chargement/*.sql).
--
-- Portage vers un autre moteur :
--   DuckDB   : compatible tel quel.
--   SQLite   : remplacer NUMERIC(p,s) -> REAL, TEXT reste TEXT, DATE -> TEXT.
--   BigQuery : remplacer NUMERIC(p,s) -> NUMERIC, VARCHAR(n) -> STRING,
--              supprimer les PRIMARY KEY / FOREIGN KEY / CHECK (non contraignants)
--              et préférer un partitionnement sur sales_daily.date.
-- =========================================================================

-- Ordre inverse des dépendances (un DROP par table : DuckDB n'accepte pas
-- de liste, contrairement à PostgreSQL).
DROP TABLE IF EXISTS sales_hourly CASCADE;
DROP TABLE IF EXISTS sales_daily CASCADE;
DROP TABLE IF EXISTS traffic_daily CASCADE;
DROP TABLE IF EXISTS weather CASCADE;
DROP TABLE IF EXISTS inflation CASCADE;
DROP TABLE IF EXISTS promo_scope CASCADE;
DROP TABLE IF EXISTS promo_calendar CASCADE;
DROP TABLE IF EXISTS products CASCADE;
DROP TABLE IF EXISTS store_hours CASCADE;
DROP TABLE IF EXISTS stores CASCADE;

-- -------------------------------------------------------------------------
-- 1. stores — 12 magasins physiques + 1 magasin virtuel 'ONLINE'
--    Le canal e-commerce est modélisé comme un magasin : toute requête
--    « par magasin » l'inclut. Filtrer sur is_online pour l'exclure.
-- -------------------------------------------------------------------------
CREATE TABLE stores (
    store_id    VARCHAR(8)  PRIMARY KEY,
    store_name  TEXT        NOT NULL,           -- ville, ou 'Canal Online'
    region      TEXT        NOT NULL,           -- 'ONLINE' pour le e-commerce
    store_type  TEXT        NOT NULL CHECK (store_type IN ('grand','moyen','petit','online')),
    surface_m2  INTEGER     NOT NULL,           -- 0 pour ONLINE
    population  INTEGER     NOT NULL,           -- population de la ville ; 0 pour ONLINE
    latitude    DOUBLE PRECISION,               -- NULL pour ONLINE
    longitude   DOUBLE PRECISION,               -- NULL pour ONLINE
    is_online   SMALLINT    NOT NULL CHECK (is_online IN (0,1))
);

-- -------------------------------------------------------------------------
-- 2. store_hours — horaires théoriques par magasin x jour de semaine
--    Grain : 13 magasins x 7 jours = 91 lignes. 0 = lundi … 6 = dimanche.
-- -------------------------------------------------------------------------
CREATE TABLE store_hours (
    store_id    VARCHAR(8)  NOT NULL REFERENCES stores(store_id),
    day_of_week SMALLINT    NOT NULL CHECK (day_of_week BETWEEN 0 AND 6),
    open_hour   NUMERIC(4,2) NOT NULL,          -- 9.0 = 9 h ; 0 si fermé
    close_hour  NUMERIC(4,2) NOT NULL,          -- 19.5 = 19 h 30 ; 24.0 pour ONLINE
    is_closed   SMALLINT    NOT NULL CHECK (is_closed IN (0,1)),
    PRIMARY KEY (store_id, day_of_week)
);

-- -------------------------------------------------------------------------
-- 3. products — 60 SKU sur 8 univers (commodity_group)
--    launch_date NON NULL = SKU lancé en cours d'historique (cas « cold start ») :
--    aucune vente n'existe avant cette date.
-- -------------------------------------------------------------------------
CREATE TABLE products (
    sku_id          VARCHAR(8)  PRIMARY KEY,
    sku_label       TEXT        NOT NULL,
    commodity_group TEXT        NOT NULL,       -- Chien, Chat, Aquariophilie, Oiseau,
                                                -- Rongeur, Reptile, Hygiène & Soins,
                                                -- Accessoires & Jouets
    brand           TEXT        NOT NULL,
    brand_type      TEXT        NOT NULL CHECK (brand_type IN ('nationale','exclusive','distributeur')),
    is_eb           SMALLINT    NOT NULL CHECK (is_eb IN (0,1)),  -- exclusivité enseigne
    is_pl           SMALLINT    NOT NULL CHECK (is_pl IN (0,1)),  -- marque de distributeur
    base_price      NUMERIC(8,2) NOT NULL CHECK (base_price > 0), -- prix catalogue hors inflation
    launch_date     DATE                        -- NULL = présent depuis le début
);

-- -------------------------------------------------------------------------
-- 4. promo_calendar — 56 campagnes, 6 typologies aux effets distincts
--    store_id NON NULL uniquement pour promo_type = 'ouverture_magasin'
--    (campagne locale sur un seul magasin).
--    discount_rate = 0 pour les typologies sans remise (l'effet passe par
--    le trafic ou le panier, pas par le prix).
-- -------------------------------------------------------------------------
CREATE TABLE promo_calendar (
    promo_id      VARCHAR(8)  PRIMARY KEY,
    campaign_name TEXT        NOT NULL,
    promo_type    TEXT        NOT NULL CHECK (promo_type IN
                    ('produits','seuils','ouverture_magasin','influence',
                     'mise_en_avant','cadeau_seuil')),
    mechanic      TEXT        NOT NULL,         -- libellé métier : '-20 % univers chat'
    discount_rate NUMERIC(4,3) NOT NULL CHECK (discount_rate BETWEEN 0 AND 1),
    date_start    DATE        NOT NULL,
    date_end      DATE        NOT NULL,
    perimeter     TEXT        NOT NULL CHECK (perimeter IN ('magasin','online','omnicanal')),
    store_id      VARCHAR(8)  REFERENCES stores(store_id),
    CHECK (date_end >= date_start)
);

-- -------------------------------------------------------------------------
-- 5. promo_scope — périmètre SKU des campagnes (relation N-N)
--    ABSENCE DE LIGNES = campagne SANS ciblage SKU (seuils, cadeau_seuil,
--    ouverture_magasin) : elle porte sur tout le catalogue.
-- -------------------------------------------------------------------------
CREATE TABLE promo_scope (
    promo_id VARCHAR(8) NOT NULL REFERENCES promo_calendar(promo_id),
    sku_id   VARCHAR(8) NOT NULL REFERENCES products(sku_id),
    PRIMARY KEY (promo_id, sku_id)
);

-- -------------------------------------------------------------------------
-- 6. inflation — indice des prix mensuel (base 100 = 2021-07)
--    is_projection = 1 pour les mois postérieurs à l'historique réalisé.
-- -------------------------------------------------------------------------
CREATE TABLE inflation (
    month            CHAR(7)     PRIMARY KEY,   -- 'YYYY-MM'
    inflation_mm_pct NUMERIC(5,2) NOT NULL,     -- inflation mois/mois en %
    cpi_index        NUMERIC(8,3) NOT NULL,     -- indice cumulé
    is_projection    SMALLINT    NOT NULL CHECK (is_projection IN (0,1))
);

-- -------------------------------------------------------------------------
-- 7. weather — météo quotidienne par magasin
--    VRAIE météo historique (Open-Meteo) pour les 12 villes ; le magasin
--    ONLINE porte une moyenne nationale.
--    temp_anomaly = écart à la normale climatique du jour : c'est CETTE
--    colonne qui porte le signal météo, pas temp_mean_c (un 25 °C en août
--    est normal, un 25 °C en avril ne l'est pas).
--    Couvre aussi le futur (jusqu'au 2026-12-31) pour la prévision.
-- -------------------------------------------------------------------------
CREATE TABLE weather (
    date         DATE       NOT NULL,
    store_id     VARCHAR(8) NOT NULL REFERENCES stores(store_id),
    temp_mean_c  NUMERIC(5,2) NOT NULL,
    rain_mm      NUMERIC(6,2) NOT NULL,
    source       TEXT       NOT NULL CHECK (source IN ('open-meteo','synthetique','climatologie')),
    temp_anomaly NUMERIC(5,2) NOT NULL,
    PRIMARY KEY (date, store_id)
);

-- -------------------------------------------------------------------------
-- 8. traffic_daily — fréquentation par magasin x jour
--    nb_tickets = nombre de tickets de caisse (= nombre de commandes pour ONLINE).
--    Panier article (PA) = SUM(sales_daily.quantity) / nb_tickets.
-- -------------------------------------------------------------------------
CREATE TABLE traffic_daily (
    date       DATE       NOT NULL,
    store_id   VARCHAR(8) NOT NULL REFERENCES stores(store_id),
    nb_tickets INTEGER    NOT NULL CHECK (nb_tickets >= 0),
    PRIMARY KEY (date, store_id)
);

-- -------------------------------------------------------------------------
-- 9. sales_daily — TABLE DE FAITS PRINCIPALE
--    Grain : magasin x SKU x jour. ~1,36 M lignes sur 5 ans.
--
--    quantity = 0 est une VRAIE ligne (jour sans vente) : ~46 % des lignes.
--    Ne pas confondre avec une absence de ligne, qui signifie « SKU pas
--    encore lancé ».
--
--    is_rupture = 1 : rupture de stock, la vente est CENSURÉE (les ventes
--    observées sous-estiment la demande réelle). ~1,1 % des lignes.
--
--    unit_price = base_price x indice d'inflation du mois x (1 - remise).
--    promo_id = campagne active ce jour-là sur ce SKU (NULL sinon) ; en cas
--    de campagnes concurrentes, la plus prioritaire est retenue.
-- -------------------------------------------------------------------------
CREATE TABLE sales_daily (
    date       DATE       NOT NULL,
    store_id   VARCHAR(8) NOT NULL REFERENCES stores(store_id),
    sku_id     VARCHAR(8) NOT NULL REFERENCES products(sku_id),
    quantity   INTEGER    NOT NULL CHECK (quantity >= 0),
    unit_price NUMERIC(8,2) NOT NULL CHECK (unit_price > 0),
    revenue    NUMERIC(12,2) NOT NULL CHECK (revenue >= 0),  -- = quantity x unit_price
    is_rupture SMALLINT   NOT NULL CHECK (is_rupture IN (0,1)),
    promo_id   VARCHAR(8) REFERENCES promo_calendar(promo_id),
    PRIMARY KEY (date, store_id, sku_id)
);

-- -------------------------------------------------------------------------
-- 10. sales_hourly — CA et tickets par magasin x jour x heure ouvrée
--     Seules les heures d'ouverture sont présentes (pas de ligne à 3 h du
--     matin pour un magasin physique). Par construction :
--     SUM(ca) par magasin/jour == SUM(sales_daily.revenue) du même magasin/jour.
-- -------------------------------------------------------------------------
CREATE TABLE sales_hourly (
    date       DATE       NOT NULL,
    store_id   VARCHAR(8) NOT NULL REFERENCES stores(store_id),
    hour       SMALLINT   NOT NULL CHECK (hour BETWEEN 0 AND 23),
    ca         NUMERIC(10,2) NOT NULL CHECK (ca >= 0),
    nb_tickets INTEGER    NOT NULL CHECK (nb_tickets >= 0),
    PRIMARY KEY (date, store_id, hour)
);

-- -------------------------------------------------------------------------
-- Index : les axes d'analyse usuels sur la table de faits.
-- -------------------------------------------------------------------------
CREATE INDEX idx_sales_date     ON sales_daily (date);
CREATE INDEX idx_sales_store    ON sales_daily (store_id);
CREATE INDEX idx_sales_sku      ON sales_daily (sku_id);
CREATE INDEX idx_sales_promo    ON sales_daily (promo_id);
CREATE INDEX idx_traffic_date   ON traffic_daily (date);
CREATE INDEX idx_hourly_date    ON sales_hourly (date);
CREATE INDEX idx_weather_date   ON weather (date);
CREATE INDEX idx_promo_dates    ON promo_calendar (date_start, date_end);
