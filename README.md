# SupportOps AI — AutoGen Multi-Agent Customer Support

Advanced Streamlit implementation of the Social Eagle weekly buildathon.

## Agent architecture

Exactly three `AssistantAgent` instances run sequentially with `RoundRobinGroupChat`:

1. **Assistant** — direct answer, no tools
2. **Web Search Assistant** — exclusive Serper web-search connector
3. **Entry Agent** — exclusive file-writing connector

The Entry Agent persists the query and both answers. The team terminates on the actual `save_support_case` function call or a safety message limit.

## Advanced UI

- Live AutoGen event streaming with `team.run_stream()`
- Agent/tool activity timeline
- Workflow stepper
- KPI cards and execution duration
- Separate direct-answer and web-research answer cards
- Persistence/download area
- Centralized exception logging
- Skill files for each agent
- Dedicated connector documentation
- Secure `.env` configuration

## Run

```powershell
python -m venv venv
venv\\Scripts\\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env
streamlit run app.py
```

Set `OPENAI_API_KEY`, `SERPER_API_KEY`, and optionally `OPENAI_MODEL` in `.env`.

## Docker + Hostinger VPS + HTTPS

The project includes a production-style Docker deployment with Nginx as the HTTPS reverse proxy.

### Architecture

```text
Internet
   |
 HTTPS :443
   |
 Nginx container
   |
 Docker network
   |
 Streamlit / AutoGen container :8501
```

The Streamlit port `8501` is **not exposed publicly**. Only Nginx publishes ports `80` and `443`.

### Hostinger Web Console deployment

On the VPS Web Console:

```bash
git clone <YOUR_GITHUB_REPO_URL>
cd buildathon-support-autogen
cp .env.example .env
nano .env
```

Set:

```env
OPENAI_API_KEY=...
SERPER_API_KEY=...
OPENAI_MODEL=gpt-4o-mini
DOMAIN=supportops.yourdomain.com
LETSENCRYPT_EMAIL=your-email@example.com
```

Make sure the DNS `A` record for the domain points to the VPS public IP and allow TCP ports `80` and `443` in Hostinger/VPS firewalls.

Build and start the application and Nginx:

```bash
docker compose up -d --build supportops-ai nginx
```

Request the Let's Encrypt certificate:

```bash
./scripts/issue-ssl.sh
```

The application is then available at:

```text
https://supportops.yourdomain.com
```

### Check the deployment

```bash
docker compose ps
docker compose logs -f supportops-ai
docker compose logs -f nginx
```

### Renew the certificate

Run periodically (for example from cron):

```bash
./scripts/renew-ssl.sh
```

### Updating the application

After pushing new code to GitHub:

```bash
git pull
docker compose up -d --build supportops-ai nginx
```

The `logs/` and `output/` folders are mounted as persistent Docker volumes so application logs and saved support cases remain outside the container image.
