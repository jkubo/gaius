"""MCP SDK 1 / SDK 2 import selection and a real stdio client lifecycle.

The server must bind the class the installed SDK actually exports:
MCPServer from mcp.server on SDK 2, FastMCP from mcp.server.fastmcp on
SDK 1. Nothing in this module substitutes one class for the other.

Tool-result fields are snake_case on SDK 2 (is_error, protocol_version,
server_info). SDK 1 still uses the camelCase attributes (isError,
protocolVersion, serverInfo). Read whichever the installed client has.
"""

import asyncio
import json
import os
import sqlite3
import sys
from pathlib import Path

os.environ["GAIUS_CONFIG"] = "/dev/null"

import pytest

pytest.importorskip("mcp")

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

REPO = Path(__file__).resolve().parents[1]

TOOL_NAMES = {
    "gaius_search",
    "gaius_kg_query",
    "gaius_kg_timeline",
    "gaius_stats",
    "gaius_prime_session",
    "gaius_skill_recommend",
    "gaius_fact_add",
}

FACT_TEXT = (
    "The widget cache TTL is sixty seconds in the default profile."
)


def _installed_mcp_major() -> int:
    from importlib.metadata import version

    return int(version("mcp").split(".", 1)[0])


def _first_attr(obj, *names):
    for name in names:
        if hasattr(obj, name):
            return getattr(obj, name)
    raise AssertionError(
        f"{type(obj).__name__} has none of {names}; fields={list(getattr(obj, 'model_fields', {}))}"
    )


def _tool_failed(result) -> bool:
    return bool(_first_attr(result, "is_error", "isError"))


def _tool_text(result) -> str:
    parts = []
    for block in result.content or []:
        text = getattr(block, "text", None)
        if text:
            parts.append(text)
    return "\n".join(parts)


def _child_env(root: Path, db_path: Path, telemetry_path: Path, session_uuid: str) -> dict:
    """Disposable paths the server already honors. Do not repurpose HOME."""
    memory = root / "memory"
    domain = root / "domain"
    skills = root / "skills"
    cache = root / "cache"
    for path in (memory, domain, skills, cache):
        path.mkdir(parents=True, exist_ok=True)
    return {
        "PATH": os.environ.get("PATH", ""),
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        "PYTHONPATH": str(REPO),
        "PYTHONNOUSERSITE": "1",
        "GAIUS_CONFIG": "/dev/null",
        "GAIUS_DB_PATH": str(db_path),
        "GAIUS_TELEMETRY_DB": str(telemetry_path),
        "GAIUS_SESSION_UUID": session_uuid,
        "GAIUS_AGENT": "mcp-compat",
        "GAIUS_MEMORY_DIR": str(memory),
        "GAIUS_DOMAIN_DIR": str(domain),
        "GAIUS_SKILLS_DIR": str(skills),
        "HF_HOME": str(cache / "hf"),
        "SENTENCE_TRANSFORMERS_HOME": str(cache / "sentence-transformers"),
        "XDG_CACHE_HOME": str(cache),
    }


def test_import_binds_the_installed_server_class():
    """The production module binds the server class the installed SDK exports."""
    from gaius import mcp_server

    assert Path(mcp_server.__file__).resolve() == REPO / "gaius" / "mcp_server.py"

    major = _installed_mcp_major()
    if major >= 2:
        from mcp.server import MCPServer

        assert type(mcp_server.mcp) is MCPServer
    else:
        from mcp.server.fastmcp import FastMCP

        assert type(mcp_server.mcp) is FastMCP
        server_api = __import__("mcp.server", fromlist=["MCPServer"])
        assert not hasattr(server_api, "MCPServer")

    instructions = mcp_server.mcp.instructions or ""
    assert "gaius_search" in instructions
    assert "gaius_fact_add" in instructions


async def _stdio_round(db_path: Path, telemetry_path: Path, session_uuid: str, fact_text: str):
    env = _child_env(db_path.parent / f"scratch-{session_uuid}", db_path, telemetry_path, session_uuid)
    server = StdioServerParameters(
        command=sys.executable,
        args=["-m", "gaius.mcp_server"],
        env=env,
        cwd=str(REPO),
    )
    async with stdio_client(server) as streams:
        async with ClientSession(*streams) as session:
            init = await session.initialize()
            protocol = _first_attr(init, "protocol_version", "protocolVersion")
            info = _first_attr(init, "server_info", "serverInfo")
            assert protocol
            assert info.name == "gaius"

            listed = await session.list_tools()
            names = {tool.name for tool in listed.tools}
            assert names == TOOL_NAMES

            stats = await session.call_tool("gaius_stats", {})
            assert not _tool_failed(stats), _tool_text(stats)
            assert "facts" in _tool_text(stats).lower()

            # Empty source falls through to GAIUS_SESSION_UUID inside fact_add.
            added = await session.call_tool(
                "gaius_fact_add",
                {"fact_text": fact_text, "domain": "general", "source": ""},
            )
            assert not _tool_failed(added), _tool_text(added)
            assert "reject" not in _tool_text(added).lower()


def test_stdio_lifecycle_across_two_session_uuids(tmp_path):
    """initialize, all seven tools, stats, fact_add, distinct session attribution, source=mcp."""
    from gaius.facts import init_db

    db_path = tmp_path / "facts.db"
    telemetry_path = tmp_path / "telemetry.db"
    init_db(db_path).close()

    uuids = ("mcp-session-alpha", "mcp-session-beta")

    async def _both():
        for session_uuid in uuids:
            await _stdio_round(db_path, telemetry_path, session_uuid, FACT_TEXT)

    asyncio.run(asyncio.wait_for(_both(), timeout=90))

    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT agents, sessions, confirmation_count FROM facts WHERE fact_text = ?",
            (FACT_TEXT,),
        ).fetchone()
    finally:
        conn.close()
    assert row is not None, "fact_add did not write the temporary database"
    assert "mcp-compat" in json.loads(row[0])
    assert set(json.loads(row[1])) == set(uuids), row
    assert row[2] >= 2

    telem = sqlite3.connect(telemetry_path)
    try:
        events = telem.execute(
            "SELECT tool_name, source, session_id FROM tool_events ORDER BY id"
        ).fetchall()
    finally:
        telem.close()
    assert events, "telemetry database has no tool_events"
    assert {event[1] for event in events} == {"mcp"}
    assert {event[2] for event in events} == set(uuids)
    assert {event[0] for event in events} >= {"gaius_stats", "gaius_fact_add"}
