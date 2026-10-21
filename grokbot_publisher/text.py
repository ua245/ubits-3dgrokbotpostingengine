"""Caption rules: length counting, hashtag placement and trimming.

Each platform counts length differently, so each gets its own counter:

* X counts "weighted" characters (most CJK and emoji count double, every
  link counts as 23), max 280.
* TikTok and Instagram count UTF-16 code units.
* YouTube descriptions are limited in UTF-8 *bytes*.

Trimming order is fixed: drop hashtags from the end first, then shorten the
body at a word boundary and add an ellipsis. Every change is recorded in
`notes` so the report shows exactly what was posted vs. what was approved.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Callable, Iterable

ELLIPSIS = "…"

# Bare domains are counted as links by X too ("ubits.tech" costs 23 chars and the
# link price), so detect the common TLDs as well as full URLs.
_TLDS = (
    "com|net|org|io|ai|co|tech|dev|app|xyz|me|tv|uk|us|eu|de|fr|in|pk|ae|ly|gg|so|sh|to|cc|info|biz|site|online|link"
)
URL_RE = re.compile(
    r"(?i)\b(?:https?://[^\s<>\"']+|(?:www\.)?[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9-]+)*\.(?:"
    + _TLDS
    + r")\b(?:/[^\s<>\"']*)?)"
)
HASHTAG_RE = re.compile(r"(?<![\w&])#(\w+)", re.UNICODE)


def utf16_len(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def utf8_len(text: str) -> int:
    return len(text.encode("utf-8"))


def codepoint_len(text: str) -> int:
    return len(text)


# twitter-text v3 weights: these ranges count 1, everything else counts 2.
_X_LIGHT = ((0, 4351), (8192, 8205), (8208, 8223), (8242, 8247))
_ZERO_WIDTH_IN_EMOJI = {0x200D, 0xFE0E, 0xFE0F, 0x20E3}


def _x_weight(cp: int) -> int:
    for lo, hi in _X_LIGHT:
        if lo <= cp <= hi:
            return 1
    return 2


def x_weighted_len(text: str) -> int:
    """Approximate twitter-text weighted length (errs on the long side)."""
    total = 0
    pos = 0
    for match in URL_RE.finditer(text):
        total += _x_plain_len(text[pos:match.start()])
        total += 23
        pos = match.end()
    return total + _x_plain_len(text[pos:])


def _x_plain_len(text: str) -> int:
    total = 0
    after_zwj = False
    for ch in text:
        cp = ord(ch)
        if after_zwj:
            after_zwj = False
            continue  # the joined emoji is part of the previous glyph
        if cp in _ZERO_WIDTH_IN_EMOJI or 0x1F3FB <= cp <= 0x1F3FF:
            after_zwj = cp == 0x200D
            continue
        total += _x_weight(cp)
    return total


def find_urls(text: str) -> list[str]:
    return [m.group(0) for m in URL_RE.finditer(text)]


def normalize_tag(tag: str) -> str:
    tag = tag.strip().lstrip("#")
    return "".join(ch for ch in tag if ch == "_" or unicodedata.category(ch)[0] in "LN")


def existing_tags(text: str) -> set[str]:
    return {t.lower() for t in HASHTAG_RE.findall(text)}


def clean_body(text: str) -> str:
    text = (text or "").replace("\r\n", "\n").strip()
    return re.sub(r"\n{3,}", "\n\n", text)


@dataclass
class Rendered:
    text: str
    hashtags: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    trimmed: bool = False


def compose(
    body: str,
    hashtags: Iterable[str] = (),
    *,
    limit: int,
    counter: Callable[[str], int] = utf16_len,
    max_tags: int | None = None,
    separator: str = "\n\n",
    policy: str = "trim",
    label: str = "caption",
) -> Rendered:
    """Build the final caption: body + hashtags at the end, within `limit`."""
    body = clean_body(body)
    hashtags = list(hashtags or ())
    notes: list[str] = []
    present = existing_tags(body)
    tags: list[str] = []
    for raw in hashtags:
        tag = normalize_tag(raw)
        if tag and tag.lower() not in present and tag.lower() not in {t.lower() for t in tags}:
            tags.append(tag)
    if max_tags is not None:
        room = max(0, max_tags - len(present))
        if len(tags) > room:
            notes.append(f"{label}: dropped {len(tags) - room} hashtag(s) over the limit of {max_tags}")
            tags = tags[:room]

    def build(b: str, t: list[str]) -> str:
        if not t:
            return b
        line = " ".join(f"#{x}" for x in t)
        return f"{b}{separator}{line}" if b else line

    text = build(body, tags)
    if counter(text) <= limit:
        return Rendered(text=text, hashtags=tags, notes=notes)

    if policy == "strict":
        from grokbot_publisher.errors import ValidationError

        raise ValidationError(f"{label} is {counter(text)} long; the limit is {limit} (caption policy: strict)")

    original_len = counter(text)
    kept = list(tags)
    while kept and counter(build(body, kept)) > limit:
        kept.pop()
    if len(kept) < len(tags):
        notes.append(f"{label}: dropped {len(tags) - len(kept)} trailing hashtag(s) to fit {limit}")
    if counter(build(body, kept)) <= limit:
        return Rendered(text=build(body, kept), hashtags=kept, notes=notes, trimmed=True)

    body = trim_to(body, limit, counter)
    notes.append(f"{label}: trimmed from {original_len} to {counter(body)} (limit {limit})")
    return Rendered(text=body, hashtags=[], notes=notes, trimmed=True)


def trim_to(text: str, limit: int, counter: Callable[[str], int] = utf16_len) -> str:
    """Shorten to fit `limit`, preferring a word boundary, ending with an ellipsis."""
    if counter(text) <= limit:
        return text
    if limit <= 1:
        return ELLIPSIS[:limit]
    # Binary search for the longest prefix that fits with the ellipsis.
    lo, hi = 0, len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if counter(text[:mid].rstrip() + ELLIPSIS) <= limit:
            lo = mid
        else:
            hi = mid - 1
    cut = text[:lo]
    boundary = max(cut.rfind(" "), cut.rfind("\n"))
    if boundary >= int(len(cut) * 0.6):
        cut = cut[:boundary]
    # Never leave a dangling combining mark or half an emoji sequence.
    while cut and (unicodedata.combining(cut[-1]) or ord(cut[-1]) in _ZERO_WIDTH_IN_EMOJI):
        cut = cut[:-1]
    return cut.rstrip(" \n,;:-") + ELLIPSIS


# LinkedIn "little text": these characters must be backslash-escaped or the
# post is rejected or silently truncated (parentheses are the classic trap).
_LITTLE_RESERVED = set("\\|{}@[]()<>#*_~")


def linkedin_escape(text: str) -> str:
    """Escape commentary for the LinkedIn Posts API and turn #tags into
    hashtag templates so they still render as real hashtags."""
    out: list[str] = []
    i = 0
    while i < len(text):
        ch = text[i]
        if ch == "#":
            match = HASHTAG_RE.match(text, i)
            prev = text[i - 1] if i else " "
            if match and not (prev.isalnum() or prev in "&_"):
                out.append("{hashtag|\\#|" + match.group(1).replace("_", "\\_") + "}")
                i = match.end()
                continue
        out.append("\\" + ch if ch in _LITTLE_RESERVED else ch)
        i += 1
    return "".join(out)


def youtube_safe(text: str) -> str:
    """YouTube rejects '<' and '>' in titles and descriptions."""
    return text.replace("<", "‹").replace(">", "›")
