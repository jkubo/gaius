"""BA-6 profile provenance — hash of the agent *version*, never its contents.

`kub0.profile.sha256` covers {instruction files, skills *list*, MCP structure,
model, sampling}. File bodies and MCP env/headers are hashed or dropped
locally; only the digest is returned. Spec: ansible
drafts/agent-observability-gaps-geap-20260813.md §4 BA-6.

The document is versioned (`v`) so a later input-set change does not silently
collide with an old digest.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

PROFILE_VERSION = 1
HASH_CAP = 64 * 1024 * 1024
INSTRUCTION_NAMES = ("CLAUDE.md", "AGENTS.md")

_SECRET_ARG_RE = re.compile(
    r"(?i)(token|secret|passwd|password|credential|api[_-]?key|authorization|"
    r"bearer|cookie|vault)"
)
# Value-shape redaction (same class as telemetry._REDACT_PATTERNS) so a
# command path or arg that IS a token never sits in the in-memory document.
_SECRET_VAL_RES = [re.compile(p) for p in (
    r"\bgh[pousr]_[A-Za-z0-9]{8,}\b",
    r"\bgithub_pat_[A-Za-z0-9_]{20,}\b",
    r"\bsk-ant-[A-Za-z0-9_-]{16,}\b",
    r"\bsk-[A-Za-z0-9_-]{20,}\b",
    r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b",
    r"\bAKIA[0-9A-Z]{16}\b",
    r"\btskey-[A-Za-z0-9-]{12,}\b",
    r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{5,}\b",
    r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{16,}",
)]


def _canonical_json(obj) -> str:
    try:
        return json.dumps(obj, sort_keys=True, separators=(",", ":"),
                          ensure_ascii=False, default=str)
    except Exception:
        return str(obj)


def _home(home=None) -> Path:
    return Path(home) if home is not None else Path.home()


def relpath_for(path: Path, home=None) -> str:
    """Stable path key: relative to $HOME when possible, else absolute."""
    resolved = path.resolve()
    try:
        return str(resolved.relative_to(_home(home).resolve()))
    except ValueError:
        return str(resolved)


def file_sha256(path: Path) -> str:
    """sha256 of file bytes, or a sentinel. Never returns content."""
    try:
        size = path.stat().st_size
        if size > HASH_CAP:
            return f"over-cap:{size}"
        h = hashlib.sha256()
        with path.open("rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                h.update(chunk)
        return h.hexdigest()
    except Exception:
        return "unreadable"


def _walk_stop(cwd: Path, home: Path) -> Path:
    """Where the instruction-file walk ends.

    cwd under $HOME → stop at $HOME (never climb into /tmp or /).
    cwd outside $HOME → stop at the git root if one exists, else cwd.
    `d == d.root` is Path==str and was a dead terminator.
    """
    try:
        if cwd == home or home in cwd.parents:
            return home
    except Exception:
        pass
    d = cwd
    for _ in range(16):
        try:
            if (d / ".git").exists():
                return d
        except Exception:
            break
        if d.parent == d:
            break
        d = d.parent
    return cwd


def collect_instruction_files(cwd, home=None) -> list[Path]:
    """Walk cwd→stop for CLAUDE.md / AGENTS.md plus ~/.claude/CLAUDE.md."""
    cwd = Path(cwd).resolve()
    home_p = _home(home).resolve()
    stop = _walk_stop(cwd, home_p)
    found: dict[str, Path] = {}

    def _add(p: Path):
        try:
            if p.is_file():
                found[str(p.resolve())] = p.resolve()
        except Exception:
            return

    for d in [cwd, *cwd.parents]:
        for name in INSTRUCTION_NAMES:
            _add(d / name)
        _add(d / ".claude" / "CLAUDE.md")
        if d == stop:
            break

    _add(home_p / ".claude" / "CLAUDE.md")
    return sorted(found.values(), key=lambda p: str(p))


def collect_skill_names(cwd, home=None) -> list[str]:
    """Sorted unique skill *names* (directory with SKILL.md). Bodies ignored."""
    home_p = _home(home)
    cwd_p = Path(cwd)
    dirs = (
        home_p / ".claude" / "skills",
        home_p / ".grok" / "skills",
        home_p / ".grok" / "bundled" / "skills",
        cwd_p / ".grok" / "skills",
        cwd_p / ".claude" / "skills",
    )
    names: set[str] = set()
    for d in dirs:
        try:
            if not d.is_dir():
                continue
            for child in d.iterdir():
                if not child.is_dir():
                    continue
                if (child / "SKILL.md").is_file() or (child / "skill.md").is_file():
                    names.add(child.name)
        except Exception:
            continue
    return sorted(names)


def _strip_url(url: str) -> str:
    """scheme+host+port+path only — drop userinfo, query, fragment."""
    try:
        p = urlsplit(url)
    except Exception:
        return ""
    host = p.hostname or ""
    if p.port:
        host = f"{host}:{p.port}"
    return urlunsplit((p.scheme, host, p.path, "", ""))


def _redact_arg(arg) -> str:
    s = str(arg)
    if _SECRET_ARG_RE.search(s):
        return "[REDACTED]"
    for pat in _SECRET_VAL_RES:
        if pat.search(s):
            return "[REDACTED]"
    return s


def is_digest(value: str) -> bool:
    v = (value or "").strip()
    return len(v) == 64 and all(c in "0123456789abcdef" for c in v)


def mcp_structural(name: str, cfg) -> dict:
    """Name + transport identity. Never env, headers, or auth material."""
    if not isinstance(cfg, dict):
        return {"name": name, "type": "unknown"}
    out = {
        "name": name,
        "type": cfg.get("type") or ("http" if cfg.get("url") else "stdio"),
    }
    if cfg.get("command"):
        out["command"] = _redact_arg(cfg["command"])
    if isinstance(cfg.get("args"), list):
        out["args"] = [_redact_arg(a) for a in cfg["args"]]
    if cfg.get("url"):
        out["url"] = _strip_url(str(cfg["url"]))
    return out


def _load_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def collect_mcp(cwd, home=None) -> list[dict]:
    """Structural MCP inventory from the usual Claude/Grok config locations."""
    home_p = _home(home)
    cwd_p = Path(cwd)
    files = (
        home_p / ".claude.json",
        home_p / ".claude" / ".mcp.json",
        cwd_p / ".mcp.json",
        cwd_p / ".claude.json",
        home_p / ".grok" / "mcp.json",
        cwd_p / ".grok" / "mcp.json",
    )
    servers: dict[str, dict] = {}
    for f in files:
        try:
            if not f.is_file():
                continue
        except Exception:
            continue
        data = _load_json(f)
        if not isinstance(data, dict):
            continue
        block = data.get("mcpServers") if "mcpServers" in data else data.get("mcp")
        if not isinstance(block, dict):
            continue
        for name, cfg in block.items():
            servers[str(name)] = mcp_structural(str(name), cfg)
    return [servers[k] for k in sorted(servers)]


def _read_toml(path: Path) -> dict:
    try:
        import tomllib
        with path.open("rb") as f:
            return tomllib.load(f) or {}
    except Exception:
        return {}


def collect_model_sampling(home=None, model=None, sampling=None) -> tuple[str, dict]:
    """Model + sampling from Claude settings / Grok config, then overrides."""
    got_model = ""
    got_sampling: dict = {}
    settings = _home(home) / ".claude" / "settings.json"
    data = _load_json(settings) if settings.is_file() else None
    if isinstance(data, dict):
        if data.get("model"):
            got_model = str(data["model"])
        if data.get("effortLevel"):
            got_sampling["effortLevel"] = data["effortLevel"]
        for k in ("temperature", "top_p", "topP", "max_tokens", "maxTokens"):
            if k in data:
                got_sampling[k] = data[k]
    grok_cfg = _home(home) / ".grok" / "config.toml"
    if grok_cfg.is_file():
        cfg = _read_toml(grok_cfg)
        # common shapes: model = "..." or [model] name = "..."
        if isinstance(cfg.get("model"), str) and not got_model:
            got_model = cfg["model"]
        elif isinstance(cfg.get("model"), dict) and cfg["model"].get("name") and not got_model:
            got_model = str(cfg["model"]["name"])
        for k in ("temperature", "top_p"):
            if k in cfg and k not in got_sampling:
                got_sampling[k] = cfg[k]
    if model:
        got_model = str(model)
    if sampling:
        got_sampling.update(sampling)
    return got_model, got_sampling


def build_profile(cwd=None, home=None, model=None, sampling=None) -> dict:
    """Canonical profile document. Contains hashes and names — never file bodies."""
    cwd_p = Path(cwd or os.getcwd())
    m, samp = collect_model_sampling(home=home, model=model, sampling=sampling)
    files = collect_instruction_files(cwd_p, home=home)
    return {
        "v": PROFILE_VERSION,
        "claude_md": [
            {"path": relpath_for(p, home=home), "sha256": file_sha256(p)}
            for p in files
        ],
        "skills": collect_skill_names(cwd_p, home=home),
        "mcp": collect_mcp(cwd_p, home=home),
        "model": m,
        "sampling": samp,
    }


def profile_sha256(cwd=None, home=None, model=None, sampling=None) -> str:
    doc = build_profile(cwd=cwd, home=home, model=model, sampling=sampling)
    return hashlib.sha256(
        _canonical_json(doc).encode("utf-8", "replace")
    ).hexdigest()


def merge_otel_resource(existing: str, digest: str) -> str:
    """Append or preserve kub0.profile.sha256 on OTEL_RESOURCE_ATTRIBUTES.

    An already-set kub0.profile.sha256 wins (operator / parent pin).
    """
    key = "kub0.profile.sha256"
    existing = (existing or "").strip()
    if not digest:
        return existing
    if existing:
        kept = []
        pinned = None
        for part in existing.split(","):
            p = part.strip()
            if not p:
                continue
            if p.startswith(key + "="):
                val = p.split("=", 1)[1]
                if is_digest(val):
                    pinned = val
                continue  # drop invalid pins
            kept.append(p)
        if pinned:
            kept.append(f"{key}={pinned}")
            return ",".join(kept)
        if digest:
            kept.append(f"{key}={digest}")
        return ",".join(kept)
    return f"{key}={digest}"


def inventory_lines(doc: dict, digest: str) -> str:
    """Operator-facing summary. Paths and names only — no file contents."""
    mcp_names = ",".join(m.get("name", "") for m in doc.get("mcp") or [])
    lines = [
        f"kub0.profile.sha256={digest}",
        f"claude_md: {len(doc.get('claude_md') or [])}",
        f"skills: {len(doc.get('skills') or [])}",
        f"mcp: {mcp_names}",
        f"model: {doc.get('model') or ''}",
        f"sampling: {_canonical_json(doc.get('sampling') or {})}",
    ]
    return "\n".join(lines) + "\n"
