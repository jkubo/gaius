#!/usr/bin/env python3
"""gaius record — capture any AI chat session into gaius-compatible JSONL.

Wraps an arbitrary chat command (vLLM, Ollama, llama.cpp server, etc.)
and logs each turn as a JSONL line compatible with `gaius retire --format vllm`.

Usage:
    gaius record -- python3 -m my_chat_tui
    gaius record --model gemma-4-27b -- curl-based-chat.sh
    gaius record --stdin  # reads from stdin pipe, no subprocess

Session JSONL format (one line per turn):
    {"ts": "ISO8601", "query": "user input", "response": "model output",
     "model": "model-name", "tokens": N, "latency_ms": N, "session_type": "interactive"}

Output: ~/.gaius/sessions/<UUID>.jsonl (configurable via config.yaml sessions_dir)
"""
import json
import os
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

# Default sessions directory for recorded sessions
DEFAULT_SESSIONS_DIR = Path.home() / ".gaius" / "sessions"


def get_sessions_dir() -> Path:
    """Resolve sessions dir from config or default."""
    config_path = Path.home() / ".gaius" / "config.yaml"
    if config_path.exists():
        try:
            import yaml
            with open(config_path) as f:
                cfg = yaml.safe_load(f) or {}
            if cfg.get("backend") == "vllm" and cfg.get("sessions_dir"):
                return Path(os.path.expanduser(cfg["sessions_dir"]))
        except Exception:
            pass
    return DEFAULT_SESSIONS_DIR


def record_stdin(model: str = "unknown", session_type: str = "interactive"):
    """Record from stdin — expects alternating user/assistant lines separated by blank lines.

    Protocol:
        USER: <text>        → logged as query
        ASSISTANT: <text>   → logged as response (paired with previous query)

    Or simpler: odd lines = query, even lines = response (blank line = turn boundary).
    """
    sessions_dir = get_sessions_dir()
    sessions_dir.mkdir(parents=True, exist_ok=True)

    session_id = str(uuid.uuid4())
    session_path = sessions_dir / f"{session_id}.jsonl"

    print(f"gaius record: writing to {session_path}", file=sys.stderr)
    print(f"gaius record: model={model}, format=jsonl", file=sys.stderr)
    print(f"gaius record: send USER:/ASSISTANT: prefixed lines, blank line = turn boundary", file=sys.stderr)
    print(f"gaius record: Ctrl+D to end session", file=sys.stderr)

    current_query = None
    turn_start = None

    with open(session_path, "a") as out:
        for line in sys.stdin:
            line = line.rstrip("\n")

            if line.startswith("USER:"):
                current_query = line[5:].strip()
                turn_start = time.time()

            elif line.startswith("ASSISTANT:"):
                response = line[10:].strip()
                if current_query:
                    latency_ms = int((time.time() - turn_start) * 1000) if turn_start else 0
                    entry = {
                        "ts": datetime.now(timezone.utc).isoformat(),
                        "query": current_query,
                        "response": response,
                        "model": model,
                        "tokens": len(response.split()),  # rough estimate
                        "latency_ms": latency_ms,
                        "session_type": session_type,
                        "source": "gaius-record",
                    }
                    out.write(json.dumps(entry) + "\n")
                    out.flush()
                    current_query = None
                    turn_start = None

    print(f"\ngaius record: session saved → {session_path}", file=sys.stderr)
    print(f"gaius record: run `gaius retire --format vllm` to extract", file=sys.stderr)
    return session_path


def record_openai_compatible(endpoint: str, model: str = "unknown",
                             session_type: str = "interactive",
                             api_key: str = ""):
    """Interactive REPL that sends to an OpenAI-compatible endpoint and records turns.

    Works with vLLM, Ollama, llama.cpp, LiteLLM, etc.
    """
    import urllib.request
    import urllib.error

    sessions_dir = get_sessions_dir()
    sessions_dir.mkdir(parents=True, exist_ok=True)

    session_id = str(uuid.uuid4())
    session_path = sessions_dir / f"{session_id}.jsonl"

    print(f"gaius record: {endpoint} (model={model})", file=sys.stderr)
    print(f"gaius record: session → {session_path}", file=sys.stderr)
    print(f"gaius record: type your message, Enter to send, Ctrl+D to end\n", file=sys.stderr)

    messages = []  # conversation history for multi-turn

    with open(session_path, "a") as out:
        while True:
            try:
                query = input("you> ").strip()
            except (EOFError, KeyboardInterrupt):
                break

            if not query:
                continue

            messages.append({"role": "user", "content": query})
            turn_start = time.time()

            # Call OpenAI-compatible endpoint
            headers = {"Content-Type": "application/json"}
            if api_key:
                headers["Authorization"] = f"Bearer {api_key}"

            payload = json.dumps({
                "model": model,
                "messages": messages,
                "max_tokens": 2048,
            }).encode()

            req = urllib.request.Request(
                f"{endpoint.rstrip('/')}/v1/chat/completions",
                data=payload, headers=headers, method="POST"
            )

            try:
                with urllib.request.urlopen(req, timeout=120) as resp:
                    data = json.loads(resp.read())
                    response = data["choices"][0]["message"]["content"]
                    usage = data.get("usage", {})
                    tokens = usage.get("completion_tokens", len(response.split()))
            except (urllib.error.URLError, KeyError, json.JSONDecodeError) as e:
                print(f"  error: {e}", file=sys.stderr)
                messages.pop()  # remove failed query from history
                continue

            latency_ms = int((time.time() - turn_start) * 1000)
            messages.append({"role": "assistant", "content": response})

            # Print response
            print(f"\n{response}\n")

            # Log turn
            entry = {
                "ts": datetime.now(timezone.utc).isoformat(),
                "query": query,
                "response": response,
                "model": model,
                "tokens": tokens,
                "latency_ms": latency_ms,
                "session_type": session_type,
                "source": "gaius-record",
            }
            out.write(json.dumps(entry) + "\n")
            out.flush()

    print(f"\ngaius record: {len(messages)//2} turns saved → {session_path}", file=sys.stderr)
    print(f"gaius record: run `gaius retire --format vllm` to extract", file=sys.stderr)
    return session_path


def main(args=None):
    """CLI entry point for `gaius record`."""
    import argparse

    parser = argparse.ArgumentParser(
        prog="gaius record",
        description="Capture AI chat sessions into gaius-compatible JSONL"
    )
    parser.add_argument("--model", default="unknown",
                        help="Model name (e.g. gemma-4-27b, nemotron-mini)")
    parser.add_argument("--endpoint", default=None,
                        help="OpenAI-compatible endpoint URL (e.g. http://localhost:8000)")
    parser.add_argument("--api-key", default="",
                        help="API key for endpoint (optional)")
    parser.add_argument("--stdin", action="store_true",
                        help="Read USER:/ASSISTANT: prefixed lines from stdin")
    parser.add_argument("--session-type", default="interactive",
                        help="Session type tag (default: interactive)")
    parser.add_argument("--output", default=None,
                        help="Override output path (default: ~/.gaius/sessions/<uuid>.jsonl)")

    parsed = parser.parse_args(args)

    if parsed.stdin:
        record_stdin(model=parsed.model, session_type=parsed.session_type)
    elif parsed.endpoint:
        record_openai_compatible(
            endpoint=parsed.endpoint,
            model=parsed.model,
            session_type=parsed.session_type,
            api_key=parsed.api_key,
        )
    else:
        # Default: try to detect from config
        config_path = Path.home() / ".gaius" / "config.yaml"
        if config_path.exists():
            try:
                import yaml
                with open(config_path) as f:
                    cfg = yaml.safe_load(f) or {}
                endpoint = cfg.get("vllm_endpoint") or cfg.get("endpoint")
                if endpoint:
                    model = parsed.model if parsed.model != "unknown" else cfg.get("model", "unknown")
                    record_openai_compatible(
                        endpoint=endpoint, model=model,
                        session_type=parsed.session_type, api_key=parsed.api_key
                    )
                    return
            except Exception:
                pass

        print("gaius record: specify --endpoint URL or --stdin", file=sys.stderr)
        print("  Examples:", file=sys.stderr)
        print("    gaius record --endpoint http://localhost:8000 --model gemma-4-27b", file=sys.stderr)
        print("    gaius record --stdin --model nemotron-mini", file=sys.stderr)
        print("    gaius record  # uses vllm_endpoint from ~/.gaius/config.yaml", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
