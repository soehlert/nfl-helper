import json
import logging
import os
import sqlite3
from pathlib import Path
from typing import Any

from nfl_helper.models.cheatsheet import CheatsheetContext

logger = logging.getLogger(__name__)

DEFAULT_DB_PATH = Path(__file__).resolve().parent.parent.parent.parent / "data" / "nfl_helper.db"


def _resolve_db_path(db_path: Path | str | None = None) -> Path:
    """Dynamically resolve database path supporting environment variables and test patching."""
    if db_path is not None:
        return Path(db_path)
    env_path = os.environ.get("NFL_HELPER_DB_PATH")
    if env_path:
        return Path(env_path)
    return DEFAULT_DB_PATH


def get_db_connection(db_path: Path | str | None = None) -> sqlite3.Connection:
    """Create or connect to SQLite database with foreign keys and Row factory enabled."""
    path = _resolve_db_path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn


def init_db(db_path: Path | str | None = None) -> None:
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
    layer_mode: bool = True,
    db_path: Path | str | None = None,
) -> int:
    """Persist parsed cheatsheet into SQLite. If layer_mode is False, deactivates older active sheets."""
    init_db(db_path)
    parsed_str = context.model_dump_json(indent=2)
    player_count = len(context.entries)
    rule_count = len(context.strategy_rules)

    conn = get_db_connection(db_path)
    try:
        if not layer_mode:
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


def get_active_cheatsheets(db_path: Path | str | None = None) -> list[CheatsheetContext]:
    """Fetch all active cheatsheet contexts in chronological order from SQLite."""
    init_db(db_path)
    conn = get_db_connection(db_path)
    try:
        rows = conn.execute(
            """
            SELECT parsed_json FROM cheatsheets
            WHERE is_active = 1
            ORDER BY id ASC;
            """
        ).fetchall()
        contexts: list[CheatsheetContext] = []
        for row in rows:
            if row["parsed_json"]:
                try:
                    contexts.append(CheatsheetContext.model_validate(json.loads(row["parsed_json"])))
                except Exception as parse_err:
                    logger.warning("Failed to validate active cheatsheet: %s", parse_err)
        return contexts
    except Exception as err:
        logger.warning("Failed to retrieve active cheatsheets from SQLite: %s", err)
        return []
    finally:
        conn.close()


def get_active_cheatsheet(db_path: Path | str | None = None) -> CheatsheetContext | None:
    """Fetch and consolidate all active cheatsheet contexts into a unified layered context."""
    from nfl_helper.core.cheatsheet import merge_cheatsheet_contexts

    active_sheets = get_active_cheatsheets(db_path)
    if not active_sheets:
        return None
    return merge_cheatsheet_contexts(active_sheets)


def get_cheatsheet_history(db_path: Path | str | None = None) -> list[dict[str, Any]]:
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


def toggle_cheatsheet_active(
    cheatsheet_id: int,
    active: bool | None = None,
    db_path: Path | str | None = None,
) -> bool:
    """Toggle or explicitly set the active status of a cheatsheet by ID."""
    init_db(db_path)
    conn = get_db_connection(db_path)
    try:
        if active is None:
            conn.execute(
                "UPDATE cheatsheets SET is_active = CASE WHEN is_active = 1 THEN 0 ELSE 1 END WHERE id = ?;",
                (cheatsheet_id,),
            )
        else:
            conn.execute(
                "UPDATE cheatsheets SET is_active = ? WHERE id = ?;",
                (1 if active else 0, cheatsheet_id),
            )
        conn.commit()
        return True
    finally:
        conn.close()


def clear_active_cheatsheet(db_path: Path | str | None = None) -> bool:
    """Deactivate or clear currently active cheatsheets from SQLite."""
    init_db(db_path)
    conn = get_db_connection(db_path)
    try:
        conn.execute("UPDATE cheatsheets SET is_active = 0 WHERE is_active = 1;")
        conn.commit()
        return True
    finally:
        conn.close()


def delete_cheatsheet(cheatsheet_id: int, db_path: Path | str | None = None) -> bool:
    """Permanently remove a cheatsheet by ID from SQLite."""
    init_db(db_path)
    conn = get_db_connection(db_path)
    try:
        conn.execute("DELETE FROM cheatsheets WHERE id = ?;", (cheatsheet_id,))
        conn.commit()
        return True
    finally:
        conn.close()


def activate_cheatsheet(cheatsheet_id: int, db_path: Path | str | None = None) -> CheatsheetContext | None:
    """Switch active status to a single saved cheatsheet (replacing others)."""
    init_db(db_path)
    conn = get_db_connection(db_path)
    try:
        conn.execute("UPDATE cheatsheets SET is_active = 0 WHERE is_active = 1;")
        conn.execute("UPDATE cheatsheets SET is_active = 1 WHERE id = ?;", (cheatsheet_id,))
        conn.commit()
        return get_active_cheatsheet(db_path)
    finally:
        conn.close()


def delete_all_cheatsheets(db_path: Path | str | None = None) -> bool:
    """Permanently delete all cheatsheet records from SQLite."""
    init_db(db_path)
    conn = get_db_connection(db_path)
    try:
        conn.execute("DELETE FROM cheatsheets;")
        conn.commit()
        return True
    finally:
        conn.close()
