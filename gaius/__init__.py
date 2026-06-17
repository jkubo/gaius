"""gaius — Session memory lifecycle manager for Claude Code projects.

Import key symbols from the core module for programmatic access.
"""

from gaius._core import (
    # Entry point
    main,
    # DB
    init_db,
    DB_PATH,
    # Embedding
    _embed_text,
    _embed_texts,
    _embed_via_daemon,
    _get_embed_model,
    _EMBED_DIM,
    _EMBED_DAEMON_SOCK,
    # Facts
    upsert_fact,
    # Skills
    load_skills,
    compute_skill_score,
    # Config
    DOMAIN_DIR,
    SKILLS_DIR,
    STAGING_DIR,
    CORPUS_DIR,
)

__version__ = "0.1.0"

__all__ = [
    "main",
    "init_db",
    "DB_PATH",
    "_embed_text",
    "_embed_texts",
    "_embed_via_daemon",
    "_get_embed_model",
    "_EMBED_DIM",
    "_EMBED_DAEMON_SOCK",
    "upsert_fact",
    "load_skills",
    "compute_skill_score",
    "DOMAIN_DIR",
    "SKILLS_DIR",
    "STAGING_DIR",
    "CORPUS_DIR",
]
