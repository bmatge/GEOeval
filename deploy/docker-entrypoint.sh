#!/bin/sh
# Entrypoint du service web : attend PostgreSQL puis démarre uvicorn sur :3000.
#
# Lot 1.4 (ADR-088) : le schéma n'est PLUS géré ici. Le service `migrate`
# (python -m scripts.migrate : alembic upgrade head + seed) s'exécute avant, et
# web / worker en dépendent (service_completed_successfully). Plus de course entre
# réplicas, et un échec de migration arrête le déploiement au lieu de laisser un
# web démarrer sur un schéma incomplet. /readyz répond 503 tant que le schéma
# n'est pas là.
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

echo "[entrypoint] démarrage uvicorn sur 0.0.0.0:3000"
# --proxy-headers : derrière Traefik, X-Forwarded-Proto=https doit être honoré
# (cookies Secure + redirect_uri OIDC en https — ADR-086).
# --no-access-log : le journal d'accès est émis par le middleware (JSON, request_id).
exec uvicorn geoeval.web.app:app --host 0.0.0.0 --port 3000 \
    --proxy-headers --forwarded-allow-ips='*' --no-access-log
