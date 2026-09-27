#!/bin/sh
set -eu

if [ ! -f .env ]; then
  echo "ERROR: .env not found. Copy .env.example to .env and set DOMAIN and LETSENCRYPT_EMAIL."
  exit 1
fi

set -a
. ./.env
set +a

: "${DOMAIN:?DOMAIN is required in .env}"
: "${LETSENCRYPT_EMAIL:?LETSENCRYPT_EMAIL is required in .env}"

echo "Starting application and HTTP reverse proxy..."
docker compose up -d --build supportops-ai nginx

echo "Requesting Let's Encrypt certificate for ${DOMAIN}..."
docker compose --profile ssl run --rm certbot

echo "Reloading Nginx with HTTPS configuration..."
docker compose restart nginx

echo "HTTPS setup complete: https://${DOMAIN}"
