"""
Prompt Validator — Matches "Expect to Hear" prompts (with tags) against actual prompts.

Supported tags (any combination):
  {Date}                     Spoken dates
  {Time}                     Spoken times
  {Number}                   Naturally spoken numbers
  {Digits}                   Individually spoken digits (1-20)
  {Digits Length=x}          Exactly x spoken digits
  {Digits Length=x-y}        Between x and y spoken digits
  {Currency}                 Spoken currency (language-aware)
  {AlphaNum}                 Alphanumeric sequence (1-20 tokens)
  {AlphaNum Length=x}        Exactly x alphanumeric tokens
  {AlphaNum Length=x-y}      Between x and y alphanumeric tokens
  {Choice a|b|c}             One of several alternatives
  {Choice x=a:1|b:2|c:3}    Choice with variable capture → result.captured_variables
  {Optional text}            Phrase is optional (not scored)
  {*} / {wildcard}           Wildcard — absorbs text up to the next anchor (both spellings are identical)
  {BypassRecognition}        Always returns 100% match, ignores everything after it
  {BargeIn}                  Text before this tag is matched normally; template text after
                              it is dropped entirely (no audio exists past the barge-in point)

match_percentage = matched_units / total_units × 100
  • {Optional} and {*}/{wildcard} are not counted in total_units
  • {BypassRecognition} short-circuits to 100% immediately
  • {BargeIn} truncates the contract — only text before it is ever scored
  • Choice variable=value pairs are returned in result.captured_variables
"""

import re
import sys
from dataclasses import dataclass, field


# ──────────────────────────────────────────────────────────────────────────────
# Vocabulary building blocks
# ──────────────────────────────────────────────────────────────────────────────

_NUM_WORD = (
    r"(?:zero|one|two|three|four|five|six|seven|eight|nine|ten"
    r"|eleven|twelve|thirteen|fourteen|fifteen|sixteen|seventeen"
    r"|eighteen|nineteen|twenty|thirty|forty|fifty|sixty|seventy"
    r"|eighty|ninety|hundred|thousand|million|billion|and)"
)
_NUM_W_OR_D = rf"(?:{_NUM_WORD}|\d+)"

_DIGIT_WORDS = (
    r"(?:zero|one|two|three|four|five|six|seven|eight|nine|oh"
    r"|cero|uno|dos|tres|cuatro|cinco|seis|siete|ocho|nueve"
    r"|nul|een|twee|drie|vier|vijf|zes|zeven|acht|negen)"
)
_DIGIT_TOKEN = rf"(?:{_DIGIT_WORDS}|\d)"

# Single-token regexes used by the exact-count scorer
_DIGIT_TOKEN_RE  = re.compile(
    r"^(?:zero|one|two|three|four|five|six|seven|eight|nine|oh"
    r"|cero|uno|dos|tres|cuatro|cinco|seis|siete|ocho|nueve"
    r"|nul|een|twee|drie|vier|vijf|zes|zeven|acht|negen|\d+)$",
    re.IGNORECASE,
)
_ALPHANUM_TOKEN_RE = re.compile(r"^[A-Za-z0-9]+$")

_NUMBER = (
    r"(?:(?:minus|negative|plus)\s+)?"
    rf"(?:{_NUM_WORD}|\d+)"
    rf"(?:\s+(?:{_NUM_WORD}|\d+))*"
    r"(?:\s+point\s+(?:\w+\s*)+)?"
)

_MONTH   = r"(?:january|february|march|april|may|june|july|august|september|october|november|december)"
_WEEKDAY = r"(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday)"
_ORD_W   = (
    r"(?:first|second|third|fourth|fifth|sixth|seventh|eighth|ninth|tenth"
    r"|eleventh|twelfth|thirteenth|fourteenth|fifteenth|sixteenth|seventeenth"
    r"|eighteenth|nineteenth|twentieth"
    r"|twenty[-\s]first|twenty[-\s]second|twenty[-\s]third|twenty[-\s]fourth"
    r"|twenty[-\s]fifth|twenty[-\s]sixth|twenty[-\s]seventh|twenty[-\s]eighth"
    r"|twenty[-\s]ninth|thirtieth|thirty[-\s]first)"
)
_ORD  = rf"(?:\d{{1,2}}(?:st|nd|rd|th)?|{_ORD_W})"
# _YEAR = r"(?:\d{4}|(?:nineteen|twenty)\s+\w+(?:\s+\w+)?)"

_YEAR_UNIT = (
    r"(?:zero|one|two|three|four|five|six|seven|eight|nine"
    r"|ten|eleven|twelve|thirteen|fourteen|fifteen|sixteen|seventeen"
    r"|eighteen|nineteen|twenty|thirty|forty|fifty|sixty|seventy"
    r"|eighty|ninety)"
)

_YEAR = (
    r"(?:"
    r"\d{4}"
    rf"|(?:nineteen|twenty)\s+{_YEAR_UNIT}(?:\s+{_YEAR_UNIT})?"
    rf"|two\s+thousand(?:\s+and)?(?:\s+{_YEAR_UNIT}){{0,2}}"
    rf"|twenty\s+twenty(?:\s+{_YEAR_UNIT})?"
    r")"
)

_DATE = "(?:" + "|".join([
    rf"(?:{_WEEKDAY}\s+)?(?:the\s+)?{_ORD}\s+of\s+{_MONTH}(?:\s+{_YEAR})?",
    rf"(?:{_WEEKDAY}\s+)?{_MONTH}\s+{_ORD}(?:\s+{_YEAR})?",
    rf"{_WEEKDAY}\s+{_MONTH}\s+{_ORD}(?:\s+{_YEAR})?",
    rf"(?:today|tomorrow|yesterday|(?:this|next|last|previous)\s+{_WEEKDAY})",
    r"\d{1,2}[\/\-]\d{1,2}(?:[\/\-]\d{2,4})?",
    _MONTH,
    rf"{_WEEKDAY}(?:\s+the\s+{_ORD})?",
    r"\d{1,2}\s+\d{1,2}",
    _YEAR,
]) + ")"

_TIME = "(?:" + "|".join([
    r"(?:noon|midnight|midday)",
    r"(?:before|after)\s+\w+(?:\s+\w+)?",
    r"\d{1,2}:\d{2}\s*(?:am|pm)?",
    r"\d{1,2}\s*(?:am|pm)",
    r"(?:half\s+past|quarter\s+(?:past|to))\s+\w+",
    r"\d{1,2}\s+hundred\s+hours?",
    r"twenty[-\s]four\s+hundred(?:\s+hours?)?",
    r"\d{1,2}\s+o'clock",
    (
        r"(?:(?:one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve"
        r"|thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|twenty"
        r"|twenty[-\s]\w+|thirty)\s+)+"
        r"(?:am|pm|in\s+the\s+(?:morning|afternoon|evening))?"
    ),
]) + ")"

CURRENCY_PATTERNS: dict = {
    "en-AU": [
        rf"{_NUM_W_OR_D}(?:\s+{_NUM_WORD})*\s+(?:australian\s+)?dollars?\s+and\s+{_NUM_W_OR_D}(?:\s+{_NUM_WORD})*\s+cents?",
    ],
    "en-GB": [
        rf"{_NUM_W_OR_D}(?:\s+{_NUM_WORD})*\s+pounds?\s+and\s+{_NUM_W_OR_D}(?:\s+{_NUM_WORD})*\s+(?:pence|penny)",
        rf"{_NUM_W_OR_D}(?:\s+{_NUM_WORD})*\s+euros?\s+and\s+{_NUM_W_OR_D}(?:\s+{_NUM_WORD})*\s+cents?",
    ],
    "en-US": [
        rf"{_NUM_W_OR_D}(?:\s+{_NUM_WORD})*\s+(?:u\.?s\.?\s+)?dollars?\s+and\s+{_NUM_W_OR_D}(?:\s+{_NUM_WORD})*\s+cents?",
        rf"{_NUM_W_OR_D}(?:\s+{_NUM_WORD})*\s+dollars?\s+{_NUM_W_OR_D}(?:\s+{_NUM_WORD})*\s+cents?",
        rf"{_NUMBER}\s+(?:u\.?s\.?\s+)?dollars?",   # add this
    ],
    "es-US": [
        r"(?:\w+)(?:\s+\w+)*\s+(?:pesos?|d(?:o|ó)lares?)\s+y\s+(?:\w+)(?:\s+\w+)*\s+centavos?",
    ],
    "nl-NL": [
        r"(?:\w+)(?:\s+\w+)*\s+euro\s+en\s+(?:\w+)(?:\s+\w+)*\s+cent",
    ],
}

# _CURRENCY_GENERIC = (
#     r"(?:"
#     rf"{_NUM_W_OR_D}(?:\s+{_NUM_WORD})*"
#     r"\s+(?:dollars?|euros?|pounds?|pesos?|yen|francs?|rupees?|kroner?)"
#     r"(?:\s+and\s+\w+(?:\s+\w+)*\s+(?:cents?|pence|penny|centavos?|centimes?))?"
#     rf"|{_NUM_W_OR_D}(?:\s+{_NUM_WORD})*"   # bare amount (backtrack target)
#     r")"
# )

_CURRENCY_GENERIC = (
    r"(?:"
    rf"{_NUMBER}\s+(?:dollars?|euros?|pounds?|pesos?|yen|francs?|rupees?|kroner?)"
    r"(?:\s+and\s+"
    rf"{_NUMBER}\s+(?:cents?|pence|penny|centavos?|centimes?))?"
    r"|"
    rf"{_NUMBER}"
    r")"
)

# ──────────────────────────────────────────────────────────────────────────────
# Exact-count helpers for Digits and AlphaNum
# ──────────────────────────────────────────────────────────────────────────────

def _count_leading_tokens(text: str, token_re: re.Pattern) -> tuple:
    """
    Count how many leading whitespace-separated tokens satisfy token_re.
    Returns (count, all_tokens_list).
    """
    tokens = text.strip().split()
    count = 0
    for tok in tokens:
        if token_re.match(tok):
            count += 1
        else:
            break
    return count, tokens


def _match_exact_tokens(remaining: str, lo: int, hi: int,
                        token_re: re.Pattern, sep: str = r"\s+") -> str | None:
    """
    Match exactly lo..hi leading tokens from *remaining*.
    Returns the matched text, or None if count is out of range or more tokens follow.
    The 'more tokens follow' check uses token_re so only same-type extras are penalised.
    """
    count, tokens = _count_leading_tokens(remaining, token_re)
    if not (lo <= count <= hi):
        return None
    matched_text = (" " if sep == r"\s+" else "").join(tokens[:count])
    # Ensure no additional same-type token immediately follows
    rest_tokens = tokens[count:]
    if rest_tokens and token_re.match(rest_tokens[0]):
        return None   # too many tokens
    return matched_text


# ──────────────────────────────────────────────────────────────────────────────
# Choice tag parser — extracts (varname, [(phrase, value), ...])
# ──────────────────────────────────────────────────────────────────────────────

def _parse_choice(body: str) -> tuple:
    """
    Parse the body of a Choice tag (everything after 'CHOICE ').
    Returns (varname: str|None, alternatives: list[(phrase, value)]).
    Variable values default to the phrase itself if not explicitly set.
    """
    varname = None
    m = re.match(r"^(\w+)=(.+)$", body, re.DOTALL)
    if m:
        varname = m.group(1)
        body = m.group(2)

    alternatives = []
    for alt in body.split("|"):
        # Split "phrase:value" — value part is optional
        parts = re.split(r":(\S+)$", alt.strip())
        phrase = parts[0].strip()
        value  = parts[1].strip() if len(parts) > 1 else phrase
        alternatives.append((phrase, value))
    return varname, alternatives


# ──────────────────────────────────────────────────────────────────────────────
# Tokeniser
# ──────────────────────────────────────────────────────────────────────────────

_TAG_SPLIT = re.compile(r"(\{[^}]+\})")


def _tokenise(template: str) -> list:
    tokens = []
    for part in _TAG_SPLIT.split(template):
        if not part:
            continue
        if part.startswith("{") and part.endswith("}"):
            tokens.append(("tag", part[1:-1].strip()))
        else:
            tokens.append(("literal", part))
    return tokens


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────

def _literal_to_regex(text: str) -> str:
    esc = re.escape(text)
    esc = re.sub(r"\\ +", r"\\s+", esc)
    return esc


def _is_bargein_tag(tag_value: str) -> bool:
    """
    Return True for BargeIn control tags, including attributes.

    Supported examples:
      {BargeIn}
      {BargeIn after=7.9}
      {barge-in after=12.5}
      {BargeIn timeout=5}
    """
    normalized = tag_value.strip()

    # Match the tag name only; anything after it is treated as attributes.
    return bool(
        re.match(
            r"^BARGE[\s\-]*IN(?:\s+.*)?$",
            normalized,
            re.IGNORECASE,
        )
    )


def _tag_to_regex(tag: str, language: str) -> str:
    """Convert tag content (without braces) to a regex fragment."""
    t  = tag.strip()
    tu = t.upper()

    if t == "*" or tu == "WILDCARD":
        return r"(?:.*?)"
    if tu == "DATE":
        return _DATE
    if tu == "TIME":
        return _TIME
    if tu == "NUMBER":
        return rf"(?:{_NUMBER})"
    if tu == "CURRENCY":
        pats = CURRENCY_PATTERNS.get(language, []) + [_CURRENCY_GENERIC]
        return "(?:" + "|".join(f"(?:{p})" for p in pats) + ")"
    if tu == "BYPASSRECOGNITION":
        return r"(?:.*)"   # matches everything (used in full-regex path)
    if _is_bargein_tag(t):
        return r"(?:.*)"   # safety fallback; main paths handle {BargeIn} before reaching here

    m = re.match(r"DIGITS(?:\s+LENGTH=(\d+)(?:-(\d+))?)?$", tu)
    if m:
        lo = int(m.group(1)) if m.group(1) else 1
        hi = int(m.group(2)) if m.group(2) else (lo if m.group(1) else 20)
        # Accept both spoken-separated digits (1 2 3 4) and compact digit
        # strings (1234). Exact overlength is enforced by the anchored regex
        # here and by the partial scorer.
        return rf"(?:(?:\d{{{lo},{hi}}})|(?:{_DIGIT_TOKEN}(?:\s+{_DIGIT_TOKEN}){{{lo - 1},{hi - 1}}}))"

    m = re.match(r"ALPHANUM(?:\s+LENGTH=(\d+)(?:-(\d+))?)?(?:\s+\$\w+.*)?$", tu)
    if m:
        lo = int(m.group(1)) if m.group(1) else 1
        hi = int(m.group(2)) if m.group(2) else (lo if m.group(1) else 20)
        # Accept both compact IDs (ABC123) and spoken-separated IDs
        # (A B C 1 2 3).
        return rf"(?:(?:[A-Za-z0-9]{{{lo},{hi}}})|(?:[A-Za-z0-9](?:\s+[A-Za-z0-9]){{{lo - 1},{hi - 1}}}))"

    m = re.match(r"OPTIONAL\s+(.+)$", t, re.IGNORECASE)
    if m:
        inner = _literal_to_regex(m.group(1).strip())
        return rf"(?:{inner})?"

    m = re.match(r"CHOICE\s+(.+)$", t, re.IGNORECASE)
    if m:
        _, alts = _parse_choice(m.group(1).strip())
        parts = []
        for phrase, _ in alts:
            parts.append("" if phrase == "." else _literal_to_regex(phrase))
        return "(?:" + "|".join(f"(?:{p})" for p in parts) + ")"

    return r"(?:.*?)"   # unknown tag → wildcard


# ──────────────────────────────────────────────────────────────────────────────
# Full-template regex  (exact / 100% matching)
# ──────────────────────────────────────────────────────────────────────────────

def build_regex(template: str, language: str = "en-US") -> str:
    """
    Convert a full Expect-to-Hear template into a single anchored regex.
    {BypassRecognition} short-circuits: everything from that tag onward is ignored.
    """
    tokens = _tokenise(template)
    frags  = []

    for kind, value in tokens:
        # BypassRecognition: anchor what came before, match rest with .*
        if kind == "tag" and value.strip().upper() == "BYPASSRECOGNITION":
            frags.append(r"(?:.*)")
            break
        # BargeIn: text before the tag must match normally. Everything after
        # the tag in both the template and the actual transcript is ignored.
        # The trailing .* lets an already-buffered STT transcript contain words
        # beyond the barge-in point without causing the validation to fail.
        if kind == "tag" and _is_bargein_tag(value):
            frags.append(r"(?:.*)")
            break
        if kind == "tag":
            frags.append(_tag_to_regex(value, language))
        else:
            stripped = value.strip()
            if stripped:
                frags.append(rf"(?:\s*{_literal_to_regex(stripped)}\s*)")
            else:
                frags.append(r"\s*")

    inner = r"\s*".join(frags)
    return rf"(?i)^\s*{inner}\s*$"


# ──────────────────────────────────────────────────────────────────────────────
# Scored unit extraction  (for partial matching)
# ──────────────────────────────────────────────────────────────────────────────

def _is_scoreable_literal(text: str) -> bool:
    return bool(re.search(r"\w", text, re.UNICODE))


_WORD_STRIP_CHARS = " \t\r\n.,;:!?()[]{}<>\\/\"'“”‘’`~@#$%^&*-_=+|"

def _word_tokens(text: str) -> list[str]:
    """
    Whitespace tokenizer that preserves non-English words such as Hindi.
    We strip common punctuation from token edges but do not use \\w for token
    extraction, because Python's \\w can split Indic words at vowel signs.
    """
    words = []
    for raw in text.split():
        tok = raw.strip(_WORD_STRIP_CHARS)
        if tok:
            words.append(tok)
    return words


def _literal_words(text: str) -> list[str]:
    """
    Split a literal template span into individual words.

    This is intentionally word-level. A literal span like
    "you have selected account information" must not be scored as one
    all-or-nothing unit.
    """
    return _word_tokens(text)


def _word_regex(word: str) -> re.Pattern:
    """
    Regex for a single literal word with Unicode-aware non-word boundaries.
    This avoids matching "one" inside "someone".
    """
    return re.compile(r"(?<!\w)" + re.escape(word) + r"(?!\w)", re.IGNORECASE | re.UNICODE)


def _count_words(text: str) -> int:
    """Count actual words, including non-English words such as Hindi."""
    return len(_word_tokens(text))


def _add_unexpected_extra(breakdown: list, skipped_text: str) -> int:
    """
    Add penalty units for words skipped before the next expected unit.
    Returns how many extra-word penalty units were added.
    """
    words = _word_tokens(skipped_text)
    for word in words:
        breakdown.append((f'extra "{word}"', False, word))
    return len(words)


def _extract_scored_units(template: str, language: str) -> list:
    """
    Return ordered list of dicts, one per scoreable unit:
      label       — human-readable name
      regex       — compiled Pattern (used for non-length-constrained tags)
      optional    — bool: True for {Optional}, {*}, {BypassRecognition}
      kind        — "literal" | "tag"
      tag_type    — for tags: "DIGITS"|"ALPHANUM"|"CHOICE"|"WILDCARD"|"BYPASS"|"BARGEIN"|"OTHER"
      lo, hi      — for DIGITS/ALPHANUM: length range
      choice_meta — for CHOICE: (varname, [(phrase, value), ...])
    """
    tokens = _tokenise(template)
    units  = []

    for kind, value in tokens:
        if kind == "literal":
            if not _is_scoreable_literal(value):
                continue

            # Score literal text at word level, not as one all-or-nothing span.
            for word in _literal_words(value):
                units.append({
                    "label":    f'literal "{word}"',
                    "regex":    _word_regex(word),
                    "optional": False,
                    "kind":     "literal",
                    "tag_type": "LITERAL_WORD",
                    "word":     word,
                })
            continue

        # ── tag ───────────────────────────────────────────────────────────────
        t  = value.strip()
        tu = t.upper()

        # BypassRecognition: stop processing — all remaining units are skipped
        if tu == "BYPASSRECOGNITION":
            units.append({
                "label":    "{BypassRecognition}",
                "regex":    re.compile(r".*", re.IGNORECASE),
                "optional": True,
                "kind":     "tag",
                "tag_type": "BYPASS",
            })
            break   # nothing after BypassRecognition is scored

        # BargeIn: everything before this tag is matched/scored normally;
        # any template text after it is dropped — there's no audio for it,
        # since recognition stops the moment the barge-in timer expires.
        if _is_bargein_tag(t):
            units.append({
                "label":    "{" + t + "}",
                "regex":    None,
                "optional": True,
                "kind":     "tag",
                "tag_type": "BARGEIN",
            })
            break   # nothing after {BargeIn} is scored

        is_optional = tu.startswith("OPTIONAL") or t == "*" or tu == "WILDCARD"

        # Digits with length constraints
        md = re.match(r"DIGITS(?:\s+LENGTH=(\d+)(?:-(\d+))?)?$", tu)
        if md:
            lo = int(md.group(1)) if md.group(1) else 1
            hi = int(md.group(2)) if md.group(2) else (lo if md.group(1) else 20)
            units.append({
                "label":    "{" + t + "}",
                "regex":    None,   # use exact-count logic, not regex search
                "optional": False,
                "kind":     "tag",
                "tag_type": "DIGITS",
                "lo": lo, "hi": hi,
            })
            continue

        # AlphaNum with length constraints
        ma = re.match(r"ALPHANUM(?:\s+LENGTH=(\d+)(?:-(\d+))?)?(?:\s+\$\w+.*)?$", tu)
        if ma:
            lo = int(ma.group(1)) if ma.group(1) else 1
            hi = int(ma.group(2)) if ma.group(2) else (lo if ma.group(1) else 20)
            units.append({
                "label":    "{" + t + "}",
                "regex":    None,
                "optional": False,
                "kind":     "tag",
                "tag_type": "ALPHANUM",
                "lo": lo, "hi": hi,
            })
            continue

        # Choice — store metadata for variable capture
        mc = re.match(r"CHOICE\s+(.+)$", t, re.IGNORECASE)
        if mc:
            varname, alts = _parse_choice(mc.group(1).strip())
            frag = _tag_to_regex(t, language)
            units.append({
                "label":       "{" + t + "}",
                "regex":       re.compile(frag, re.IGNORECASE),
                "optional":    False,
                "kind":        "tag",
                "tag_type":    "CHOICE",
                "choice_meta": (varname, alts),
            })
            continue

        # Wildcard
        if t == "*" or tu == "WILDCARD":
            units.append({
                "label":    "{" + t + "}",
                "regex":    re.compile(r".*?", re.IGNORECASE),
                "optional": True,
                "kind":     "tag",
                "tag_type": "WILDCARD",
            })
            continue

        # All other tags
        frag = _tag_to_regex(t, language)
        units.append({
            "label":    "{" + t + "}",
            "regex":    re.compile(frag, re.IGNORECASE),
            "optional": is_optional,
            "kind":     "tag",
            "tag_type": "OTHER",
        })

    return units


# ──────────────────────────────────────────────────────────────────────────────
# Partial match scorer  (with wildcard anchoring + exact-count enforcement)
# ──────────────────────────────────────────────────────────────────────────────


def _actual_token_spans(text: str) -> list[tuple[str, int, int]]:
    """
    Return comparable actual tokens with their character spans in the original text.
    This preserves Hindi / non-English words by splitting on whitespace and
    trimming only edge punctuation.
    """
    spans = []
    for m in re.finditer(r"\S+", text):
        raw = m.group(0)
        left = 0
        right = len(raw)
        while left < right and raw[left] in _WORD_STRIP_CHARS:
            left += 1
        while right > left and raw[right - 1] in _WORD_STRIP_CHARS:
            right -= 1
        if left < right:
            spans.append((raw[left:right], m.start() + left, m.start() + right))
    return spans


def _token_text_from_span(actual: str, spans: list[tuple[str, int, int]], start_idx: int, end_idx: int) -> str:
    if start_idx >= end_idx or start_idx >= len(spans):
        return ""
    return actual[spans[start_idx][1]:spans[end_idx - 1][2]]


def _char_end_to_token_index(spans: list[tuple[str, int, int]], start_idx: int, char_end: int) -> int:
    """Return the first token index after char_end."""
    k = start_idx
    while k < len(spans) and spans[k][1] < char_end:
        k += 1
    return k


def _expanded_token_len(token: str, tag_type: str, token_re: re.Pattern) -> int | None:
    """
    Length contribution for exact-count tags.
    DIGITS: 123456 counts as six digits; one/two/etc count as one.
    ALPHANUM: ABC123 counts as six alphanumeric symbols; A/B/1 count as one.
    """
    stripped = token.strip(_WORD_STRIP_CHARS)
    if tag_type == "DIGITS":
        if stripped.isdigit():
            return len(stripped)
        return 1 if token_re.match(stripped) else None
    if tag_type == "ALPHANUM":
        # Treat compact IDs like ABC123 / A1B2 and spoken characters like
        # A B C 1 2 3 as alphanumeric units, but do not let ordinary words
        # such as "is" or "code" satisfy {AlphaNum}.
        if re.fullmatch(r"[A-Za-z0-9]+", stripped):
            if len(stripped) == 1 or any(ch.isdigit() for ch in stripped) or stripped.isupper():
                return len(stripped)
        return None
    return None



def _invalid_exact_token_run_at(unit: dict, spans: list[tuple[str, int, int]], j: int) -> tuple[int, str] | None:
    """
    If an exact-count tag is facing a same-type run with the wrong length,
    consume that whole run as a single failed tag unit. This prevents the
    aligner from turning a bad PIN/code into many unrelated extras or from
    matching a suffix of the run.
    """
    if j >= len(spans):
        return None
    tag_type = unit["tag_type"]
    token_re = _DIGIT_TOKEN_RE if tag_type == "DIGITS" else _ALPHANUM_TOKEN_RE
    k = j
    count = 0
    while k < len(spans):
        n = _expanded_token_len(spans[k][0], tag_type, token_re)
        if n is None:
            break
        count += n
        k += 1
    if k > j and not (unit["lo"] <= count <= unit["hi"]):
        return k, _token_text_from_span(_CURRENT_ACTUAL_TEXT, spans, j, k)
    return None

def _match_exact_token_unit_at(unit: dict, spans: list[tuple[str, int, int]], j: int) -> tuple[int, str] | None:
    tag_type = unit["tag_type"]
    lo, hi = unit["lo"], unit["hi"]
    token_re = _DIGIT_TOKEN_RE if tag_type == "DIGITS" else _ALPHANUM_TOKEN_RE

    k = j
    count = 0
    while k < len(spans):
        n = _expanded_token_len(spans[k][0], tag_type, token_re)
        if n is None:
            break
        count += n
        k += 1

    if lo <= count <= hi:
        return k, _token_text_from_span(_CURRENT_ACTUAL_TEXT, spans, j, k)

    # Do not match a suffix of an overlong run. The entire run fails.
    return None


def _match_regex_unit_at(unit: dict, actual: str, spans: list[tuple[str, int, int]], j: int) -> tuple[int, str] | None:
    if j >= len(spans):
        return None
    regex = unit.get("regex")
    if regex is None:
        return None

    start_char = spans[j][1]
    remaining = actual[start_char:]
    m = regex.match(remaining)
    if not m:
        return None

    text = m.group(0).strip()
    if not text:
        return j, ""

    end_char = start_char + m.end()
    next_j = _char_end_to_token_index(spans, j, end_char)
    if next_j == j:
        next_j = j + 1
    return next_j, actual[start_char:end_char].strip()


def _match_choice_unit_at(unit: dict, actual: str, spans: list[tuple[str, int, int]], j: int) -> tuple[int, str, str | None] | None:
    varname, alts = unit["choice_meta"]

    # Empty alternative.
    for phrase, value in alts:
        if phrase == ".":
            return j, "", value

    if j >= len(spans):
        return None

    start_char = spans[j][1]
    remaining = actual[start_char:]
    best = None
    for phrase, value in alts:
        pat = re.compile(_literal_to_regex(phrase), re.IGNORECASE | re.UNICODE)
        m = pat.match(remaining)
        if m:
            text = m.group(0)
            end_char = start_char + m.end()
            next_j = _char_end_to_token_index(spans, j, end_char)
            cand = (next_j, text.strip(), value)
            if best is None or len(cand[1]) > len(best[1]):
                best = cand
    return best


def _score_partial(units: list, actual: str) -> tuple:
    """
    Sequence-alignment scorer.

    The old scorer was greedy and literal-span based. This version treats every
    literal word as a unit, aligns expected units to actual tokens, and classifies
    mismatches as substitutions, insertions/extras, or missing units.

    Scoring rules:
      - literal word match: +1 matched / +1 total
      - required tag match: +1 matched / +1 total
      - substitution: +0 matched / +1 total
      - missing expected unit: +0 matched / +1 total
      - unexpected extra actual word: +0 matched / +1 total
      - wildcard-consumed text: no score impact
    """
    global _CURRENT_ACTUAL_TEXT
    _CURRENT_ACTUAL_TEXT = actual
    spans = _actual_token_spans(actual)
    n_units = len(units)
    n_tokens = len(spans)
    memo = {}

    def score_key(res):
        matched, total, breakdown, captured = res
        pct = matched / total if total else 1.0
        # Prefer higher percentage, then more matched units, then fewer total penalties.
        return (pct, matched, -total)

    def merge_captured(a, b):
        if not b:
            return a
        out = dict(a)
        out.update(b)
        return out

    def dp(i: int, j: int):
        key = (i, j)
        if key in memo:
            return memo[key]

        # No more expected units: remaining actual words are unexpected extras.
        if i >= n_units:
            breakdown = []
            total = 0
            for k in range(j, n_tokens):
                word = spans[k][0]
                breakdown.append((f'extra "{word}"', False, word))
                total += 1
            res = (0, total, breakdown, {})
            memo[key] = res
            return res

        unit = units[i]
        label = unit["label"]
        optional = unit["optional"]
        tag_type = unit.get("tag_type")

        # Control tags end the scored contract.
        if tag_type == "BYPASS":
            res = (0, 0, [(label, True, None)], {})
            memo[key] = res
            return res

        if tag_type == "BARGEIN":
            res = (0, 0, [(label, True, None)], {})
            memo[key] = res
            return res

        candidates = []

        # Wildcard can consume any number of actual tokens with no penalty.
        if tag_type == "WILDCARD":
            for k in range(j, n_tokens + 1):
                tail = dp(i + 1, k)
                consumed = _token_text_from_span(actual, spans, j, k).strip() or None
                candidates.append((
                    tail[0],
                    tail[1],
                    [(label, True, consumed)] + tail[2],
                    tail[3],
                ))
            best = max(candidates, key=score_key)
            memo[key] = best
            return best

        # Optional non-wildcard unit can be skipped without penalty.
        if optional:
            tail = dp(i + 1, j)
            candidates.append((tail[0], tail[1], [(label, True, None)] + tail[2], tail[3]))

        # If actual token exists, allow insertion/extra before this expected unit.
        # For exact-count tags, do not allow skipping the first token of a
        # same-type run to make an overlong run match by suffix. Example:
        # {Digits Length=4} must fail on "1 2 3 4 5" instead of treating
        # "1" as extra and matching "2 3 4 5".
        if j < n_tokens:
            current_is_same_type = False
            if tag_type in ("DIGITS", "ALPHANUM"):
                tok_re = _DIGIT_TOKEN_RE if tag_type == "DIGITS" else _ALPHANUM_TOKEN_RE
                current_is_same_type = _expanded_token_len(spans[j][0], tag_type, tok_re) is not None

            if not current_is_same_type:
                word = spans[j][0]
                tail = dp(i, j + 1)
                candidates.append((
                    tail[0],
                    tail[1] + 1,
                    [(f'extra "{word}"', False, word)] + tail[2],
                    tail[3],
                ))

        # Required unit can be missing/deleted.
        if not optional:
            tail = dp(i + 1, j)
            candidates.append((
                tail[0],
                tail[1] + 1,
                [(label, False, None)] + tail[2],
                tail[3],
            ))

        # Literal word: match or substitute one actual token.
        if tag_type == "LITERAL_WORD":
            if j < n_tokens:
                actual_word = spans[j][0]
                expected_word = unit["word"]
                tail = dp(i + 1, j + 1)
                if actual_word.lower() == expected_word.lower():
                    candidates.append((
                        tail[0] + 1,
                        tail[1] + 1,
                        [(label, True, actual_word)] + tail[2],
                        tail[3],
                    ))
                else:
                    candidates.append((
                        tail[0],
                        tail[1] + 1,
                        [(f'substitution "{expected_word}" -> "{actual_word}"', False, actual_word)] + tail[2],
                        tail[3],
                    ))

        # Exact-count tags.
        elif tag_type in ("DIGITS", "ALPHANUM"):
            m = _match_exact_token_unit_at(unit, spans, j)
            if m is not None:
                next_j, text = m
                tail = dp(i + 1, next_j)
                candidates.append((
                    tail[0] + (0 if optional else 1),
                    tail[1] + (0 if optional else 1),
                    [(label, True, text)] + tail[2],
                    tail[3],
                ))
            else:
                bad_run = _invalid_exact_token_run_at(unit, spans, j)
                if bad_run is not None and not optional:
                    next_j, text = bad_run
                    tail = dp(i + 1, next_j)
                    candidates.append((
                        tail[0],
                        tail[1] + 1,
                        [(label, False, text)] + tail[2],
                        tail[3],
                    ))

        # Choice tag with optional variable capture.
        elif tag_type == "CHOICE":
            m = _match_choice_unit_at(unit, actual, spans, j)
            if m is not None:
                next_j, text, value = m
                tail = dp(i + 1, next_j)
                captured = dict(tail[3])
                varname, _ = unit["choice_meta"]
                if varname:
                    captured[varname] = value
                candidates.append((
                    tail[0] + 1,
                    tail[1] + 1,
                    [(label, True, text or None)] + tail[2],
                    captured,
                ))

        # Regex-backed tags: Date, Time, Number, Currency, etc.
        else:
            m = _match_regex_unit_at(unit, actual, spans, j)
            if m is not None:
                next_j, text = m
                tail = dp(i + 1, next_j)
                candidates.append((
                    tail[0] + (0 if optional else 1),
                    tail[1] + (0 if optional else 1),
                    [(label, True, text)] + tail[2],
                    tail[3],
                ))

        best = max(candidates, key=score_key)
        memo[key] = best
        return best

    return dp(0, 0)

def _find_exact_token_match(text: str, lo: int, hi: int, token_re: re.Pattern) -> tuple[int, str] | None:
    """
    Find a same-type token run of length lo..hi anywhere in text.
    Returns (start_char_position, matched_text) or None.

    Important: a run longer than hi is not partially matched. For example,
    {Digits Length=4} must fail on "1 2 3 4 5" rather than matching
    "2 3 4 5" and treating "1" as an extra word.
    """
    matches = list(re.finditer(r"\S+", text))
    idx = 0

    while idx < len(matches):
        if not token_re.match(matches[idx].group(0)):
            idx += 1
            continue

        start_idx = idx
        while idx < len(matches) and token_re.match(matches[idx].group(0)):
            idx += 1

        run_len = idx - start_idx
        if lo <= run_len <= hi:
            start = matches[start_idx].start()
            end = matches[idx - 1].end()
            return start, text[start:end]

        # If the run is too short/too long, skip the whole run.
        # Do not try to match a suffix of an overlong run.

    return None


def _try_match_unit(unit: dict, remaining: str):
    """Return match text or None — used by wildcard lookahead."""
    tag_type = unit.get("tag_type")
    if tag_type in ("DIGITS", "ALPHANUM"):
        tok_re = _DIGIT_TOKEN_RE if tag_type == "DIGITS" else _ALPHANUM_TOKEN_RE
        return _match_exact_tokens(remaining, unit["lo"], unit["hi"], tok_re)
    if unit["regex"] is None:
        return None
    m = unit["regex"].search(remaining)
    return m.group(0) if m else None


def _find_right_boundary(units: list, current_i: int, remaining: str) -> int | None:
    """
    Find the start position (in *remaining*) of the next literal unit after current_i.
    Returns None if no literal follows.
    """
    for j in range(current_i + 1, len(units)):
        nu = units[j]
        if nu["kind"] == "literal" and not nu["optional"]:
            m = nu["regex"].search(remaining)
            if m:
                return m.start()
            break
    return None



# ──────────────────────────────────────────────────────────────────────────────
# Word-based match scorer
# ──────────────────────────────────────────────────────────────────────────────

def _truncate_template_for_word_score(template: str) -> str:
    """
    For word scoring, keep only the template text that is actually expected
    to be heard. {BargeIn} drops everything after it. {BypassRecognition}
    is handled separately by validate().
    """
    kept_parts = []
    for kind, value in _tokenise(template):
        if kind == "tag" and _is_bargein_tag(value):
            break
        kept_parts.append("{" + value + "}" if kind == "tag" else value)
    return "".join(kept_parts)


def _normalise_words_for_score(text: str) -> list[str]:
    """
    Convert text to comparable words for word-match percentage.
    Tags are removed because tags such as {*}, {Currency}, and {BargeIn}
    are not literal spoken words.
    """
    text = re.sub(r"\{[^}]+\}", " ", text)
    text = text.lower()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return text.split()


def _word_match_percentage(expect_to_hear: str, actual: str, language: str = "en-US") -> float:
    """
    Return tag-aware, extra-aware word/unit score.

    This score is based on the same left-to-right scorer as match_percentage:
      - literal words are individual units
      - required tags are individual units
      - unexpected extra words are penalty units
      - text consumed by {*} / {wildcard} is ignored
    """
    units = _extract_scored_units(expect_to_hear, language)
    matched, total, _, _ = _score_partial(units, actual)
    return round(matched / total * 100, 1) if total else 100.0


# ──────────────────────────────────────────────────────────────────────────────
# Public result type
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class MatchResult:
    word_match_percentage: float = 0.0
    detail: str = ""
    breakdown: list = field(default_factory=list)
    captured_variables: dict = field(default_factory=dict)
    # breakdown entries: (label:str, matched:bool, matched_text:str|None)
    # captured_variables: {varname: value} from {Choice x=a:1|b:2} tags


# ──────────────────────────────────────────────────────────────────────────────
# Public API
# ──────────────────────────────────────────────────────────────────────────────

def validate(expect_to_hear: str, actual: str, language: str = "en-US") -> MatchResult:
    """
    Validate *actual* against the *expect_to_hear* template.

    The returned MatchResult contains only:
      .word_match_percentage
      .detail
      .breakdown
      .captured_variables
    """
    word_pct = _word_match_percentage(expect_to_hear, actual, language)

    bypass_re = re.compile(r"\{BypassRecognition\}", re.IGNORECASE)
    if bypass_re.search(expect_to_hear):
        return MatchResult(
            word_match_percentage=100.0,
            detail="BypassRecognition tag present — automatic 100% match",
            breakdown=[
                (
                    "{BypassRecognition}",
                    True,
                    "bypassed — no recognition performed"
                )
            ],
        )

    pattern = build_regex(expect_to_hear, language)
    units = _extract_scored_units(expect_to_hear, language)

    try:
        full_match = bool(
            re.match(
                pattern,
                actual.strip(),
                re.DOTALL | re.IGNORECASE
            )
        )
    except re.error as exc:
        return MatchResult(
            word_match_percentage=word_pct,
            detail=f"Regex error: {exc}",
        )

    matched_units, total_units, breakdown, captured = _score_partial(
        units,
        actual
    )

    if full_match:
        successful_breakdown = [
            (label, True, matched_text)
            for label, _, matched_text in breakdown
        ]

        return MatchResult(
            word_match_percentage=100.0,
            detail="Full match (100%)",
            breakdown=successful_breakdown,
            captured_variables=captured,
        )

    unit_percentage = (
        round(matched_units / total_units * 100, 1)
        if total_units
        else 0.0
    )

    failed = [
        label
        for label, unit_matched, _ in breakdown
        if not unit_matched
    ]

    detail = (
        f"Partial match {unit_percentage}% — "
        f"{matched_units}/{total_units} units matched. "
        f"Unmatched: {failed}"
        if failed
        else f"Partial match {unit_percentage}%"
    )

    return MatchResult(
        word_match_percentage=word_pct,
        detail=detail,
        breakdown=breakdown,
        captured_variables=captured,
    )


# ──────────────────────────────────────────────────────────────────────────────
# Pretty printer
# ──────────────────────────────────────────────────────────────────────────────

def print_result(result: MatchResult, eth: str = "", actual: str = ""):
    print(f"  Template  : {eth}")
    print(f"  Actual    : {actual}")
    print(f"  Word Match: {result.word_match_percentage:.1f}%")
    print(f"  Detail    : {result.detail}")

    if result.captured_variables:
        print(f"  Captured  : {result.captured_variables}")

    if result.breakdown:
        print("  Breakdown :")
        for label, matched, matched_text in result.breakdown:
            icon = "  ✓" if matched else "  ✗"
            found = f'  → "{matched_text}"' if matched_text else ""
            print(f"    {icon}  {label}{found}")

    print()


# ──────────────────────────────────────────────────────────────────────────────
# Test suite
# ──────────────────────────────────────────────────────────────────────────────

def _run_tests():
    cases = [
        (
            "Welcome to the bank. {BargeIn} Please hold while we transfer you",
            "Welcome to the bank. extra words received after barge in",
            100.0,
        ),
        (
            "Your PIN is {Digits Length=4}. {BargeIn} Thank you for calling",
            "Your PIN is 1 2 3 4. extra transcript",
            100.0,
        ),
        (
            "{BypassRecognition}",
            "anything at all",
            100.0,
        ),
        (
            "Your balance is {Currency}",
            "Your balance is twenty dollars",
            100.0,
        ),
    ]

    passed = 0

    for template, actual, expected_word_pct in cases:
        result = validate(template, actual)

        if result.word_match_percentage == expected_word_pct:
            passed += 1
            status = "PASS"
        else:
            status = "FAIL"

        print(
            f"{status}: {template!r} -> "
            f"{result.word_match_percentage:.1f}%"
        )

    print(f"Results: {passed}/{len(cases)} passed")


# ──────────────────────────────────────────────────────────────────────────────
# Entry point
# ──────────────────────────────────────────────────────────────────────────────

_KNOWN_LANGS = {"en-US", "en-AU", "en-GB", "es-US", "nl-NL"}

_CLI_HELP = """
Usage:
  python prompt_validator.py                          # run test suite
  python prompt_validator.py "<template>" "<actual>"
  python prompt_validator.py "<template>" "<actual>" <lang>

IMPORTANT: Always quote the template and actual text.

Supported language codes: en-US (default), en-AU, en-GB, es-US, nl-NL
Note: {*} and {wildcard} are interchangeable — both mean the same thing.

Examples:
  python prompt_validator.py "Your PIN is {Digits Length=4}" "Your PIN is 1 2 3 4"
  python prompt_validator.py "{*} from {Date} to {Date}" "Flights from June 4th to June 10th"
  python prompt_validator.py "{wildcard} from {Date} to {Date}" "Flights from June 4th to June 10th"
  python prompt_validator.py "Balance is {Currency}" "Balance is two pounds" en-GB
  python prompt_validator.py "{BypassRecognition}" "any audio"
  python prompt_validator.py "Welcome to the bank. {BargeIn} please hold" "Welcome to the"
"""


if __name__ == "__main__":
    argv = sys.argv[1:]

    if len(argv) == 0:
        _run_tests()
        sys.exit(0)

    if argv[0] in ("-h", "--help"):
        print(_CLI_HELP)
        sys.exit(0)

    if len(argv) < 2:
        print("Error: need both <template> and <actual> arguments.\n")
        print(_CLI_HELP)
        sys.exit(1)

    if len(argv) > 3:
        print(f"Error: received {len(argv)} arguments but expected 2 or 3.")
        print("       Extra arguments usually mean the template/actual wasn't quoted.\n")
        roles = ["<template>", "<actual>", "<lang>"]
        for j, a in enumerate(argv):
            role = roles[j] if j < 3 else f"<extra {j-2}>"
            print(f"         [{j+1}] {role} = {a!r}")
        print()
        print(_CLI_HELP)
        sys.exit(1)

    eth    = argv[0]
    actual = argv[1]
    lang   = "en-US"

    if len(argv) == 3:
        if argv[2] in _KNOWN_LANGS:
            lang = argv[2]
        else:
            print(f"Warning: {argv[2]!r} is not a recognised language code.")
            print(f"         Known codes: {', '.join(sorted(_KNOWN_LANGS))}")
            print("         Proceeding with en-US.\n")

    result = validate(eth, actual, lang)
    print_result(result, eth, actual)
