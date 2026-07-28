-- Chargement DuckDB
--
-- Depuis le dossier export_db/ :
--     duckdb poc_retail.duckdb -c ".read schema.sql" -c ".read chargement/duckdb.sql"
--
-- ou, si les Parquet ont été exportés (python -m src.export_db --format both),
-- sans créer de tables du tout — DuckDB lit les fichiers directement :
--     SELECT * FROM 'tables/sales_daily.parquet' LIMIT 5;

INSERT INTO stores         SELECT * FROM read_csv_auto('tables/stores.csv',         header=true);
INSERT INTO store_hours    SELECT * FROM read_csv_auto('tables/store_hours.csv',    header=true);
INSERT INTO products       SELECT * FROM read_csv_auto('tables/products.csv',       header=true);
INSERT INTO promo_calendar SELECT * FROM read_csv_auto('tables/promo_calendar.csv', header=true);
INSERT INTO promo_scope    SELECT * FROM read_csv_auto('tables/promo_scope.csv',    header=true);
INSERT INTO inflation      SELECT * FROM read_csv_auto('tables/inflation.csv',      header=true);
INSERT INTO weather        SELECT * FROM read_csv_auto('tables/weather.csv',        header=true);
INSERT INTO traffic_daily  SELECT * FROM read_csv_auto('tables/traffic_daily.csv',  header=true);
INSERT INTO sales_daily    SELECT * FROM read_csv_auto('tables/sales_daily.csv',    header=true);
INSERT INTO sales_hourly   SELECT * FROM read_csv_auto('tables/sales_hourly.csv',   header=true);

-- Contrôle des volumes (variante complète 5 ans).
SELECT 'sales_daily' AS t, count(*) AS n FROM sales_daily
UNION ALL SELECT 'sales_hourly', count(*) FROM sales_hourly
UNION ALL SELECT 'traffic_daily', count(*) FROM traffic_daily;
-- Attendu : 1363726 / 244952 / 23738
