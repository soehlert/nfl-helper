"""SQLite persistence layer for cheatsheets, strategy rules, and league sessions."""

import json
import logging
import sqlite3
from pathlib import Path
from typing import Any

from nfl_helper.models.cheatsheet import CheatsheetContext

logger = logging.getLogger(__name__)

DEFAULT_DB_PATH = Path(__file__).resolve().parent.parent.parent.parent / "data" / "nfl_helper.db"


def get_db_connection(db_path: Path | str = DEFAULT_DB_PATH) -> sqlite3.Connection:
    """Create or connect to SQLite database with foreign keys and Row factory enabled."""
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn


def init_db(db_path: Path | str = DEFAULT_DB_PATH) -> None:
    """Initialize database schema tables for cheatsheets and strategy history."""
    conn = get_db_connection(db_path)
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS cheatsheets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                raw_text TEXT NOT NULL,
                parsed_json TEXT NOT NULL,
                player_count INTEGER NOT NULL DEFAULT 0,
                rule_count INTEGER NOT NULL DEFAULT 0,
                is_active INTEGER NOT NULL DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_cheatsheet_active ON cheatsheets(is_active);
            """
        )
        conn.commit()
    finally:
        conn.close()


def save_cheatsheet(
    context: CheatsheetContext,
    raw_text: str = "",
    name: str = "Active Cheatsheet",
    db_path: Path | str = DEFAULT_DB_PATH,
) -> int:
    """Persist parsed cheatsheet into SQLite, deactivating older active sheets."""
    init_db(db_path)
    parsed_str = context.model_dump_json(indent=2)
    player_count = len(context.entries)
    rule_count = len(context.strategy_rules)

    conn = get_db_connection(db_path)
    try:
        conn.execute("UPDATE cheatsheets SET is_active = 0 WHERE is_active = 1;")
        cursor = conn.execute(
            """
            INSERT INTO cheatsheets (name, raw_text, parsed_json, player_count, rule_count, is_active)
            VALUES (?, ?, ?, ?, ?, 1);
            """,
            (name, raw_text, parsed_str, player_count, rule_count),
        )
        conn.commit()
        return cursor.lastrowid or 1
    finally:
        conn.close()


def get_active_cheatsheet(db_path: Path | str = DEFAULT_DB_PATH) -> CheatsheetContext | None:
    """Fetch the latest active cheatsheet context from SQLite."""
    init_db(db_path)
    conn = get_db_connection(db_path)
    try:
        row = conn.execute(
            """
            SELECT parsed_json FROM cheatsheets
            WHERE is_active = 1
            ORDER BY id DESC LIMIT 1;
            """
        ).fetchone()

        if row and row["parsed_json"]:
            data = json.loads(row["parsed_json"])
            return CheatsheetContext.model_validate(data)
    except Exception as err:
        logger.warning("Failed to retrieve active cheatsheet from SQLite: %s", err)
    finally:
        conn.close()
    return None


def get_cheatsheet_history(db_path: Path | str = DEFAULT_DB_PATH) -> list[dict[str, Any]]:
    """Fetch history of all uploaded cheatsheets."""
    init_db(db_path)
    conn = get_db_connection(db_path)
    try:
        rows = conn.execute(
            """
            SELECT id, name, player_count, rule_count, is_active, created_at
            FROM cheatsheets
            ORDER BY id DESC;
            """
        ).fetchall()
        return [dict(r) for r in rows]
    except Exception as err:
        logger.warning("Failed to retrieve cheatsheet history from SQLite: %s", err)
        return []
    finally:
        conn.close()
