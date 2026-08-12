"""gaius.embed — embedding model, warm-daemon fast path, chunking, backfill.

Owns the lazy sentence-transformers handle (_EMBED_MODEL — module state, mutated
by _get_embed_model, deliberately NOT re-exported), the gaius-embed-daemon unix
socket client, the 256-token-safe chunker, and `gaius embed` (backfill).

Facade convention (see ARCHITECTURE.md): _core re-imports this module's public
symbols in the FACADE RE-EXPORTS block, so `from gaius._core import _embed_text`
keeps working for __init__.py, mcp_server.py, and every sibling.
"""
import json
import os
import sys
from pathlib import Path

# Lazy-loaded embedding model (sentence-transformers)
_EMBED_MODEL = None
_EMBED_DIM = 384

def _get_embed_model():
    """Load the embedding model lazily (first call takes ~2s)."""
    global _EMBED_MODEL
    if _EMBED_MODEL is None:
        try:
            from sentence_transformers import SentenceTransformer
            _EMBED_MODEL = SentenceTransformer("all-MiniLM-L6-v2")
        except ImportError:
            return None
    return _EMBED_MODEL

_EMBED_DAEMON_SOCK = Path.home() / ".gaius" / "embed.sock"

def _embed_via_daemon(text: str) -> list[float] | None:
    """Fast path: embed via the resident gaius-embed-daemon (avoids 6s model cold-load).
    Returns None if daemon is not running or errors — caller falls back to inline load."""
    sock_path = str(_EMBED_DAEMON_SOCK)
    if not os.path.exists(sock_path):
        return None
    try:
        import socket as _socket
        with _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM) as s:
            s.settimeout(2.0)
            s.connect(sock_path)
            s.sendall((json.dumps({"text": text}) + "\n").encode())
            resp = b""
            while True:
                chunk = s.recv(4096)
                if not chunk:
                    break
                resp += chunk
                if b"\n" in resp:
                    break
        data = json.loads(resp.split(b"\n")[0])
        if "vector" in data:
            return data["vector"]
        return None
    except Exception:
        return None


def _embed_text(text: str) -> list[float] | None:
    """Embed text into a 384-dim vector. Returns None if model unavailable.
    Tries warm daemon first (~5ms), falls back to inline model load (~6s)."""
    vec = _embed_via_daemon(text)
    if vec is not None:
        return vec
    model = _get_embed_model()
    if model is None:
        return None
    return model.encode(text, normalize_embeddings=True).tolist()

def _embed_texts(texts: list[str]) -> list[list[float]] | None:
    """Batch embed multiple texts. Returns None if model unavailable."""
    model = _get_embed_model()
    if model is None:
        return None
    return model.encode(texts, normalize_embeddings=True).tolist()


_CHUNK_WORDS = 170  # ~220 wordpieces, safely under all-MiniLM-L6-v2's 256-token cap


def _chunk_text(text: str, max_words: int = _CHUNK_WORDS) -> list[str]:
    """Split text into <=max_words word-windows so each fits the embedder's 256-token
    cap instead of being silently truncated to its first ~256 tokens. Short text (the
    common case) returns a single chunk == the original string byte-for-byte, so
    short-fact embeddings are unchanged."""
    words = text.split()
    if len(words) <= max_words:
        return [text]
    return [" ".join(words[i:i + max_words]) for i in range(0, len(words), max_words)]


def cmd_embed(args):
    """Backfill embeddings for all facts in facts.db.
    Run once after enabling sqlite-vec, then embeddings are maintained automatically."""
    # Lazy: resolved at command time through the fully-built _core facade, so this
    # module never depends on where init_db/_store_embedding physically live.
    from gaius._core import HAS_SQLITE_VEC, init_db, _store_embedding
    if not HAS_SQLITE_VEC:
        print("ERROR: sqlite-vec not installed. Run: uv pip install sqlite-vec sentence-transformers")
        sys.exit(1)
    model = _get_embed_model()
    if model is None:
        print("ERROR: sentence-transformers not installed.")
        sys.exit(1)

    conn = init_db()
    facts = conn.execute("SELECT id, fact_text FROM facts WHERE tombstoned_at IS NULL").fetchall()

    # Find facts without embeddings
    existing_ids = set()
    try:
        rows = conn.execute("SELECT fact_id FROM fact_embeddings").fetchall()
        existing_ids = {r[0] for r in rows}
    except Exception:
        pass

    to_embed = [(f["id"], f["fact_text"]) for f in facts if f["id"] not in existing_ids]
    if not to_embed:
        print(f"All {len(facts)} facts already have embeddings.")
        return

    print(f"Embedding {len(to_embed)} facts ({len(existing_ids)} already done)...")

    # Embed each fact via _store_embedding so long facts are chunked consistently
    # with the live write path (one row per <=256-token chunk), instead of one
    # truncated vector per fact.
    embedded = 0
    for fact_id, fact_text in to_embed:
        _store_embedding(conn, fact_id, fact_text)
        embedded += 1
        if embedded % 1000 == 0 or embedded == len(to_embed):
            print(f"  {embedded}/{len(to_embed)} embedded")
    conn.commit()
    print(f"Done. {embedded} facts embedded (chunked as needed).")
