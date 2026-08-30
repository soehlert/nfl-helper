"""Unit and integration tests for web URL cheatsheet parsing and API endpoints."""

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from nfl_helper.core.url_cheatsheet import WebCheatsheetHTMLParser, fetch_web_cheatsheet
from nfl_helper.main import app

client = TestClient(app)


def test_web_html_parser_tables_and_headings() -> None:
    """Verify WebCheatsheetHTMLParser extracts clean table rows, headings, and strategy rules."""
    sample_html = """
    <!DOCTYPE html>
    <html>
    <head><title>ESPN Fantasy Football 2024 Cheatsheet</title></head>
    <body>
      <h1>Top Redraft Rankings & Sleepers</h1>
      <h3>Tier 1: Elite Quarterbacks</h3>
      <table>
        <thead>
          <tr><th>Rank</th><th>Player</th><th>Position</th><th>Team</th><th>ADP</th></tr>
        </thead>
        <tbody>
          <tr><td>1</td><td>Josh Allen</td><td>QB</td><td>BUF</td><td>24.5</td></tr>
          <tr><td>2</td><td>Lamar Jackson</td><td>QB</td><td>BAL</td><td>28.0</td></tr>
        </tbody>
      </table>

      <h3>Tier 1: Running Backs</h3>
      <table>
        <tbody>
          <tr><td>1</td><td>Bijan Robinson</td><td>RB</td><td>ATL</td><td>2.2</td></tr>
          <tr><td>2</td><td>Breece Hall</td><td>RB</td><td>NYJ</td><td>8.5</td></tr>
        </tbody>
      </table>

      <h2>Draft Strategy Tips</h2>
      <ul>
        <li>Rounds 1-2: Target RB or WR</li>
        <li>Round 4: Target Tier 1 QB</li>
      </ul>
    </body>
    </html>
    """

    parser = WebCheatsheetHTMLParser()
    parser.feed(sample_html)
    text = parser.get_extracted_text()

    assert "Josh Allen" in text
    assert "Bijan Robinson" in text
    assert "Rounds 1-2: Target RB or WR" in text
    assert parser.article_title == "ESPN Fantasy Football 2024 Cheatsheet"


@pytest.mark.asyncio
async def test_fetch_web_cheatsheet_mocked() -> None:
    """Verify fetch_web_cheatsheet converts HTML response into CheatsheetContext cleanly."""
    mock_html = """
    <html>
    <head><title>Top Sleepers</title></head>
    <body>
      <table>
        <tr><td>Josh Allen</td><td>QB</td><td>BUF</td><td>25.0</td></tr>
        <tr><td>Bijan Robinson</td><td>RB</td><td>ATL</td><td>2.5</td></tr>
      </table>
      <p>Strategy: Wait on QB</p>
    </body>
    </html>
    """

    with patch("httpx.AsyncClient.get") as mock_get:
        from unittest.mock import MagicMock

        mock_resp = MagicMock()
        mock_resp.text = mock_html
        mock_resp.raise_for_status = MagicMock()
        mock_get.return_value = mock_resp

        context, _title, _raw_text = await fetch_web_cheatsheet("https://www.espn.com/fantasy/rankings")

        assert len(context.entries) >= 2
        assert "josh allen" in context.entries
        assert "bijan robinson" in context.entries
        assert "Wait on QB" in context.strategy_rules or "Strategy: Wait on QB" in context.strategy_rules


def test_api_upload_and_diff_web_url_endpoints() -> None:
    """Verify /api/cheatsheet/url and /api/cheatsheet/url-diff endpoints with mocked fetcher."""
    with patch("nfl_helper.main.fetch_web_cheatsheet") as mock_fetch:
        from nfl_helper.core.cheatsheet import parse_plain_text_cheatsheet

        mock_ctx = parse_plain_text_cheatsheet("1.1 Josh Allen\n1.2 Lamar Jackson\nRound 1: Target QB")
        mock_fetch.return_value = (mock_ctx, "ESPN Sleepers", "1.1 Josh Allen\n1.2 Lamar Jackson")

        # Test URL diff
        res_diff = client.post("/api/cheatsheet/url-diff", json={"url": "https://espn.com/sleepers"})
        assert res_diff.status_code == 200
        assert "top_risers" in res_diff.json()

        # Test URL upload
        res_upload = client.post("/api/cheatsheet/url", json={"url": "https://espn.com/sleepers"})
        assert res_upload.status_code == 200
        data = res_upload.json()
        assert len(data["entries"]) >= 2
