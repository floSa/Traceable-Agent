#!/usr/bin/env bash
# Chargement SQLite
#
# Depuis le dossier export_db/ :
#     bash chargement/sqlite.sh              # -> poc_retail.sqlite
#     bash chargement/sqlite.sh ma_base.db
#
# SQLite ne connaît pas le mot-clé CASCADE sur DROP TABLE : on l'enlève à la
# volée. Le reste du DDL passe tel quel (les types NUMERIC(p,s) / VARCHAR(n)
# sont acceptés par affinité de type). Vérifié.
#
# À noter : SQLite n'applique PAS les clés étrangères par défaut. Le PRAGMA
# ci-dessous les active pour la session de chargement ; toute session cliente
# qui veut la même garantie doit le rejouer.
set -euo pipefail

DB="${1:-poc_retail.sqlite}"
cd "$(dirname "$0")/.."   # se placer dans export_db/

command -v sqlite3 >/dev/null || { echo "sqlite3 introuvable — installez-le (apt install sqlite3)"; exit 1; }
rm -f "$DB"

# DDL, sans CASCADE
sed 's/ CASCADE;/;/' schema.sql | sqlite3 "$DB"

# Import des CSV, dans l'ordre des dépendances
for t in stores store_hours products promo_calendar promo_scope \
         inflation weather traffic_daily sales_daily sales_hourly; do
  sqlite3 "$DB" <<SQL
PRAGMA foreign_keys = ON;
.mode csv
.import --skip 1 tables/${t}.csv ${t}
SQL
  echo "  ${t} chargée"
done

echo
echo "Volumes :"
sqlite3 "$DB" "SELECT 'sales_daily', count(*) FROM sales_daily
        UNION ALL SELECT 'sales_hourly', count(*) FROM sales_hourly
        UNION ALL SELECT 'traffic_daily', count(*) FROM traffic_daily;"
echo "Attendu : 1363726 / 244952 / 23738"
echo "Base prête : $DB"
