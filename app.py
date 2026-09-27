import asyncio
import html
import json
import logging
import os
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests
import streamlit as st
from dotenv import load_dotenv

from autogen_agentchat.agents import AssistantAgent
from autogen_agentchat.conditions import FunctionCallTermination, MaxMessageTermination
from autogen_agentchat.teams import RoundRobinGroupChat
from autogen_ext.models.openai import OpenAIChatCompletionClient

# =============================================================================
# SupportOps AI — Advanced AutoGen + Streamlit Buildathon
# Exactly three AssistantAgents:
#   1) Assistant             -> no tools
#   2) Web Search Assistant  -> Serper tool only
#   3) Entry Agent           -> file-writing tool only
# =============================================================================

BASE_DIR = Path(__file__).resolve().parent
SKILLS_DIR = BASE_DIR / "skills"
TOOLS_DIR = BASE_DIR / "tools"
LOG_DIR = BASE_DIR / "logs"
OUTPUT_DIR = BASE_DIR / "output"
ANSWERS_FILE = OUTPUT_DIR / "answers.txt"

for directory in (SKILLS_DIR, TOOLS_DIR, LOG_DIR, OUTPUT_DIR):
    directory.mkdir(parents=True, exist_ok=True)

load_dotenv(BASE_DIR / ".env")

# =============================================================================
# Logging / error capture
# =============================================================================
logger = logging.getLogger("support_multi_agent")
logger.setLevel(logging.INFO)
if not logger.handlers:
    handler = logging.FileHandler(LOG_DIR / "app.log", encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s | %(levelname)s | %(name)s | %(message)s"))
    logger.addHandler(handler)


def log_exception(context: str, exc: Exception) -> None:
    logger.exception("%s | %s: %s", context, type(exc).__name__, exc)


def safe_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, indent=2, default=str)
    except Exception:
        return str(value)


# =============================================================================
# Skill files
# =============================================================================
DEFAULT_SKILLS = {
    "assistant.md": """# Assistant Agent Skill\n\n## Mission\nAnswer the customer's actual query directly from your own knowledge.\n\n## Tool policy\n- You have NO tools.\n- Never browse the web.\n- Never claim to have searched.\n\n## Response policy\n- Answer the user's actual question, not orchestration instructions.\n- Be concise, useful and professional.\n- State uncertainty where appropriate.\n- Do not invent citations or URLs.\n- You are Agent 1 of exactly 3 agents.\n""",
    "web_search_assistant.md": """# Web Search Assistant Skill\n\n## Mission\nResearch the customer's original query using the approved Serper connector and produce a useful answer grounded in search evidence.\n\n## Tool policy\n- You are Agent 2.\n- You have exclusive access to `web_search`.\n- Call the search tool before producing your final researched answer.\n- Do not call or imitate the file-writing tool.\n\n## Response policy\n- Use the original customer query from the shared conversation.\n- Prefer official / primary sources where possible.\n- Distinguish search evidence from synthesis.\n- If search fails, clearly disclose the limitation.\n- End with a concise natural-language answer for the customer.\n""",
    "entry_agent.md": """# Entry Agent Skill\n\n## Mission\nPersist the customer's query, Agent 1 answer and Agent 2 answer.\n\n## Tool policy\n- You are Agent 3.\n- You have exclusive access to `save_support_case`.\n- Call the save tool exactly once after collecting all three values.\n- Do not browse the web.\n\n## Required data\n1. Original customer query\n2. Assistant answer\n3. Web Search Assistant answer\n\n## Completion\nOnly after the save tool returns `SAVE_COMPLETED`, respond with `ENTRY_SAVED`.\n""",
}


def ensure_skill_files() -> None:
    for filename, content in DEFAULT_SKILLS.items():
        path = SKILLS_DIR / filename
        if not path.exists():
            path.write_text(content, encoding="utf-8")


def load_skill(filename: str) -> str:
    try:
        return (SKILLS_DIR / filename).read_text(encoding="utf-8")
    except Exception as exc:
        log_exception(f"load_skill({filename})", exc)
        return DEFAULT_SKILLS.get(filename, "Follow the configured agent role.")


ensure_skill_files()

# =============================================================================
# Tool connectors
# =============================================================================

def web_search(query: str, num_results: int = 5) -> str:
    """Search Google results through Serper. This connector belongs only to Agent 2."""
    started = time.perf_counter()
    try:
        api_key = os.getenv("SERPER_API_KEY", "").strip()
        if not api_key:
            return "WEB_SEARCH_ERROR: SERPER_API_KEY is not configured."

        query = query.strip()
        if not query:
            return "WEB_SEARCH_ERROR: Search query is empty."

        num_results = max(1, min(int(num_results), 10))
        response = requests.post(
            "https://google.serper.dev/search",
            headers={"X-API-KEY": api_key, "Content-Type": "application/json"},
            json={"q": query, "num": num_results},
            timeout=20,
        )
        response.raise_for_status()
        payload = response.json()
        organic = payload.get("organic", [])[:num_results]

        if not organic:
            return "WEB_SEARCH_RESULT: No results found."

        lines = [f"Search results for: {query}"]
        for index, item in enumerate(organic, 1):
            lines.append(
                f"{index}. {item.get('title', 'Untitled')}\n"
                f"   {item.get('snippet', '')}\n"
                f"   URL: {item.get('link', '')}"
            )
        logger.info("web_search completed | query=%r | results=%d | duration=%.2fs", query, len(organic), time.perf_counter() - started)
        return "\n".join(lines)
    except Exception as exc:
        log_exception("web_search", exc)
        return f"WEB_SEARCH_ERROR: {type(exc).__name__}: {exc}"


def save_support_case(query: str, answer_1: str, answer_2: str) -> str:
    """Persist query + both answers. This connector belongs only to Agent 3."""
    started = time.perf_counter()
    try:
        query = safe_text(query).strip()
        answer_1 = safe_text(answer_1).strip()
        answer_2 = safe_text(answer_2).strip()
        if not query:
            return "SAVE_ERROR: Query is empty."

        now = datetime.now().astimezone()
        timestamp = now.isoformat(timespec="seconds")
        record = (
            "=" * 88
            + f"\nTimestamp: {timestamp}\n"
            + f"Query:\n{query}\n\n"
            + f"Assistant Answer:\n{answer_1}\n\n"
            + f"Web Search Answer:\n{answer_2}\n"
        )

        with ANSWERS_FILE.open("a", encoding="utf-8") as handle:
            handle.write(record)

        archive = OUTPUT_DIR / f"answer_{now.strftime('%Y%m%d_%H%M%S_%f')}.txt"
        archive.write_text(record, encoding="utf-8")
        logger.info("Support case saved | archive=%s | duration=%.2fs", archive.name, time.perf_counter() - started)
        return f"SAVE_COMPLETED: {ANSWERS_FILE.name} and {archive.name}"
    except Exception as exc:
        log_exception("save_support_case", exc)
        return f"SAVE_ERROR: {type(exc).__name__}: {exc}"


# =============================================================================
# AutoGen team
# =============================================================================

def validate_environment() -> List[str]:
    missing = []
    if not os.getenv("OPENAI_API_KEY", "").strip():
        missing.append("OPENAI_API_KEY")
    if not os.getenv("SERPER_API_KEY", "").strip():
        missing.append("SERPER_API_KEY")
    return missing


def build_team() -> tuple[RoundRobinGroupChat, OpenAIChatCompletionClient]:
    model = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
    client = OpenAIChatCompletionClient(model=model, api_key=os.environ["OPENAI_API_KEY"])

    assistant = AssistantAgent(
        name="Assistant",
        model_client=client,
        system_message=load_skill("assistant.md"),
    )

    web_agent = AssistantAgent(
        name="Web_Search_Assistant",
        model_client=client,
        tools=[web_search],
        system_message=load_skill("web_search_assistant.md"),
        max_tool_iterations=2,
        reflect_on_tool_use=True,
    )

    entry_agent = AssistantAgent(
        name="Entry_Agent",
        model_client=client,
        tools=[save_support_case],
        system_message=load_skill("entry_agent.md"),
        max_tool_iterations=2,
        reflect_on_tool_use=True,
    )

    termination = FunctionCallTermination("save_support_case") | MaxMessageTermination(20)
    team = RoundRobinGroupChat(
        [assistant, web_agent, entry_agent],
        termination_condition=termination,
        max_turns=3,
    )
    return team, client


# =============================================================================
# AutoGen event normalization
# =============================================================================

def _format_event_content(content: Any) -> str:
    if isinstance(content, list):
        parts = []
        for item in content:
            name = getattr(item, "name", None)
            arguments = getattr(item, "arguments", None)
            item_content = getattr(item, "content", None)
            if name:
                parts.append(f"Tool: {name}\nArguments: {arguments or ''}")
            elif item_content is not None:
                parts.append(safe_text(item_content))
            else:
                parts.append(safe_text(item))
        return "\n".join(p for p in parts if p)
    return safe_text(content)


def normalize_event(event: Any) -> Optional[Dict[str, str]]:
    source = str(getattr(event, "source", ""))
    event_type = type(event).__name__
    content = _format_event_content(getattr(event, "content", ""))

    if not source and event_type == "TaskResult":
        return {"source": "System", "type": event_type, "content": safe_text(getattr(event, "stop_reason", "Workflow completed"))}
    if not content.strip() and event_type != "TaskResult":
        return None

    kind = "message"
    lower = event_type.lower()
    if "toolcallrequest" in lower:
        kind = "tool_request"
    elif "toolexecution" in lower or "toolresult" in lower:
        kind = "tool_result"
    elif event_type == "TaskResult":
        kind = "system"

    return {"source": source or "System", "type": event_type, "kind": kind, "content": content.strip()}


def extract_messages_from_result(result: Any) -> List[Dict[str, str]]:
    parsed = []
    for message in getattr(result, "messages", []) or []:
        item = normalize_event(message)
        if item and item.get("content"):
            parsed.append(item)
    return parsed


def extract_answer(messages: List[Dict[str, str]], source_name: str) -> str:
    target = source_name.lower().replace("_", "")
    candidates = []
    for message in messages:
        source = message.get("source", "").lower().replace("_", "")
        if source != target:
            continue
        if message.get("kind") != "message":
            continue
        content = message.get("content", "").strip()
        if content and not content.startswith("USER QUERY:"):
            candidates.append(content)
    return candidates[-1] if candidates else "No answer was returned by this agent."


# =============================================================================
# Streaming orchestration
# =============================================================================
async def run_team_streaming(query: str, live_placeholder: Any, status_box: Any) -> Dict[str, Any]:
    team = None
    client = None
    events: List[Dict[str, str]] = []
    final_result = None
    started = time.perf_counter()

    def render_live() -> None:
        if not events:
            return
        rows = []
        for event in events[-10:]:
            source = event.get("source", "System")
            kind = event.get("kind", "message")
            icon = {"Assistant": "🧠", "Web_Search_Assistant": "🌐", "Entry_Agent": "📝", "System": "⚙️"}.get(source, "⚙️")
            badge = {"message": "MESSAGE", "tool_request": "TOOL CALL", "tool_result": "TOOL RESULT", "system": "SYSTEM"}.get(kind, "EVENT")
            content = event.get("content", "").replace("<", "&lt;").replace(">", "&gt;")
            if len(content) > 420:
                content = content[:420] + "…"
            rows.append(
                f"<div class='live-row'><div class='live-icon'>{icon}</div>"
                f"<div class='live-body'><div class='live-head'><b>{source.replace('_',' ')}</b><span class='live-badge'>{badge}</span></div>"
                f"<div class='live-content'>{content}</div></div></div>"
            )
        live_placeholder.markdown("<div class='live-panel'>" + "".join(rows) + "</div>", unsafe_allow_html=True)

    try:
        team, client = build_team()
        task = query.strip()
        logger.info("Workflow started | query=%r", task)
        status_box.update(label="🔄 Agents are working…", state="running")

        async for event in team.run_stream(task=task):
            normalized = normalize_event(event)
            if normalized:
                events.append(normalized)
                source = normalized.get("source", "System")
                kind = normalized.get("kind", "message")
                if kind == "tool_request":
                    status_box.update(label=f"🔧 {source.replace('_', ' ')} is using a connector…", state="running")
                elif kind == "message":
                    status_box.update(label=f"💬 {source.replace('_', ' ')} responded…", state="running")
                elif kind == "tool_result":
                    status_box.update(label=f"✅ {source.replace('_', ' ')} connector completed…", state="running")
                render_live()

            # The final stream item is TaskResult in AutoGen AgentChat.
            if type(event).__name__ == "TaskResult":
                final_result = event

        if final_result is None:
            raise RuntimeError("AutoGen stream ended without a TaskResult.")

        # TaskResult contains the authoritative complete message history.
        messages = extract_messages_from_result(final_result)
        if not messages:
            messages = [e for e in events if e.get("source") != "System"]

        assistant_answer = extract_answer(messages, "Assistant")
        web_answer = extract_answer(messages, "Web_Search_Assistant")
        entry_answer = extract_answer(messages, "Entry_Agent")
        all_text = " ".join(m.get("content", "") for m in messages)
        saved = "SAVE_COMPLETED" in all_text or "ENTRY_SAVED" in entry_answer
        elapsed = time.perf_counter() - started

        logger.info(
            "AutoGen stream completed | stop_reason=%s | message_count=%d | duration=%.2fs | sources=%s",
            getattr(final_result, "stop_reason", "Completed"),
            len(messages), elapsed,
            [m["source"] for m in messages],
        )
        status_box.update(label="✅ Workflow completed", state="complete")
        return {
            "messages": messages,
            "live_events": events,
            "assistant_answer": assistant_answer,
            "web_answer": web_answer,
            "entry_answer": entry_answer,
            "stop_reason": getattr(final_result, "stop_reason", "Completed"),
            "saved": saved,
            "duration": elapsed,
            "error": None,
        }
    except Exception as exc:
        log_exception("run_team_streaming", exc)
        status_box.update(label="❌ Workflow failed", state="error")
        return {
            "messages": [], "live_events": events, "assistant_answer": "", "web_answer": "",
            "entry_answer": "", "stop_reason": "Error", "saved": False,
            "duration": time.perf_counter() - started, "error": f"{type(exc).__name__}: {exc}",
        }
    finally:
        if client is not None:
            try:
                await client.close()
            except Exception as exc:
                log_exception("model_client.close", exc)


def run_team_streaming_sync(query: str, live_placeholder: Any, status_box: Any) -> Dict[str, Any]:
    return asyncio.run(run_team_streaming(query, live_placeholder, status_box))


# =============================================================================
# Streamlit UI
# =============================================================================
st.set_page_config(
    page_title="SupportOps AI | Multi-Agent Support",
    page_icon="🤖",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.markdown(
    """
<style>
:root { --ink:#111827; --muted:#64748b; --line:#e5e7eb; --panel:#ffffff; --brand:#ef4444; --brand2:#f97316; --navy:#111827; }
.stApp { background:linear-gradient(180deg,#f8fafc 0%,#f1f5f9 100%); }
.block-container { max-width:1500px; padding:1rem 2rem 1.25rem; }
section[data-testid="stSidebar"] { background:linear-gradient(180deg,#0f172a 0%,#172033 100%); }
section[data-testid="stSidebar"] * { color:#e5e7eb; }
section[data-testid="stSidebar"] .stMetric label { color:#94a3b8 !important; }
section[data-testid="stSidebar"] code { color:#0f172a !important; background:#f8fafc !important; border:1px solid #cbd5e1 !important; font-weight:700 !important; opacity:1 !important; }
section[data-testid="stSidebar"] .sidebar-agent-card { background:rgba(255,255,255,.06); border:1px solid rgba(255,255,255,.10); border-radius:12px; padding:.65rem .7rem; margin:.45rem 0; }
section[data-testid="stSidebar"] .sidebar-agent-title { color:#f8fafc !important; font-weight:800; font-size:.92rem; }
section[data-testid="stSidebar"] .sidebar-agent-sub { display:inline-block; margin-top:.3rem; padding:.18rem .45rem; border-radius:6px; background:#f8fafc; color:#0f172a !important; font-size:.70rem; font-weight:750; }
section[data-testid="stSidebar"] .sidebar-connector { color:#e2e8f0 !important; font-size:.86rem; margin:.55rem 0; }
section[data-testid="stSidebar"] div[data-testid="stDownloadButton"] button { background:#ffffff !important; color:#0f172a !important; border:1px solid #cbd5e1 !important; border-radius:10px !important; font-weight:800 !important; opacity:1 !important; min-height:40px !important; box-shadow:0 3px 10px rgba(0,0,0,.14) !important; }
section[data-testid="stSidebar"] div[data-testid="stDownloadButton"] button:hover { background:#f8fafc !important; color:#0b1220 !important; border-color:#94a3b8 !important; }
section[data-testid="stSidebar"] div[data-testid="stDownloadButton"] button p, section[data-testid="stSidebar"] div[data-testid="stDownloadButton"] button span, section[data-testid="stSidebar"] div[data-testid="stDownloadButton"] button div { color:#0f172a !important; opacity:1 !important; }
.hero { position:relative; overflow:hidden; padding:1.8rem 2rem; border-radius:24px; background:radial-gradient(circle at 85% 20%,rgba(239,68,68,.38),transparent 30%),linear-gradient(135deg,#0b1220,#25334d 70%,#334155); color:white; box-shadow:0 18px 45px rgba(15,23,42,.18); }
.hero:after { content:''; position:absolute; width:180px;height:180px;border-radius:50%;right:-55px;bottom:-85px;background:rgba(255,255,255,.06); }
.hero h1 { margin:0; font-size:2.35rem; letter-spacing:-.04em; }
.hero p { margin:.5rem 0 0; color:#cbd5e1; font-size:1rem; }
.hero-badges { display:flex; gap:.5rem; margin-top:1rem; flex-wrap:wrap; }
.hero-badge { border:1px solid rgba(255,255,255,.16); background:rgba(255,255,255,.08); border-radius:999px; padding:.32rem .65rem; font-size:.78rem; }
.section-title { font-size:1.02rem; font-weight:800; color:#111827 !important; margin:.15rem 0 .45rem; }
.workflow { display:grid; grid-template-columns:1fr 42px 1fr 42px 1fr; gap:.45rem; align-items:center; margin:.45rem 0 .75rem; }
.step { background:#fff; border:1px solid #dbe2ea; border-radius:15px; padding:.7rem .85rem; box-shadow:0 4px 14px rgba(15,23,42,.05); }
.step-num { font-size:.70rem; color:#64748b !important; font-weight:800; }
.step-name { color:#111827 !important; font-weight:800; font-size:.95rem; margin-top:.12rem; }
.step-sub { color:#475569 !important; font-size:.76rem; margin-top:.15rem; }
.arrow { text-align:center; color:#94a3b8 !important; font-size:1.25rem; font-weight:700; }
.query-card { background:#fff; border:1px solid var(--line); border-radius:18px; padding:.75rem .9rem .45rem; box-shadow:0 6px 20px rgba(15,23,42,.05); }
/* Main-page text contrast */
.stApp [data-testid="stMetricLabel"] {
    color:#64748b !important;
}
.stApp [data-testid="stMetricValue"] {
    color:#111827 !important;
}
.query-card [data-testid="stCaptionContainer"],
.query-card [data-testid="stCaptionContainer"] * {
    color:#64748b !important;
}
.stApp [data-testid="stStatusWidget"],
.stApp [data-testid="stStatusWidget"] * {
    color:#111827 !important;
}

/* Customer query textarea — explicit readable colors for Streamlit/BaseWeb */
div[data-testid="stTextArea"] textarea,
div[data-testid="stTextArea"] [data-baseweb="textarea"] textarea {
    background-color:#ffffff !important;
    color:#111827 !important;
    -webkit-text-fill-color:#111827 !important;
    caret-color:#111827 !important;
    border:1px solid #cbd5e1 !important;
    border-radius:14px !important;
    font-size:1rem !important;
    font-weight:500 !important;
    opacity:1 !important;
}

div[data-testid="stTextArea"] [data-baseweb="base-input"],
div[data-testid="stTextArea"] [data-baseweb="textarea"] {
    background-color:#ffffff !important;
}

div[data-testid="stTextArea"] textarea::placeholder,
div[data-testid="stTextArea"] [data-baseweb="textarea"] textarea::placeholder {
    color:#64748b !important;
    -webkit-text-fill-color:#64748b !important;
    opacity:1 !important;
}

div[data-testid="stTextArea"] textarea::selection {
    background-color:#bfdbfe !important;
    color:#111827 !important;
    -webkit-text-fill-color:#111827 !important;
}
div.stButton > button { border-radius:12px; font-weight:750; min-height:44px; }
.metric-card { background:#fff; border:1px solid var(--line); border-radius:16px; padding:.8rem 1rem; box-shadow:0 5px 18px rgba(15,23,42,.04); }
.answer-card { background:#fff; border:1px solid var(--line); border-radius:18px; overflow:hidden; min-height:220px; height:auto; box-shadow:0 10px 28px rgba(15,23,42,.07); }
.answer-head { display:flex; align-items:center; justify-content:space-between; gap:.8rem; padding:.9rem 1rem; border-bottom:1px solid #eef2f7; background:linear-gradient(180deg,#ffffff 0%,#f8fafc 100%); }
.answer-title-wrap { display:flex; align-items:center; gap:.55rem; }
.answer-icon { width:34px; height:34px; display:flex; align-items:center; justify-content:center; border-radius:10px; font-size:1.05rem; }
.answer-icon.direct { background:#fce7f3; }
.answer-icon.web { background:#dbeafe; }
.answer-agent { font-size:.72rem; color:#64748b; font-weight:700; margin-top:.1rem; }
.answer-chip { font-size:.63rem; font-weight:800; letter-spacing:.04em; padding:.25rem .45rem; border-radius:999px; }
.answer-chip.direct { color:#be185d; background:#fce7f3; }
.answer-chip.web { color:#1d4ed8; background:#dbeafe; }
.answer-content { height:auto; max-height:none; overflow:visible; padding:1.05rem 1.1rem 1.25rem; color:#334155; line-height:1.7; font-size:.93rem; white-space:pre-wrap; word-break:break-word; }
.answer-card.web { border-top:4px solid #3b82f6; }
.answer-card.direct { border-top:4px solid #ec4899; }
.answer-label { font-weight:850; font-size:.98rem; color:var(--ink); }
.answer-body { color:#334155; line-height:1.65; }
.live-panel { background:linear-gradient(180deg,#0b1220 0%,#111827 100%); border:1px solid #1f2937; border-radius:16px; padding:.35rem .55rem; height:310px; overflow-y:auto; overflow-x:hidden; box-shadow:0 12px 30px rgba(15,23,42,.13); scrollbar-width:thin; scrollbar-color:#475569 #0b1220; }
.live-panel::-webkit-scrollbar { width:7px; }
.live-panel::-webkit-scrollbar-track { background:#0b1220; border-radius:8px; }
.live-panel::-webkit-scrollbar-thumb { background:#475569; border-radius:8px; }
.live-panel::-webkit-scrollbar-thumb:hover { background:#64748b; }
.live-row { display:flex; gap:.65rem; padding:.55rem .5rem; border-bottom:1px solid rgba(255,255,255,.07); }
.live-row:first-child { padding-top:.45rem; }
.live-row:last-child { border-bottom:0; }
.live-icon { font-size:1rem; width:24px; flex:0 0 24px; }
.live-body { flex:1; min-width:0; }
.live-head { color:#f8fafc; display:flex; gap:.45rem; align-items:center; font-size:.76rem; }
.live-badge { font-size:.56rem; letter-spacing:.06em; padding:.18rem .38rem; border-radius:999px; background:rgba(148,163,184,.18); color:#cbd5e1; }
.live-content { margin-top:.2rem; color:#a8b3c7; font-size:.70rem; white-space:pre-wrap; word-break:break-word; }
.artifact-card { display:flex; align-items:center; gap:.55rem; padding:.55rem .65rem; margin:.35rem 0; border:1px solid rgba(148,163,184,.18); background:rgba(255,255,255,.055); border-radius:9px; }
.artifact-icon { font-size:.9rem; width:22px; text-align:center; }
.artifact-name { color:#e2e8f0 !important; font-size:.76rem; font-weight:700; overflow-wrap:anywhere; }
.artifact-meta { color:#94a3b8 !important; font-size:.62rem; margin-top:.08rem; }
.footer { color:#94a3b8; font-size:.78rem; text-align:center; padding:1.2rem 0 .2rem; }
@media (max-width: 900px) { .workflow { grid-template-columns:1fr; } .arrow { transform:rotate(90deg); } }
</style>
""",
    unsafe_allow_html=True,
)

# Session defaults
for key, default in {
    "query_input": "",
    "last_result": None,
    "run_count": 0,
}.items():
    if key not in st.session_state:
        st.session_state[key] = default

# Sidebar
with st.sidebar:
    st.markdown("## 🎛️ Control Center")
    model_name = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
    st.metric("Model", model_name)
    missing_keys = validate_environment()
    if missing_keys:
        st.error("Missing: " + ", ".join(missing_keys))
    else:
        st.success("● API configuration ready")

    st.divider()
    st.caption("AGENT PIPELINE")
    st.markdown("""
    <div class="sidebar-agent-card">
      <div class="sidebar-agent-title">🧠 01 — Assistant</div>
      <div class="sidebar-agent-sub">No tools</div>
    </div>
    <div class="sidebar-agent-card">
      <div class="sidebar-agent-title">🌐 02 — Web Search</div>
      <div class="sidebar-agent-sub">Serper connector</div>
    </div>
    <div class="sidebar-agent-card">
      <div class="sidebar-agent-title">📝 03 — Entry Agent</div>
      <div class="sidebar-agent-sub">File connector</div>
    </div>
    """, unsafe_allow_html=True)

    st.divider()
    st.caption("CONNECTORS")
    st.markdown('<div class="sidebar-connector">🔎 &nbsp;Serper web search</div>', unsafe_allow_html=True)
    st.markdown('<div class="sidebar-connector">💾 &nbsp;Local case persistence</div>', unsafe_allow_html=True)
    st.markdown('<div class="sidebar-connector">📋 &nbsp;Skill files</div>', unsafe_allow_html=True)

    st.divider()
    st.caption("ARTIFACTS")
    st.markdown(
        '<div class="artifact-card"><div class="artifact-icon">📄</div><div><div class="artifact-name">answers.txt</div><div class="artifact-meta">output / saved cases</div></div></div>',
        unsafe_allow_html=True,
    )
    st.markdown(
        '<div class="artifact-card"><div class="artifact-icon">🪵</div><div><div class="artifact-name">app.log</div><div class="artifact-meta">logs / diagnostics</div></div></div>',
        unsafe_allow_html=True,
    )
    st.markdown(
        '<div class="artifact-card"><div class="artifact-icon">🧩</div><div><div class="artifact-name">skills/*.md</div><div class="artifact-meta">agent skill definitions</div></div></div>',
        unsafe_allow_html=True,
    )
    # Keep the download control visible even before the first case is saved.
    answers_available = ANSWERS_FILE.exists()
    st.download_button(
        "⬇️ Download answers.txt",
        data=ANSWERS_FILE.read_bytes() if answers_available else b"",
        file_name="answers.txt",
        mime="text/plain",
        use_container_width=True,
        disabled=not answers_available,
        help="Run a workflow first to create answers.txt." if not answers_available else "Download the saved customer-support cases.",
    )

# Header
st.markdown(
    """
<div class="hero">
  <h1>🤖 SupportOps AI</h1>
  <p>Real-time multi-agent customer support orchestration</p>
  <div class="hero-badges">
    <span class="hero-badge">AutoGen AgentChat</span>
    <span class="hero-badge">RoundRobinGroupChat</span>
    <span class="hero-badge">Live event streaming</span>
    <span class="hero-badge">Secure environment keys</span>
  </div>
</div>
""",
    unsafe_allow_html=True,
)

# Workflow visual
st.markdown("<div class='section-title'>Agent workflow</div>", unsafe_allow_html=True)
st.markdown(
    """
<div class="workflow">
  <div class="step"><div class="step-num">01</div><div class="step-name">🧠 Assistant</div><div class="step-sub">Direct knowledge • no tools</div></div>
  <div class="arrow">→</div>
  <div class="step"><div class="step-num">02</div><div class="step-name">🌐 Web Search Assistant</div><div class="step-sub">Serper research • synthesis</div></div>
  <div class="arrow">→</div>
  <div class="step"><div class="step-num">03</div><div class="step-name">📝 Entry Agent</div><div class="step-sub">Save case • confirm</div></div>
</div>
""",
    unsafe_allow_html=True,
)

# Query area
st.markdown("<div class='query-card'>", unsafe_allow_html=True)
st.markdown("<div class='section-title'>Customer query / task</div>", unsafe_allow_html=True)
query = st.text_area(
    "",
    placeholder="Ask a customer-support question… e.g. How do I reset my password?",
    height=100,
    key="query_input",
    label_visibility="collapsed",
)
col_run, col_clear, col_hint = st.columns([1.05, .72, 2.25])

def clear_workflow_state() -> None:
    st.session_state["query_input"] = ""
    st.session_state["last_result"] = None

with col_run:
    run_clicked = st.button("🚀 Run Multi-Agent Workflow", type="primary", use_container_width=True)
with col_clear:
    st.button("🧹 Clear", use_container_width=True, on_click=clear_workflow_state)
with col_hint:
    st.caption("Sequential execution: Assistant → Web Search → Entry Agent")
st.markdown("</div>", unsafe_allow_html=True)

if run_clicked:
    missing = validate_environment()
    cleaned_query = query.strip()
    if missing:
        st.error("Please configure: " + ", ".join(missing) + " in your .env file.")
    elif not cleaned_query:
        st.warning("Enter a customer query before starting the workflow.")
    elif len(cleaned_query) > 5000:
        st.warning("Please keep the query below 5,000 characters.")
    else:
        st.session_state["run_count"] += 1
        st.markdown("<div class='section-title'>Live agent activity</div>", unsafe_allow_html=True)
        live_placeholder = st.empty()
        with st.status("🚀 Starting multi-agent workflow…", expanded=True) as workflow_status:
            st.write("Initializing the three-agent pipeline…")
            result = run_team_streaming_sync(cleaned_query, live_placeholder, workflow_status)
        st.session_state["last_result"] = result
        # The sidebar is rendered before the workflow executes. Rerun once so the
        # newly-created answers.txt appears in the sidebar download control.
        st.rerun()

result = st.session_state.get("last_result")
if result:
    st.divider()
    if result.get("error"):
        st.error("The workflow encountered an error. Check `logs/app.log` for the full traceback.")
        with st.expander("Technical error details"):
            st.code(result["error"], language="text")
    else:
        # KPI strip
        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Agents", "3")
        m2.metric("Web Search", "Enabled")
        m3.metric("Persistence", "Saved" if result.get("saved") else "Pending")
        m4.metric("Duration", f"{result.get('duration', 0):.1f}s")

        tab1, tab2, tab3 = st.tabs(["💬 Answers", "⚡ Live Trace", "📁 Persistence"])
        with tab1:
            a, b = st.columns(2, gap="large")
            assistant_text = html.escape(result.get("assistant_answer", "No answer returned."))
            web_text = html.escape(result.get("web_answer", "No web answer returned."))
            with a:
                st.markdown(
                    f"""<div class="answer-card direct">
<div class="answer-head"><div class="answer-title-wrap"><div class="answer-icon direct">🧠</div><div><div class="answer-label">Assistant Answer</div><div class="answer-agent">Agent 01 · Direct knowledge</div></div></div><span class="answer-chip direct">NO TOOLS</span></div>
<div class="answer-content">{assistant_text}</div>
</div>""",
                    unsafe_allow_html=True,
                )
            with b:
                st.markdown(
                    f"""<div class="answer-card web">
<div class="answer-head"><div class="answer-title-wrap"><div class="answer-icon web">🌐</div><div><div class="answer-label">Web Search Answer</div><div class="answer-agent">Agent 02 · Serper research</div></div></div><span class="answer-chip web">WEB SEARCH</span></div>
<div class="answer-content">{web_text}</div>
</div>""",
                    unsafe_allow_html=True,
                )

        with tab2:
            st.markdown("### ⚡ Execution timeline")
            for idx, message in enumerate(result.get("messages", []), start=1):
                source = message.get("source", "System")
                icon = {"Assistant": "🧠", "Web_Search_Assistant": "🌐", "Entry_Agent": "📝", "System": "⚙️"}.get(source, "⚙️")
                kind = message.get("kind", "message").replace("_", " ").title()
                with st.expander(f"{idx:02d}  {icon} {source.replace('_',' ')}  ·  {kind}", expanded=False):
                    st.write(message.get("content", ""))

        with tab3:
            if result.get("saved"):
                st.success(f"Case persisted successfully → `{ANSWERS_FILE.relative_to(BASE_DIR)}`")
                st.caption("The Entry Agent wrote the customer query and both agent answers to the case log.")
                if ANSWERS_FILE.exists():
                    st.download_button(
                        "⬇️ Download saved case log",
                        data=ANSWERS_FILE.read_bytes(),
                        file_name="answers.txt",
                        mime="text/plain",
                    )
            else:
                st.warning("The Entry Agent did not report a successful save.")

st.markdown(
    "<div class='footer'>SupportOps AI · Buildathon-aligned three-agent architecture · Live AutoGen events · Dedicated skills & connectors · Centralized error logging</div>",
    unsafe_allow_html=True,
)
