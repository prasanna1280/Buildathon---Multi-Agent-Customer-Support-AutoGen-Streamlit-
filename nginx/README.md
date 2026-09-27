# Nginx + HTTPS

Nginx is the public reverse proxy. The Streamlit container is intentionally not published directly to the Internet; it is reachable only inside the Docker Compose network on port 8501.

Nginx listens on ports 80 and 443 and forwards Streamlit traffic to `supportops-ai:8501`. WebSocket upgrade headers are enabled because Streamlit uses WebSockets for its live session/streaming UI.

Before requesting a certificate:

1. Point the DNS A record for `DOMAIN` to the VPS public IP.
2. Make sure TCP 80 and 443 are allowed in Hostinger's firewall and the VPS OS firewall.
3. Set `DOMAIN` and `LETSENCRYPT_EMAIL` in `.env`.
4. Run `./scripts/issue-ssl.sh`.
