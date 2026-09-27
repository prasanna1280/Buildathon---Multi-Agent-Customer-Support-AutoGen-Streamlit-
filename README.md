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
