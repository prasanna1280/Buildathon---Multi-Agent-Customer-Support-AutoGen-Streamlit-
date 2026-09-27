# Tool Connectors

## 1. `web_search`
- Provider: Serper (`google.serper.dev/search`)
- Agent: Web Search Assistant only
- Purpose: current web research
- Secret: `SERPER_API_KEY`
- Timeout: 20 seconds
- Errors are returned to the agent and logged to `logs/app.log`.

## 2. `save_support_case`
- Agent: Entry Agent only
- Purpose: persist customer query + Assistant answer + Web Search answer
- Primary file: `output/answers.txt`
- Archive: timestamped `output/answer_*.txt`
- Errors are returned to the agent and logged.

No API keys are hard-coded. Do not commit `.env`.
