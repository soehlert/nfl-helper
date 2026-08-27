"""Player name normalization for matching plain-text cheatsheets to player IDs."""

import re
import unicodedata

SUFFIX_PATTERN = re.compile(r"\b(jr\.?|sr\.?|ii|iii|iv|v)\b", re.IGNORECASE)
PUNCTUATION_PATTERN = re.compile(r"[\.\x27\`]")


def normalize_player_name(raw_name: str) -> str:
    """Normalize names by stripping accents, suffixes, and punctuation for reliable ID matching."""
    if not raw_name:
        return ""
    # Decompose unicode characters into base letters and diacritical marks
    decomposed = unicodedata.normalize("NFKD", raw_name)
    stripped_accents = "".join(c for c in decomposed if not unicodedata.combining(c))

    # Remove apostrophes and dots (e.g. De'Von -> Devon, A.J. -> AJ)
    cleaned = PUNCTUATION_PATTERN.sub("", stripped_accents)
    # Remove common suffixes
    without_suffixes = SUFFIX_PATTERN.sub("", cleaned)
    # Lowercase and standardize spacing around hyphens
    lowered = without_suffixes.lower()
    return re.sub(r"\s+", " ", lowered).strip()
