#!/bin/sh
set -eu

if [ ! -f .env ]; then
  echo "ERROR: .env not found."
  exit 1
fi

set -a
. ./.env
set +a

: "${DOMAIN:?DOMAIN is required in .env}"

# Renew only when needed. Then restart Nginx so it loads a renewed certificate.
docker compose --profile ssl run --rm certbot renew --webroot --webroot-path=/var/www/certbot
docker compose restart nginx
