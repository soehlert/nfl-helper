"""Ingestion engine for web articles, ESPN rankings, and online fantasy cheatsheets."""

import logging
import re
from html.parser import HTMLParser

import httpx

from nfl_helper.core.cheatsheet import parse_plain_text_cheatsheet
from nfl_helper.models.cheatsheet import CheatsheetContext

logger = logging.getLogger(__name__)

USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"


class WebCheatsheetHTMLParser(HTMLParser):
    """HTML Parser extracting tables, headings, ordered lists, and strategy bullets from web articles."""

    def __init__(self) -> None:
        super().__init__()
        self.text_chunks: list[str] = []
        self.in_table = False
        self.in_row = False
        self.current_row: list[str] = []
        self.current_tag = ""
        self.article_title = "Online Cheatsheet"
        self.in_title = False
        self.title_chunks: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.current_tag = tag.lower()
        if self.current_tag in ("h1", "h2", "h3", "h4", "h5"):
            self.text_chunks.append("\n")
        elif self.current_tag == "table":
            self.in_table = True
        elif self.current_tag == "tr":
            self.in_row = True
            self.current_row = []
        elif self.current_tag == "title":
            self.in_title = True

    def handle_endtag(self, tag: str) -> None:
        tag_lower = tag.lower()
        if tag_lower in ("h1", "h2", "h3", "h4", "h5", "p", "li", "div"):
            self.text_chunks.append("\n")
        elif tag_lower == "tr":
            if self.current_row:
                row_str = " | ".join(self.current_row).strip()
                if row_str:
                    self.text_chunks.append(f"{row_str}\n")
            self.in_row = False
            self.current_row = []
        elif tag_lower == "table":
            self.in_table = False
            self.text_chunks.append("\n")
        elif tag_lower == "title":
            self.in_title = False
            if self.title_chunks:
                self.article_title = " ".join("".join(self.title_chunks).split()).strip()

    def handle_data(self, data: str) -> None:
        clean = data.strip()
        if not clean:
            return
        if self.in_title:
            self.title_chunks.append(clean)
        elif self.in_row:
            self.current_row.append(clean)
        else:
            self.text_chunks.append(f"{clean} ")

    def get_extracted_text(self) -> str:
        raw = "".join(self.text_chunks)
        # Normalize multiple spaces and excess blank lines
        lines = [re.sub(r"\s+", " ", ln).strip() for ln in raw.splitlines()]
        clean_lines = []
        for ln in lines:
            if ln:
                clean_lines.append(ln)
            elif clean_lines and clean_lines[-1] != "":
                clean_lines.append("")
        return "\n".join(clean_lines)


async def fetch_web_cheatsheet(url: str, timeout: float = 10.0) -> tuple[CheatsheetContext, str, str]:
    """Fetch web page from URL, strip HTML, extract tabular rankings and rules into CheatsheetContext."""
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
    }

    async with httpx.AsyncClient(headers=headers, follow_redirects=True, timeout=timeout) as client:
        res = await client.get(url)
        res.raise_for_status()
        html_content = res.text

    parser = WebCheatsheetHTMLParser()
    parser.feed(html_content)
    extracted_text = parser.get_extracted_text()
    title = parser.article_title or "Web Rankings"

    logger.info("Successfully fetched %s ('%s'): %d characters extracted", url, title, len(extracted_text))
    context = parse_plain_text_cheatsheet(extracted_text)
    return context, title, extracted_text
