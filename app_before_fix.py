import asyncio
import json
import logging
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests
import streamlit as st
from dotenv import load_dotenv

from autogen_agentchat.agents import AssistantAgent
from autogen_agentchat.conditions import MaxMessageTermination, TextMentionTermination
from autogen_agentchat.teams import RoundRobinGroupChat
from autogen_core import CancellationToken
from autogen_ext.models.openai import OpenAIChatCompletionClient

# -----------------------------------------------------------------------------
# Configuration
# -----------------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent
SKILLS_DIR = BASE_DIR / "skills"
TOOLS_DIR = BASE_DIR / "tools"
LOG_DIR = BASE_DIR / "logs"
OUTPUT_DIR = BASE_DIR / "output"
ANSWERS_FILE = OUTPUT_DIR / "answers.txt"

for directory in (SKILLS_DIR, TOOLS_DIR, LOG_DIR, OUTPUT_DIR):
    directory.mkdir(parents=True, exist_ok=True)

load_dotenv(BASE_DIR / ".env")

# -----------------------------------------------------------------------------
# Error logging / observability
# -----------------------------------------------------------------------------
logger = logging.getLogger("support_multi_agent")
logger.setLevel(logging.INFO)
if not logger.handlers:
    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)s | %(name)s | %(message)s"
    )
    file_handler = logging.FileHandler(LOG_DIR / "app.log", encoding="utf-8")
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)


def log_exception(context: str, exc: Exception) -> None:
    """Centralized exception logger used by UI, orchestration and tools."""
    logger.exception("%s | %s: %s", context, type(exc).__name__, exc)


def safe_text(value: Any) -> str:
    """Convert AutoGen message content into safe displayable text."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, indent=2, default=str)
    except Exception:
        return str(value)


# -----------------------------------------------------------------------------
# Skill files
# -----------------------------------------------------------------------------
DEFAULT_SKILLS = {
    "assistant.md": """# Assistant Agent Skill\n\n## Mission\nAnswer the user's query directly from the model's own knowledge.\n\n## Rules\n- Do not browse the web.\n- Do not call external tools.\n- Be concise but complete.\n- State uncertainty when the answer is not known.\n- Never invent citations, URLs, or facts.\n- This is Agent 1 in a fixed three-agent sequence.\n""",
    "web_search_assistant.md": """# Web Search Assistant Skill\n\n## Mission\nUse the approved web-search connector to research the user's query, then provide a factual answer grounded in the returned search results.\n\n## Rules\n- You are Agent 2 and have exclusive access to the web-search tool.\n- Search before answering when current/external information is needed.\n- Distinguish search-result facts from your synthesis.\n- Prefer primary/official sources when available.\n- Do not expose API keys or internal tool errors.\n- If search fails, explain the limitation and provide only a clearly qualified answer.\n""",
    "entry_agent.md": """# Entry Agent Skill\n\n## Mission\nPersist the user's query and both previous agent answers using the approved file-writing connector.\n\n## Rules\n- You are Agent 3 and have exclusive access to the file-writing tool.\n- Read the shared conversation history to obtain the original query, Agent 1 answer and Agent 2 answer.\n- Call save_support_case exactly once after collecting the three values.\n- After successful persistence, respond with a short confirmation containing the exact marker ENTRY_SAVED.\n- Do not call the web-search tool.\n- Do not modify the user's answers.\n""",
}


def ensure_skill_files() -> None:
    for filename, content in DEFAULT_SKILLS.items():
        path = SKILLS_DIR / filename
        if not path.exists():
            path.write_text(content, encoding="utf-8")


def load_skill(filename: str) -> str:
    try:
        path = SKILLS_DIR / filename
        if not path.exists():
            raise FileNotFoundError(f"Skill file not found: {path}")
        return path.read_text(encoding="utf-8")
    except Exception as exc:
        log_exception(f"load_skill({filename})", exc)
        return "Follow the role described by the application configuration."


ensure_skill_files()

# -----------------------------------------------------------------------------
# Tool connectors
# -----------------------------------------------------------------------------

def web_search(query: str, num_results: int = 5) -> str:
    """Search the web through Serper and return compact result evidence."""
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
            headers={
                "X-API-KEY": api_key,
                "Content-Type": "application/json",
            },
            json={"q": query, "num": num_results},
            timeout=20,
        )
        response.raise_for_status()
        payload = response.json()

        organic = payload.get("organic", [])[:num_results]
        if not organic:
            return "WEB_SEARCH_RESULT: No results found."

        lines = [f"Search results for: {query}"]
        for index, item in enumerate(organic, start=1):
            title = item.get("title", "Untitled")
            snippet = item.get("snippet", "")
            link = item.get("link", "")
            lines.append(f"{index}. {title}\n   {snippet}\n   URL: {link}")
        return "\n".join(lines)
    except Exception as exc:
        log_exception("web_search", exc)
        return f"WEB_SEARCH_ERROR: {type(exc).__name__}: {exc}"


def save_support_case(query: str, answer_1: str, answer_2: str) -> str:
    """Persist query + both agent answers to answers.txt and a timestamped archive."""
    try:
        query = safe_text(query).strip()
        answer_1 = safe_text(answer_1).strip()
        answer_2 = safe_text(answer_2).strip()
        if not query:
            return "SAVE_ERROR: Query is empty."

        timestamp = datetime.now().astimezone().isoformat(timespec="seconds")
        record = (
            "=" * 88
            + f"\nTimestamp: {timestamp}\n"
            + f"Query:\n{query}\n\n"
            + f"Assistant Answer:\n{answer_1}\n\n"
            + f"Web Search Answer:\n{answer_2}\n"
        )
        with ANSWERS_FILE.open("a", encoding="utf-8") as handle:
            handle.write(record)

        archive_name = OUTPUT_DIR / f"answer_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}.txt"
        archive_name.write_text(record, encoding="utf-8")
        logger.info("Support case saved: %s", archive_name.name)
        return f"SAVE_COMPLETED: {ANSWERS_FILE.name} and {archive_name.name}"
    except Exception as exc:
        log_exception("save_support_case", exc)
        return f"SAVE_ERROR: {type(exc).__name__}: {exc}"


# -----------------------------------------------------------------------------
# AutoGen orchestration
# -----------------------------------------------------------------------------

def validate_environment() -> List[str]:
    missing = []
    if not os.getenv("OPENAI_API_KEY", "").strip():
        missing.append("OPENAI_API_KEY")
    if not os.getenv("SERPER_API_KEY", "").strip():
        missing.append("SERPER_API_KEY")
    return missing


def build_team() -> tuple[RoundRobinGroupChat, OpenAIChatCompletionClient]:
    """Create exactly three AssistantAgents in the assignment's required order."""
    model = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
    client = OpenAIChatCompletionClient(
        model=model,
        api_key=os.environ["OPENAI_API_KEY"],
        parallel_tool_calls=False,
    )

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
    )

    entry_agent = AssistantAgent(
        name="Entry_Agent",
        model_client=client,
        tools=[save_support_case],
        system_message=load_skill("entry_agent.md"),
        max_tool_iterations=2,
    )

    # TextMentionTermination is the primary completion signal. The max-message
    # guard is a safety net against malformed model/tool behavior.
    termination = TextMentionTermination("ENTRY_SAVED") | MaxMessageTermination(12)
    team = RoundRobinGroupChat(
        [assistant, web_agent, entry_agent],
        termination_condition=termination,
        max_turns=3,
    )
    return team, client


def extract_messages(result: Any) -> List[Dict[str, str]]:
    """Normalize AutoGen events into displayable records.

    AutoGen can return TextMessage, ToolCallRequestEvent, ToolCallExecutionEvent
    and other event types. We retain all useful textual content instead of
    assuming every event is a normal TextMessage.
    """
    parsed: List[Dict[str, str]] = []
    for message in getattr(result, "messages", []) or []:
        source = str(getattr(message, "source", "unknown"))
        content = getattr(message, "content", "")

        # Tool execution content can occasionally be a list of tool results.
        if isinstance(content, list):
            parts = []
            for item in content:
                if isinstance(item, dict):
                    parts.append(safe_text(item.get("content", item)))
                else:
                    parts.append(safe_text(item))
            content = "\n".join(p for p in parts if p)
        else:
            content = safe_text(content)

        if content.strip():
            parsed.append({
                "source": source,
                "content": content.strip(),
                "type": type(message).__name__,
            })
    return parsed


def extract_answer(messages: List[Dict[str, str]], source_name: str) -> str:
    """Extract an agent's final text, with fallbacks for AutoGen source naming.

    Some AutoGen versions/events expose source names slightly differently.
    We first match the exact name, then case-insensitive names, and finally use
    the expected three-agent order as a defensive fallback.
    """
    exact = [m["content"] for m in messages if m["source"] == source_name]
    if exact:
        # Prefer a normal assistant/text response over tool-event payloads.
        non_tool = [
            m["content"] for m in messages
            if m["source"] == source_name
            and m.get("type", "").lower() in {"textmessage", "response", "chatmessage"}
        ]
        return (non_tool or exact)[-1]

    target = source_name.lower().replace("_", "")
    fuzzy = [
        m["content"] for m in messages
        if m["source"].lower().replace("_", "") == target
    ]
    if fuzzy:
        return fuzzy[-1]

    return "No answer was returned by this agent."


async def run_team_async(query: str) -> Dict[str, Any]:
    team = None
    client = None
    try:
        team, client = build_team()
        task = (
            "USER QUERY:\n"
            f"{query}\n\n"
            "Execute the three-agent workflow exactly once: Agent 1 answers directly, "
            "Agent 2 searches the web and answers, Agent 3 saves the query and both "
            "answers using its file-writing tool, then emits ENTRY_SAVED."
        )
        result = await team.run(task=task)
        messages = extract_messages(result)
        logger.info(
            "AutoGen completed | stop_reason=%s | message_count=%d | sources=%s",
            getattr(result, "stop_reason", "Completed"),
            len(messages),
            [m["source"] for m in messages],
        )
        assistant_answer = extract_answer(messages, "Assistant")
        web_answer = extract_answer(messages, "Web_Search_Assistant")
        entry_answer = extract_answer(messages, "Entry_Agent")

        # Defensive fallback: if a specific AutoGen release does not expose
        # the expected source names, recover the first three non-tool messages
        # in sequence rather than showing an empty UI.
        if assistant_answer == "No answer was returned by this agent." and messages:
            candidates = [m["content"] for m in messages if "tool" not in m.get("type", "").lower()]
            if len(candidates) >= 1:
                assistant_answer = candidates[0]
            if len(candidates) >= 2 and web_answer == "No answer was returned by this agent.":
                web_answer = candidates[1]
        return {
            "messages": messages,
            "assistant_answer": assistant_answer,
            "web_answer": web_answer,
            "entry_answer": entry_answer,
            "stop_reason": getattr(result, "stop_reason", "Completed"),
            "saved": "ENTRY_SAVED" in entry_answer or ANSWERS_FILE.exists(),
            "error": None,
        }
    except Exception as exc:
        log_exception("run_team_async", exc)
        return {
            "messages": [],
            "assistant_answer": "",
            "web_answer": "",
            "entry_answer": "",
            "stop_reason": "Error",
            "saved": False,
            "error": f"{type(exc).__name__}: {exc}",
        }
    finally:
        if client is not None:
            try:
                await client.close()
            except Exception as exc:
                log_exception("model_client.close", exc)


def run_team(query: str) -> Dict[str, Any]:
    return asyncio.run(run_team_async(query))


# -----------------------------------------------------------------------------
# Streamlit UI
# -----------------------------------------------------------------------------

st.set_page_config(
    page_title="SupportOps AI • Multi-Agent",
    page_icon="🤖",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.markdown(
    """
    <style>
    .stApp { background: #f7f8fb; }
    .block-container { padding-top: 1.3rem; max-width: 1450px; }
    .hero { padding: 1.4rem 1.6rem; border-radius: 18px; background: linear-gradient(135deg,#111827,#374151); color:white; margin-bottom:1rem; }
    .hero h1 { margin:0; font-size:2.1rem; }
    .hero p { margin:.45rem 0 0; color:#d1d5db; }
    .card { background:white; border:1px solid #e5e7eb; border-radius:16px; padding:1rem 1.1rem; min-height:130px; box-shadow:0 5px 18px rgba(15,23,42,.05); }
    .agent-pill { display:inline-block; padding:.25rem .55rem; border-radius:999px; background:#eef2ff; font-size:.78rem; font-weight:700; }
    .small { color:#6b7280; font-size:.86rem; }
    .success { color:#047857; font-weight:700; }
    .danger { color:#b91c1c; font-weight:700; }
    </style>
    """,
    unsafe_allow_html=True,
)

st.markdown(
    """
    <div class="hero">
      <h1>🤖 SupportOps AI</h1>
      <p>Three-agent customer support workflow • AutoGen AgentChat • Streamlit</p>
    </div>
    """,
    unsafe_allow_html=True,
)

with st.sidebar:
    st.subheader("⚙️ Control Center")
    model_name = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
    st.metric("Model", model_name)
    missing_keys = validate_environment()
    if missing_keys:
        st.error("Missing: " + ", ".join(missing_keys))
    else:
        st.success("API configuration ready")

    st.divider()
    st.caption("Agent sequence")
    st.markdown("1. 🧠 **Assistant** — no tools")
    st.markdown("2. 🌐 **Web Search Assistant** — Serper")
    st.markdown("3. 📝 **Entry Agent** — file writer")

    st.divider()
    st.caption("Artifacts")
    st.write(f"📄 `{ANSWERS_FILE.relative_to(BASE_DIR)}`")
    st.write(f"🧾 `{(LOG_DIR / 'app.log').relative_to(BASE_DIR)}`")
    if ANSWERS_FILE.exists():
        st.download_button(
            "⬇️ Download answers.txt",
            data=ANSWERS_FILE.read_bytes(),
            file_name="answers.txt",
            mime="text/plain",
            use_container_width=True,
        )

query = st.text_area(
    "Customer query / task",
    placeholder="Example: How do I reset my password?",
    height=130,
    key="query_input",
)

col_run, col_clear, col_status = st.columns([1.1, 0.8, 2.1])
with col_run:
    run_clicked = st.button("🚀 Run Multi-Agent Workflow", type="primary", use_container_width=True)
with col_clear:
    if st.button("🧹 Clear", use_container_width=True):
        st.session_state.pop("last_result", None)
        st.session_state["query_input"] = ""
        st.rerun()
with col_status:
    st.caption("Fixed sequence: Assistant → Web Search Assistant → Entry Agent")

if run_clicked:
    missing = validate_environment()
    if missing:
        st.error("Please configure: " + ", ".join(missing) + " in your .env file.")
    elif not query.strip():
        st.warning("Enter a customer query before starting the workflow.")
    elif len(query.strip()) > 5000:
        st.warning("Please keep the query below 5,000 characters.")
    else:
        with st.status("Running three-agent workflow…", expanded=True) as status:
            st.write("🧠 Assistant is preparing a direct answer…")
            st.write("🌐 Web Search Assistant will research the query…")
            st.write("📝 Entry Agent will persist the complete case…")
            result = run_team(query.strip())
            if result["error"]:
                status.update(label="Workflow failed", state="error")
                st.session_state["last_result"] = result
                st.error(result["error"])
            else:
                status.update(label="Workflow completed", state="complete")
                st.session_state["last_result"] = result

result = st.session_state.get("last_result")
if result:
    st.divider()
    if result.get("error"):
        st.error("The workflow encountered an error. Check logs/app.log for the full traceback.")
    else:
        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Agents", "3")
        m2.metric("Web Search", "Enabled")
        m3.metric("Saved", "Yes" if result.get("saved") else "No")
        m4.metric("Status", result.get("stop_reason", "Completed"))

        tab1, tab2, tab3 = st.tabs(["💬 Answers", "🔎 Execution Trace", "📁 Persistence"])
        with tab1:
            a, b = st.columns(2)
            with a:
                st.markdown("### 🧠 Assistant Answer")
                st.markdown(result.get("assistant_answer", "No answer returned."))
            with b:
                st.markdown("### 🌐 Web Search Answer")
                st.markdown(result.get("web_answer", "No web answer returned."))

        with tab2:
            st.markdown("### Agent timeline")
            for idx, message in enumerate(result.get("messages", []), start=1):
                source = message["source"]
                icon = {"Assistant": "🧠", "Web_Search_Assistant": "🌐", "Entry_Agent": "📝"}.get(source, "⚙️")
                with st.expander(f"{idx}. {icon} {source}", expanded=False):
                    st.write(message["content"])

        with tab3:
            if result.get("saved"):
                st.success(f"Saved to `{ANSWERS_FILE}`")
                if ANSWERS_FILE.exists():
                    st.download_button(
                        "⬇️ Download saved case log",
                        data=ANSWERS_FILE.read_bytes(),
                        file_name="answers.txt",
                        mime="text/plain",
                    )
            else:
                st.warning("The Entry Agent did not report a successful save.")

st.divider()
st.caption("Buildathon-aligned design: exactly three AssistantAgents, RoundRobinGroupChat, dedicated tools, termination guard, and secure environment variables.")
