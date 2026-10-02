FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1

# psql + pg_isready pour l'entrypoint (attente DB + geoeval/db/seed.sql)
RUN apt-get update \
    && apt-get install -y --no-install-recommends postgresql-client \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
RUN chmod +x deploy/docker-entrypoint.sh

EXPOSE 3000
CMD ["./deploy/docker-entrypoint.sh"]
