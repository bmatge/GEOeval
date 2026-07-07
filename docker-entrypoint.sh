#!/bin/sh
# Entrypoint conteneur : attend PostgreSQL, crée le schéma, applique la seed
# (idempotente, ON CONFLICT DO NOTHING) puis démarre l'UI web sur :3000.
set -e

: "${PGHOST:=db}"
: "${PGUSER:=geoeval}"
: "${PGPASSWORD:=geoeval}"
: "${PGDATABASE:=geoeval}"
export PGHOST PGUSER PGPASSWORD PGDATABASE

echo "[entrypoint] attente de PostgreSQL ($PGHOST)..."
until pg_isready -q; do
    sleep 1
done

echo "[entrypoint] création du schéma (init_db.py)..."
python init_db.py

echo "[entrypoint] migrations idempotentes (migrations.sql)..."
psql -v ON_ERROR_STOP=1 -q -f migrations.sql

echo "[entrypoint] seed idempotente (seed.sql)..."
psql -v ON_ERROR_STOP=1 -q -f seed.sql

echo "[entrypoint] démarrage uvicorn sur 0.0.0.0:3000"
exec uvicorn webapp.app:app --host 0.0.0.0 --port 3000
