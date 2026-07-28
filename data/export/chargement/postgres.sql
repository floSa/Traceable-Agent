-- Chargement PostgreSQL
--
-- Depuis le dossier export_db/ :
--     createdb poc_retail
--     psql -d poc_retail -f schema.sql
--     psql -d poc_retail -f chargement/postgres.sql
--
-- \copy (et non COPY) : la lecture se fait côté CLIENT, donc les chemins sont
-- relatifs au dossier depuis lequel psql est lancé — pas besoin que le serveur
-- ait accès aux fichiers, ce qui marche aussi sur une base distante.
--
-- En CSV, un champ vide non quoté est lu comme NULL par défaut : c'est ce qu'on
-- veut pour launch_date, promo_calendar.store_id, sales_daily.promo_id et les
-- coordonnées du magasin ONLINE.

\copy stores         FROM 'tables/stores.csv'         WITH (FORMAT csv, HEADER true)
\copy store_hours    FROM 'tables/store_hours.csv'    WITH (FORMAT csv, HEADER true)
\copy products       FROM 'tables/products.csv'       WITH (FORMAT csv, HEADER true)
\copy promo_calendar FROM 'tables/promo_calendar.csv' WITH (FORMAT csv, HEADER true)
\copy promo_scope    FROM 'tables/promo_scope.csv'    WITH (FORMAT csv, HEADER true)
\copy inflation      FROM 'tables/inflation.csv'      WITH (FORMAT csv, HEADER true)
\copy weather        FROM 'tables/weather.csv'        WITH (FORMAT csv, HEADER true)
\copy traffic_daily  FROM 'tables/traffic_daily.csv'  WITH (FORMAT csv, HEADER true)
\copy sales_daily    FROM 'tables/sales_daily.csv'    WITH (FORMAT csv, HEADER true)
\copy sales_hourly   FROM 'tables/sales_hourly.csv'   WITH (FORMAT csv, HEADER true)

ANALYZE;

-- Contrôle : les volumes attendus (variante complète 5 ans).
SELECT 'stores' t, count(*) n FROM stores
UNION ALL SELECT 'store_hours',    count(*) FROM store_hours
UNION ALL SELECT 'products',       count(*) FROM products
UNION ALL SELECT 'promo_calendar', count(*) FROM promo_calendar
UNION ALL SELECT 'promo_scope',    count(*) FROM promo_scope
UNION ALL SELECT 'inflation',      count(*) FROM inflation
UNION ALL SELECT 'weather',        count(*) FROM weather
UNION ALL SELECT 'traffic_daily',  count(*) FROM traffic_daily
UNION ALL SELECT 'sales_daily',    count(*) FROM sales_daily
UNION ALL SELECT 'sales_hourly',   count(*) FROM sales_hourly
ORDER BY 1;
-- Attendu : 13 / 91 / 60 / 56 / 349 / 66 / 26130 / 23738 / 1363726 / 244952
