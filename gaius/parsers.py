"""gaius session-format adapters.

Each supported coding/inference agent records transcripts in its own layout;
this module turns each into the gaius "event" dicts the staging pipeline
consumes. To add support for a new agent, add a parse_*_events() here and wire
it into gaius._core.cmd_retire (dispatch + auto-scan) and the COMMANDS table.

Shared scoring/config (MODEL_INFO, credential/noise filters, content_hash,
_is_noise) lives in gaius._core; this module imports it. gaius._core re-imports
the names defined here at module end, so the public contract
`from gaius._core import parse_grok_events` (etc.) is preserved.
"""
import hashlib
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from gaius._core import (
    MODEL_INFO,
    content_hash,
    _is_noise,
    CREDENTIAL_PATTERNS,
    GEMINI_NOISE_SUBJECTS,
    GEMINI_CREDENTIAL_PATTERNS,
)

PEER_AGENT_MIN_RESPONSE = 40  # drop trivial assistant closers/narration

# Codex injects environment/instruction context as synthetic 'user' messages;
# these start with these markers and must not be treated as operator queries.
_CODEX_CONTEXT_MARKERS = ("<environment_context", "<permissions", "# AGENTS.md", "<INSTRUCTIONS")


def detect_format(path: Path) -> str:
    """Return 'claude' for .jsonl, 'gemini' for .json, None to skip."""
    if path.suffix == '.jsonl':
        return 'claude'
    if path.suffix == '.json':
        return 'gemini'
    return None


def parse_claude_events(path: Path) -> list[dict]:
    """Extract high-signal facts from compact summaries and significant tool-result pairs."""
    events = []
    summaries = []
    try:
        with open(path) as f:
            for line in f:
                entry = json.loads(line)
                # Capture existing compact summaries first (highest signal)
                if entry.get("isCompactSummary"):
                    text = entry.get("message", {}).get("content", "")
                    if text:
                        # Split by section headers to create individual facts
                        sections = re.split(r'\n\d+\.\s+[A-Z][^\n]*\n', text)
                        for s in sections:
                            if len(s.strip()) > 100:
                                summaries.append(s.strip())
                
                # Capture significant tool results
                if entry.get("type") == "user":
                    msg_content = entry.get("message", {}).get("content", [])
                    if isinstance(msg_content, list):
                        for item in msg_content:
                            if item.get("type") == "tool_result" and not item.get("is_error"):
                                res_text = str(item.get("content"))
                                if len(res_text) > 500 and len(res_text) < 5000:
                                    events.append({
                                        "signal": "Observed cluster state/output",
                                        "source": f"tool_result({item.get('tool_use_id')})",
                                        "outcome": res_text[:2000],
                                        "source_type": "claude",
                                        "verification_cmd": ""
                                    })
    except Exception:
        pass
    
    # Map summaries to event format
    for s in summaries:
        events.append({
            "signal": s[:500],
            "source": "CompactSummary",
            "outcome": s,
            "source_type": "claude",
            "verification_cmd": ""
        })
        
    # Strict deduplication and boilerplate filtering
    high_signal = []
    seen_hashes = set()
    for ev in events:
        ignore = ["I will read", "I will list", "Checking file", "Searching for"]
        if any(p in ev["signal"] for p in ignore):
            continue
        # Apply noise filter
        if _is_noise(ev["signal"]):
            continue

        h = hashlib.sha256(ev["outcome"].encode()).hexdigest()
        if h in seen_hashes:
            continue
        seen_hashes.add(h)
        high_signal.append(ev)

    return high_signal


def parse_gemini_events(path: Path) -> list[dict]:
    """Parse a cold Gemini CLI session JSON into typed events.

    Returns list of events with keys:
      type: 'decision' | 'discovery' | 'response'
      provenance: 'structured_reasoning' | 'automated'
      agent: 'gemini'
      session_uuid: str (from sessionId)
      timestamp: str (ISO, approximated from file mtime)
      subject: str (for decision)
      description: str (for decision)
      tool: str (for discovery)
      args: dict (for discovery)
      output: str | None (for discovery)
    """
    try:
        with open(path) as f:
            data = json.load(f)
    except Exception as e:
        print(f"  warning: failed to parse {path.name}: {e}", file=sys.stderr)
        return []

    if not isinstance(data, dict):
        return []

    session_uuid = data.get("sessionId", path.stem)
    # Approximate timestamp from file mtime
    mtime_iso = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).isoformat()

    events = []
    for msg in data.get("messages", []):
        if not isinstance(msg, dict):
            continue

        # thoughts[] → decision events (structured reasoning)
        for thought in msg.get("thoughts", []):
            if not isinstance(thought, dict):
                continue
            subject = thought.get("subject", "").strip()
            description = thought.get("description", "").strip()
            if not subject and not description:
                continue
            # Drop navigation/orientation noise — secondary agent orienting itself,
            # not domain knowledge worth keeping.
            if subject.lower() in GEMINI_NOISE_SUBJECTS:
                continue
            # fact_key: hash of subject for cross-session dedup
            fact_key = hashlib.sha256(subject.lower().encode()).hexdigest()[:16]
            events.append({
                "type": "decision",
                "provenance": "structured_reasoning",
                "agent": "gemini",
                "session_uuid": session_uuid,
                "timestamp": mtime_iso,
                "fact_key": fact_key,
                "subject": subject,
                "description": description,
                "outcome": None,  # reviewer assigns at merge time
            })

        # toolCalls[] → discovery events (production reality)
        for tc in msg.get("toolCalls", []):
            if not isinstance(tc, dict):
                continue
            tool_name = tc.get("name", "")
            args = tc.get("args", {})
            # Navigate nested result structure defensively
            output = None
            result = tc.get("result")
            if isinstance(result, list) and len(result) > 0:
                try:
                    output = result[0]["functionResponse"]["response"]["output"]
                except (KeyError, TypeError):
                    pass
            if not output and not tool_name:
                continue
            # Drop discoveries whose output contains credential-like patterns.
            if output and any(pat in output for pat in GEMINI_CREDENTIAL_PATTERNS):
                continue
            # fact_key: hash of tool_name + args for cross-session dedup
            args_str = json.dumps(args, sort_keys=True) if args else ""
            fact_key = hashlib.sha256(f"{tool_name}:{args_str}".encode()).hexdigest()[:16]
            events.append({
                "type": "discovery",
                "provenance": "automated",
                "agent": "gemini",
                "session_uuid": session_uuid,
                "timestamp": mtime_iso,
                "fact_key": fact_key,
                "tool": tool_name,
                "args": args,
                "output": str(output)[:2000] if output else None,
                "outcome": None,
            })

    return events


def parse_pentagi_flow(flow_data: dict, logs: dict) -> list[dict]:
    """Convert a PentAGI flow's logs into gaius events.

    Args:
        flow_data: {id, status, title, createdAt, updatedAt}
        logs: {agentLogs, terminalLogs, searchLogs, messageLogs}

    Returns: list of event dicts with type/provenance/fact_key/model_family etc.
    """
    flow_id = str(flow_data.get("id", ""))
    timestamp = flow_data.get("createdAt", "")
    events = []
    model_family = MODEL_INFO["pentagi"]["family"]
    model_version = MODEL_INFO["pentagi"]["default_version"]

    # AgentLogs
    for log in logs.get("agentLogs", []):
        log_id = str(log.get("id", ""))
        executor = log.get("executor", "unknown")
        result = (log.get("result") or "")[:500].strip()
        task = (log.get("task") or "").strip()
        if not result or len(result) < 20:
            continue
        if any(pat in result for pat in CREDENTIAL_PATTERNS):
            continue

        if executor in ("pentester", "coder"):
            ev_type, prov = "discovery", "automated"
            fact_text = f"[pentest:{executor}] {result}"
        else:
            ev_type, prov = "decision", "structured_reasoning"
            fact_text = f"[plan] {task}: {result}" if task else f"[plan] {result}"

        fact_key = hashlib.sha256(f"agent:{log_id}".encode()).hexdigest()[:16]
        events.append({
            "type": ev_type,
            "provenance": prov,
            "agent": "pentagi",
            "session_uuid": f"pentagi-flow-{flow_id}",
            "timestamp": timestamp,
            "fact_key": fact_key,
            "subject": task[:200] if ev_type == "decision" else fact_text[:200],
            "description": result if ev_type == "decision" else "",
            "tool": f"pentagi-{executor}" if ev_type == "discovery" else None,
            "output": result if ev_type == "discovery" else None,
            "outcome": None,
            "model_family": model_family,
            "model_version": model_version,
        })

    # TerminalLogs — stdout only, skip short entries and stdin
    for log in logs.get("terminalLogs", []):
        log_id = str(log.get("id", ""))
        log_type = log.get("type", "")
        text = (log.get("text") or "").strip()
        if log_type != "stdout" or len(text) < 50:
            continue
        if any(pat in text for pat in CREDENTIAL_PATTERNS):
            continue

        fact_key = hashlib.sha256(f"terminal:{log_id}".encode()).hexdigest()[:16]
        events.append({
            "type": "discovery",
            "provenance": "automated",
            "agent": "pentagi",
            "session_uuid": f"pentagi-flow-{flow_id}",
            "timestamp": timestamp,
            "fact_key": fact_key,
            "tool": "terminal",
            "output": text[:500],
            "outcome": None,
            "model_family": model_family,
            "model_version": model_version,
        })

    # SearchLogs
    for log in logs.get("searchLogs", []):
        log_id = str(log.get("id", ""))
        engine = log.get("engine", "unknown")
        query = (log.get("query") or "").strip()
        result = (log.get("result") or "")[:300].strip()
        if not result:
            continue
        if any(pat in result for pat in CREDENTIAL_PATTERNS):
            continue

        fact_key = hashlib.sha256(f"search:{log_id}".encode()).hexdigest()[:16]
        events.append({
            "type": "discovery",
            "provenance": "automated",
            "agent": "pentagi",
            "session_uuid": f"pentagi-flow-{flow_id}",
            "timestamp": timestamp,
            "fact_key": fact_key,
            "tool": f"search-{engine}",
            "output": f"{query}: {result}",
            "outcome": None,
            "model_family": model_family,
            "model_version": model_version,
        })

    # MessageLogs — thoughts and reports only
    for log in logs.get("messageLogs", []):
        log_id = str(log.get("id", ""))
        msg_type = log.get("type", "")
        message = (log.get("message") or "").strip()
        if msg_type not in ("thoughts", "report", "done"):
            continue
        if not message or len(message) < 20:
            continue
        if any(pat in message for pat in CREDENTIAL_PATTERNS):
            continue

        fact_key = hashlib.sha256(f"message:{log_id}".encode()).hexdigest()[:16]
        events.append({
            "type": "decision",
            "provenance": "structured_reasoning",
            "agent": "pentagi",
            "session_uuid": f"pentagi-flow-{flow_id}",
            "timestamp": timestamp,
            "fact_key": fact_key,
            "subject": f"[{msg_type}]",
            "description": message[:500],
            "outcome": None,
            "model_family": model_family,
            "model_version": model_version,
        })

    return events


def parse_pentagi_flow_from_jsonl(path: Path) -> list[dict]:
    """Parse a local PentAGI flow JSONL (written by pentagi-retire fetch phase) into events.

    JSONL format: first line is _meta header with flow_data, subsequent lines are log entries.
    """
    try:
        with open(path) as f:
            lines = [line.strip() for line in f if line.strip()]
    except Exception as e:
        print(f"  warning: failed to read {path.name}: {e}", file=sys.stderr)
        return []

    if not lines:
        return []

    # First line is _meta header
    try:
        meta = json.loads(lines[0])
    except json.JSONDecodeError:
        return []

    flow_data = meta.get("_meta", meta)
    logs: dict = {"agentLogs": [], "terminalLogs": [], "searchLogs": [], "messageLogs": []}

    for line in lines[1:]:
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        log_type = entry.get("_log_type", "")
        if log_type in logs:
            logs[log_type].append(entry)

    return parse_pentagi_flow(flow_data, logs)


def parse_ollama_events(path: Path) -> list[dict]:
    """Parse Ollama inference session JSONL into gaius events.

    Each line is a {ts, query, response, model, domain, tokens, latency_ms} entry
    written by k0 ops ask.
    """
    model_family = MODEL_INFO["ollama"]["family"]
    default_version = MODEL_INFO["ollama"]["default_version"]
    events = []

    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue

                query = entry.get("query", "")
                response = entry.get("response", "")
                if not response or len(response) < 20:
                    continue
                if any(pat in response for pat in CREDENTIAL_PATTERNS):
                    continue

                fact_key = hashlib.sha256(f"ollama:{query[:100]}".encode()).hexdigest()[:16]
                events.append({
                    "type": "decision",
                    "provenance": "inference",
                    "agent": "ollama",
                    "session_uuid": path.stem,
                    "timestamp": entry.get("ts", ""),
                    "fact_key": fact_key,
                    "subject": query[:200],
                    "description": response[:2000],
                    "model_family": model_family,
                    "model_version": entry.get("model", default_version),
                    "outcome": None,
                    "source": entry.get("source", "human"),
                    "session_type": entry.get("session_type", "interactive"),
                })
    except Exception as e:
        print(f"  warning: failed to read {path.name}: {e}", file=sys.stderr)

    return events


def _content_blocks_to_text(content) -> str:
    """Flatten an LLM message 'content' (str, or list of {type,text} blocks) to text."""
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = []
        for b in content:
            if isinstance(b, dict):
                t = b.get("text")
                if isinstance(t, str):
                    parts.append(t)
            elif isinstance(b, str):
                parts.append(b)
        return "".join(parts).strip()
    return ""


def parse_grok_events(session_dir: Path) -> list[dict]:
    """Parse a Grok CLI session directory into gaius decision events.

    Reads chat_history.jsonl (the conversation) and summary.json (metadata).
    An assistant message WITHOUT tool_calls is a terminal answer (high signal);
    assistant messages WITH tool_calls are tool-preamble narration and are skipped.
    The most recent user query becomes the event subject.
    """
    chat_path = session_dir / "chat_history.jsonl"
    if not chat_path.exists():
        return []

    summary = {}
    summary_path = session_dir / "summary.json"
    if summary_path.exists():
        try:
            summary = json.loads(summary_path.read_text())
        except Exception:
            summary = {}
    info = summary.get("info", {}) if isinstance(summary, dict) else {}
    session_uuid = info.get("id") or session_dir.name
    default_version = summary.get("current_model_id") or MODEL_INFO["grok"]["default_version"]
    timestamp = summary.get("updated_at", "") if isinstance(summary, dict) else ""
    model_family = MODEL_INFO["grok"]["family"]

    events = []
    last_user = ""
    try:
        with open(chat_path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    continue
                mtype = msg.get("type")
                if mtype == "user":
                    # Grok wraps operator turns in literal <user_query> tags
                    last_user = re.sub(r'</?user_query>', '',
                                       _content_blocks_to_text(msg.get("content"))).strip()
                elif mtype == "assistant":
                    # tool_calls present → mid-turn narration, not the answer
                    if msg.get("tool_calls"):
                        continue
                    content = msg.get("content", "")
                    if not isinstance(content, str):
                        content = _content_blocks_to_text(content)
                    content = content.strip()
                    if len(content) < PEER_AGENT_MIN_RESPONSE:
                        continue
                    if any(pat in content for pat in CREDENTIAL_PATTERNS):
                        continue
                    subject = (last_user or "(no preceding user query)")[:200]
                    fact_key = hashlib.sha256(
                        f"grok:{subject[:100]}:{content_hash(content)[:8]}".encode()
                    ).hexdigest()[:16]
                    events.append({
                        "type": "decision",
                        "provenance": "inference",
                        "agent": "grok",
                        "session_uuid": session_uuid,
                        "timestamp": timestamp,
                        "fact_key": fact_key,
                        "subject": subject,
                        "description": content[:2000],
                        "model_family": model_family,
                        "model_version": msg.get("model_id") or default_version,
                        "outcome": None,
                    })
    except Exception as e:
        print(f"  warning: failed to read {chat_path}: {e}", file=sys.stderr)

    return events


def parse_codex_events(path: Path) -> list[dict]:
    """Parse a Codex CLI rollout JSONL into gaius decision events.

    Each line is {type, payload}. Conversation turns are type=='response_item'
    with payload.type=='message' and role in user/assistant. Assistant
    'output_text' blocks are answers; the most recent real user query becomes
    the subject. The line-0 session_meta supplies id/timestamp.
    """
    meta = {}
    events = []
    last_user = ""
    model_family = MODEL_INFO["codex"]["family"]
    default_version = MODEL_INFO["codex"]["default_version"]
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                rtype = rec.get("type")
                payload = rec.get("payload", {})
                if not isinstance(payload, dict):
                    continue
                if rtype == "session_meta":
                    meta = payload
                    continue
                if rtype != "response_item" or payload.get("type") != "message":
                    continue
                role = payload.get("role")
                text = _content_blocks_to_text(payload.get("content"))
                if not text:
                    continue
                if role == "user":
                    # Skip injected environment/instruction context
                    if text.startswith(_CODEX_CONTEXT_MARKERS):
                        continue
                    last_user = text
                elif role == "assistant":
                    if len(text) < PEER_AGENT_MIN_RESPONSE:
                        continue
                    if any(pat in text for pat in CREDENTIAL_PATTERNS):
                        continue
                    subject = (last_user or "(no preceding user query)")[:200]
                    fact_key = hashlib.sha256(
                        f"codex:{subject[:100]}:{content_hash(text)[:8]}".encode()
                    ).hexdigest()[:16]
                    events.append({
                        "type": "decision",
                        "provenance": "inference",
                        "agent": "codex",
                        "session_uuid": meta.get("id") or path.stem,
                        "timestamp": meta.get("timestamp", ""),
                        "fact_key": fact_key,
                        "subject": subject,
                        "description": text[:2000],
                        "model_family": model_family,
                        "model_version": default_version,
                        "outcome": None,
                    })
    except Exception as e:
        print(f"  warning: failed to read {path}: {e}", file=sys.stderr)

    return events


def _discover_grok_sessions(sessions_dir: Path):
    """Yield Grok session directories (each contains chat_history.jsonl).

    Layout: <sessions_dir>/<urlencoded-cwd>/<uuid>/chat_history.jsonl
    """
    for cwd_dir in sorted(sessions_dir.iterdir()):
        if not cwd_dir.is_dir():
            continue
        for sess in sorted(cwd_dir.iterdir()):
            if sess.is_dir() and (sess / "chat_history.jsonl").exists():
                yield sess


def _discover_codex_sessions(sessions_dir: Path):
    """Yield Codex rollout JSONL files (nested by YYYY/MM/DD)."""
    yield from sorted(sessions_dir.rglob("rollout-*.jsonl"))
