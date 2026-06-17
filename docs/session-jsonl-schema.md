# gaius Session JSONL Schema

gaius consumes session transcripts in JSONL format (one JSON object per line). Two schemas are supported:

## 1. Simple Turn Format (recommended for vLLM/open models)

One line per conversation turn. Used by `gaius record` and `gaius retire --format vllm`.

```jsonl
{"ts": "2026-05-03T14:30:00Z", "query": "how do I expand a PVC?", "response": "kubectl edit pvc ...", "model": "gemma-4-27b", "tokens": 142, "latency_ms": 890, "session_type": "interactive", "source": "gaius-record"}
{"ts": "2026-05-03T14:31:15Z", "query": "what if the storage class doesn't allow expansion?", "response": "Check allowVolumeExpansion...", "model": "gemma-4-27b", "tokens": 203, "latency_ms": 1120, "session_type": "interactive", "source": "gaius-record"}
```

### Fields

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `ts` | string (ISO 8601) | yes | Timestamp of the turn |
| `query` | string | yes | User's input |
| `response` | string | yes | Model's response |
| `model` | string | yes | Model identifier (e.g. `gemma-4-27b`, `nemotron-mini-4b`) |
| `tokens` | int | no | Completion tokens (estimated if unavailable) |
| `latency_ms` | int | no | Response latency in milliseconds |
| `session_type` | string | no | Tag for session classification (default: `interactive`) |
| `source` | string | no | What produced this entry (e.g. `gaius-record`, `my-tui`, `k0-ops-ask`) |
| `domain` | string | no | Domain hint for fact routing (e.g. `security`, `storage`) |

### Minimum viable line

```json
{"ts": "2026-05-03T14:30:00Z", "query": "...", "response": "...", "model": "gemma-4-27b"}
```

gaius will still process it — missing fields get defaults.

---

## 2. Claude Code Format (native, auto-detected)

Multi-line session with typed entries. Used by Claude Code natively — you don't need to produce this yourself unless building a Claude-compatible TUI.

```jsonl
{"type": "user", "message": {"content": [{"type": "text", "text": "expand the PVC"}]}, "timestamp": "2026-05-03T14:30:00Z"}
{"type": "assistant", "message": {"content": [{"type": "text", "text": "I'll edit the PVC manifest..."}]}, "timestamp": "2026-05-03T14:30:02Z"}
{"type": "tool_result", "tool": "Bash", "content": "persistentvolumeclaim/data-pvc patched", "timestamp": "2026-05-03T14:30:05Z"}
```

### Entry types

| `type` | What gaius extracts |
|--------|---------------------|
| `assistant` | Reasoning, decisions, procedures (scored by signal density) |
| `user` | Intent context (used for session classification) |
| `tool_result` | Errors → failure patterns; success confirmations → fact reinforcement |

### Compaction marker

Sessions with `{"isCompactSummary": true, ...}` entries are pre-summarized by Claude Code. gaius skips raw extraction and stages the summary directly.

---

## 3. Gemini CLI Format (auto-detected by .json extension)

Single JSON file (not JSONL) containing the session. Auto-detected when file extension is `.json` in the sessions directory.

See `parse_gemini_events()` in `gaius/_core.py` for the full parser.

---

## File naming

- **Location**: configurable via `sessions_dir` in `~/.gaius/config.yaml`
- **Default (Claude)**: `~/.claude/projects/<project-hash>/*.jsonl`
- **Default (vLLM)**: `~/.gaius/sessions/<uuid>.jsonl`
- **Convention**: UUID or timestamp-based filenames. gaius uses the filename stem as `session_uuid`.

---

## Producing sessions for gaius

### Option A: `gaius record` (built-in)

```bash
# Interactive REPL against a vLLM endpoint
gaius record --endpoint http://localhost:8000 --model gemma-4-27b

# Pipe from another tool
my-chat-tool | gaius record --stdin --model nemotron-mini
```

### Option B: Your own tool writes JSONL

Emit the Simple Turn Format above to `~/.gaius/sessions/<session-id>.jsonl`. Then:

```bash
gaius retire --format vllm
```

### Option C: OpenAI-compatible logging middleware

If your chat tool already talks to an OpenAI-compatible endpoint, add a logging layer:

```python
# After each /v1/chat/completions response:
with open(session_path, "a") as f:
    f.write(json.dumps({
        "ts": datetime.utcnow().isoformat() + "Z",
        "query": messages[-2]["content"],  # user message
        "response": response["choices"][0]["message"]["content"],
        "model": response["model"],
        "tokens": response["usage"]["completion_tokens"],
        "latency_ms": int(elapsed * 1000),
    }) + "\n")
```

---

## How gaius processes sessions

1. **Retire** (`gaius retire --format vllm`): Scans session files, deduplicates against processed sessions table, extracts signal using scoring pipeline
2. **Stage**: High-signal turns become staged entries in the review queue
3. **Review**: `gaius next` / `gaius batch` for human review
4. **Promote**: `gaius done <id>` marks entries as reviewed → eligible for inject

Cross-model corroboration: if both a Claude session and a vLLM session confirm the same fact (matched by entity + semantic similarity), the fact gets a 1.5x score boost.

---

## Extending for new backends

To add a new session format:

1. Write a parser: `def parse_<backend>_events(path: Path) -> list[dict]`
2. Each event must have: `type`, `provenance`, `agent`, `session_uuid`, `timestamp`, `fact_key`, `subject`, `description`
3. Register in `SUPPORTED_FORMATS` and the format dispatch in `cmd_retire`
4. Add auto-detection in `_detect_format(path)` if the file extension is unique
