"""Pytest fixtures and configuration ensuring strict test database isolation."""

from pathlib import Path
from unittest.mock import patch

import pytest


@pytest.fixture(autouse=True)
def isolate_test_db(tmp_path: Path):
    """Automatically isolate all database interactions to a temporary sqlite database."""
    test_db = tmp_path / "test_isolated.db"
    with patch("nfl_helper.core.db.DEFAULT_DB_PATH", test_db):
        yield test_db
