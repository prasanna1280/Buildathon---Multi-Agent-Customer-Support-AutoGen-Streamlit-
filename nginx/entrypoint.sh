#!/bin/sh

set -eu

: "${DOMAIN:?DOMAIN must be set in the environment}"

cat > /etc/nginx/conf.d/default.conf <<EOF_NGINX

map \$http_upgrade \$connection_upgrade {
    default upgrade;
    '' close;
}

server {

    listen 80;
    listen [::]:80;

    server_name ${DOMAIN};

    location / {

        proxy_pass http://supportops-ai:8501;

        proxy_http_version 1.1;

        proxy_set_header Upgrade \$http_upgrade;
        proxy_set_header Connection \$connection_upgrade;

        proxy_set_header Host \$host;

        proxy_set_header X-Real-IP \$remote_addr;

        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;

        proxy_set_header X-Forwarded-Proto \$http_x_forwarded_proto;

        proxy_read_timeout 86400;
        proxy_send_timeout 86400;

        proxy_buffering off;
    }

    location = /healthz {

        access_log off;

        return 200 'OK';

        add_header Content-Type text/plain;
    }
}

EOF_NGINX

exec nginx -g 'daemon off;'