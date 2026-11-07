#!/bin/bash
# Runs once, on first start of an empty data volume.
set -euo pipefail

psql -v ON_ERROR_STOP=1 \
     --username "$POSTGRES_USER" \
     --dbname "$POSTGRES_DB" \
     -f /docker-entrypoint-initdb.d/sql/10-extensions.sql \
     -f /docker-entrypoint-initdb.d/sql/20-roles-schemas.sql
