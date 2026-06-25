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
_YEAR = r"(?:\d{4}|(?:nineteen|twenty)\s+\w+(?:\s+\w+)?)"

_DATE = "(?:" + "|".join([
    rf"(?:{_WEEKDAY}\s+)?(?:the\s+)?{_ORD}\s+of\s+{_MONTH}(?:\s+{_YEAR})?",
    rf"(?:{_WEEKDAY}\s+)?{_MONTH}\s+{_ORD}(?:\s+{_YEAR})?",
    rf"{_WEEKDAY}\s+{_MONTH}\s+{_ORD}(?:\s+{_YEAR})?",
    rf"(?:today|tomorrow|yesterday|this\s+{_WEEKDAY})",
    r"\d{1,2}[\/\-]\d{1,2}(?:[\/\-]\d{2,4})?",
    _MONTH,
    rf"{_WEEKDAY}(?:\s+the\s+{_ORD})?",
    r"\d{1,2}\s+\d{1,2}",
]) + ")"

_TIME = "(?:" + "|".join([
    r"(?:noon|midnight|midday)",
    r"(?:before|after)\s+\w+(?:\s+\w+)?",
    r"\d{1,2}:\d{2}\s*(?:am|pm)?",
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
    ],
    "es-US": [
        r"(?:\w+)(?:\s+\w+)*\s+(?:pesos?|d(?:o|ó)lares?)\s+y\s+(?:\w+)(?:\s+\w+)*\s+centavos?",
    ],
    "nl-NL": [
        r"(?:\w+)(?:\s+\w+)*\s+euro\s+en\s+(?:\w+)(?:\s+\w+)*\s+cent",
    ],
}
_CURRENCY_GENERIC = (
    r"(?:"
    rf"{_NUM_W_OR_D}(?:\s+{_NUM_WORD})*"
    r"\s+(?:dollars?|euros?|pounds?|pesos?|yen|francs?|rupees?|kroner?)"
    r"(?:\s+and\s+\w+(?:\s+\w+)*\s+(?:cents?|pence|penny|centavos?|centimes?))?"
    rf"|{_NUM_W_OR_D}(?:\s+{_NUM_WORD})*"   # bare amount (backtrack target)
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
    """True if this tag is {BargeIn} (case-insensitive, tolerant of stray spaces/hyphens)."""
    return re.sub(r"[\s\-]", "", tag_value.strip().upper()) == "BARGEIN"


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
        # Full-regex fragment — boundary is enforced by exact-count in partial scorer
        return rf"(?:{_DIGIT_TOKEN}(?:\s+{_DIGIT_TOKEN}){{{lo - 1},{hi - 1}}})"

    m = re.match(r"ALPHANUM(?:\s+LENGTH=(\d+)(?:-(\d+))?)?(?:\s+\$\w+.*)?$", tu)
    if m:
        lo = int(m.group(1)) if m.group(1) else 1
        hi = int(m.group(2)) if m.group(2) else (lo if m.group(1) else 20)
        return rf"(?:[A-Za-z0-9](?:\s*[A-Za-z0-9]){{{lo - 1},{hi - 1}}})"

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
        # BargeIn: text before the tag is matched normally; text after it is
        # dropped entirely (no audio exists past the barge-in point), so we
        # add no fragment for it and stop — the anchor lands right here.
        if kind == "tag" and _is_bargein_tag(value):
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
    return bool(re.search(r"[A-Za-z0-9]", text))


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
            frag = _literal_to_regex(value.strip())
            units.append({
                "label":    f'literal "{value.strip()}"',
                "regex":    re.compile(frag, re.IGNORECASE),
                "optional": False,
                "kind":     "literal",
                "tag_type": None,
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

def _score_partial(units: list, actual: str) -> tuple:
    """
    Left-to-right ordered scan.

    Returns (matched_count, total_units, breakdown, captured_variables).
    breakdown  : list of (label, matched:bool, matched_text:str|None)
    captured_variables : dict of {varname: value} from Choice tags
    """
    remaining  = actual.strip()
    breakdown  = []
    total      = 0
    matched    = 0
    captured   = {}
    i          = 0

    while i < len(units):
        unit     = units[i]
        label    = unit["label"]
        optional = unit["optional"]
        tag_type = unit.get("tag_type")

        # ── BypassRecognition ──────────────────────────────────────────────────
        if tag_type == "BYPASS":
            breakdown.append((label, True, None))
            i += 1
            break

        # ── BargeIn: marker only — preceding units already scored normally;
        #             nothing after this point was ever part of the contract ──
        if tag_type == "BARGEIN":
            breakdown.append((label, True, None))
            i += 1
            break

        # ── Wildcard: advance remaining to where the next unit starts ──────────
        if tag_type == "WILDCARD":
            next_start = None
            for j in range(i + 1, len(units)):
                nu = units[j]
                if nu.get("tag_type") == "WILDCARD":
                    continue
                # Peek ahead using the next unit's matching logic
                test_match = _try_match_unit(nu, remaining)
                if test_match is not None:
                    # find its start position
                    if nu.get("tag_type") in ("DIGITS", "ALPHANUM"):
                        next_start = 0  # exact-count always starts from 0
                    else:
                        m = nu["regex"].search(remaining)
                        if m:
                            next_start = m.start()
                    break
            consumed  = remaining if next_start is None else remaining[:next_start]
            remaining = "" if next_start is None else remaining[next_start:]
            breakdown.append((label, True, consumed.strip() or None))
            i += 1
            continue

        # ── Exact-count: Digits / AlphaNum ────────────────────────────────────
        if tag_type in ("DIGITS", "ALPHANUM"):
            if not optional:
                total += 1
            lo, hi   = unit["lo"], unit["hi"]
            tok_re   = _DIGIT_TOKEN_RE if tag_type == "DIGITS" else _ALPHANUM_TOKEN_RE
            # Find right boundary from next literal unit
            right_boundary = _find_right_boundary(units, i, remaining)
            search_in = remaining if right_boundary is None else remaining[:right_boundary]
            result = _match_exact_tokens(search_in, lo, hi, tok_re)
            if result is not None:
                matched += 1
                breakdown.append((label, True, result))
                remaining = remaining[len(result):].lstrip()
            else:
                breakdown.append((label, False, None))
            i += 1
            continue

        # ── Choice: match and capture variable ────────────────────────────────
        if tag_type == "CHOICE":
            if not optional:
                total += 1
            varname, alts = unit["choice_meta"]
            right_boundary = _find_right_boundary(units, i, remaining)
            search_in = remaining if right_boundary is None else remaining[:right_boundary + 50]
            match_result = None
            matched_value = None
            for phrase, value in alts:
                if phrase == ".":
                    # matches empty — counts as found
                    match_result = ""
                    matched_value = value
                    break
                pat = re.compile(_literal_to_regex(phrase), re.IGNORECASE)
                m = pat.search(search_in)
                if m:
                    match_result = m.group(0)
                    matched_value = value
                    remaining = remaining[remaining.index(match_result) + len(match_result):]
                    break
            if match_result is not None:
                matched += 1
                if varname:
                    captured[varname] = matched_value
                breakdown.append((label, True, match_result or None))
            else:
                breakdown.append((label, False, None))
            i += 1
            continue

        # ── All other tags and literals ────────────────────────────────────────
        if not optional:
            total += 1

        right_boundary = _find_right_boundary(units, i, remaining)
        m = unit["regex"].search(remaining)

        if m:
            # Respect right boundary to prevent consuming upcoming literal text
            if right_boundary is not None and m.end() > right_boundary:
                bounded_m = unit["regex"].search(remaining[:right_boundary])
                if bounded_m:
                    m = bounded_m
            matched += (0 if optional else 1)
            breakdown.append((label, True, m.group(0).strip()))
            remaining = remaining[m.end():]
        else:
            breakdown.append((label, False, None))

        i += 1

    return matched, total, breakdown, captured


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
# Public result type
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class MatchResult:
    matched: bool                    # True only when full regex anchor passes (100%)
    match_percentage: float          # 0.0–100.0
    pattern_used: str                # anchored regex (for debugging)
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

    Returns MatchResult with:
      .matched              True if 100% full-regex match
      .match_percentage     0.0–100.0 partial credit
      .breakdown            [(label, matched, matched_text), ...]
      .captured_variables   {varname: value} from Choice tags with variables
      .pattern_used         full regex string
      .detail               human-readable summary
    """
    # ── BypassRecognition short-circuit ───────────────────────────────────────
    # If the first meaningful tag is {BypassRecognition}, return 100% immediately.
    bypass_re = re.compile(r"\{BypassRecognition\}", re.IGNORECASE)
    if bypass_re.search(expect_to_hear):
        bd = [("{BypassRecognition}", True, "bypassed — no recognition performed")]
        return MatchResult(
            matched=True,
            match_percentage=100.0,
            pattern_used="<BypassRecognition>",
            detail="BypassRecognition tag present — automatic 100% match",
            breakdown=bd,
        )

    pattern = build_regex(expect_to_hear, language)
    units   = _extract_scored_units(expect_to_hear, language)

    # ── Full match ────────────────────────────────────────────────────────────
    try:
        full_match = bool(re.match(pattern, actual.strip(), re.DOTALL | re.IGNORECASE))
    except re.error as exc:
        return MatchResult(matched=False, match_percentage=0.0,
                           pattern_used=pattern, detail=f"Regex error: {exc}")

    # Run partial scorer in all cases (gives us breakdown + captured variables)
    mc, total, breakdown, captured = _score_partial(units, actual)

    if full_match:
        # Rebuild breakdown to mark all scored units as matched (anchored pass)
        bd = [(label, True, text) for label, _, text in breakdown]
        return MatchResult(
            matched=True, match_percentage=100.0,
            pattern_used=pattern, detail="Full match (100%)",
            breakdown=bd, captured_variables=captured,
        )

    pct = round(mc / total * 100, 1) if total else 0.0
    failed = [lbl for lbl, ok, _ in breakdown if not ok]
    detail = (
        f"Partial match {pct}% — {mc}/{total} units matched. "
        f"Unmatched: {failed}"
        if failed else f"Partial match {pct}%"
    )
    return MatchResult(
        matched=False, match_percentage=pct,
        pattern_used=pattern, detail=detail,
        breakdown=breakdown, captured_variables=captured,
    )


# ──────────────────────────────────────────────────────────────────────────────
# Pretty printer
# ──────────────────────────────────────────────────────────────────────────────

def print_result(result: MatchResult, eth: str = "", actual: str = ""):
    bar_width = 30
    filled = int(result.match_percentage / 100 * bar_width)
    bar = "█" * filled + "░" * (bar_width - filled)

    print(f"  Template  : {eth}")
    print(f"  Actual    : {actual}")
    print(f"  Match     : [{bar}] {result.match_percentage:.1f}%  "
          f"{'✓ FULL MATCH' if result.matched else '✗ PARTIAL/NO MATCH'}")
    if result.captured_variables:
        print(f"  Captured  : {result.captured_variables}")
    if result.breakdown:
        print("  Breakdown :")
        for label, ok, text in result.breakdown:
            icon   = "  ✓" if ok else "  ✗"
            found  = f'  → "{text}"' if text else ""
            print(f"    {icon}  {label}{found}")
    print()


# ──────────────────────────────────────────────────────────────────────────────
# Test suite
# ──────────────────────────────────────────────────────────────────────────────

def _run_tests():
    PASS = "\033[92mPASS\033[0m"
    FAIL = "\033[91mFAIL\033[0m"

    # (eth, actual, lang, exp_matched, exp_pct, exp_captured)
    cases = [
        # ── Basic tag tests ───────────────────────────────────────────────────
        ("The date today is the {Date}",
         "The date today is the 4th of June 2014", "en-US", True, 100.0, {}),
        ("Your credit card will expire {Date}",
         "Your credit card will expire tomorrow", "en-US", True, 100.0, {}),
        ("At the third tone the time will be {Time} precisely",
         "At the third tone the time will be 12:24pm precisely", "en-US", True, 100.0, {}),
        ("Opening times are from {Time} till {Time} each weekday",
         "Opening times are from 9:30am till 5:30pm each weekday", "en-US", True, 100.0, {}),
        ("There are {Number} products available",
         "There are One Thousand and Four products available", "en-US", True, 100.0, {}),

        # ── AlphaNum exact length ─────────────────────────────────────────────
        ("Your postcode is {AlphaNum Length=6}",
         "Your postcode is K 2 G 4 A 3", "en-US", True, 100.0, {}),
        ("Your postcode is {AlphaNum Length=6}",
         "Your postcode is K 2 G 4 A",   "en-US", False, 50.0, {}),   # 5 tokens — too short
        ("Your postcode is {AlphaNum Length=6}",
         "Your postcode is K 2 G 4 A 3 X", "en-US", False, 50.0, {}), # 7 tokens — too long
        ("Code is {AlphaNum Length=4-6}",
         "Code is A B C D E", "en-US", True, 100.0, {}),               # 5, in range
        ("Code is {AlphaNum Length=4-6}",
         "Code is A B C", "en-US", False, 50.0, {}),                   # 3, too short
        ("Code is {AlphaNum Length=4-6}",
         "Code is A B C D E F G", "en-US", False, 50.0, {}),           # 7, too long

        # ── Digits exact length ───────────────────────────────────────────────
        ("Your telephone account PIN is {Digits Length=4}",
         "Your telephone account PIN is 9 8 9 8", "en-US", True, 100.0, {}),
        ("Your telephone account PIN is {Digits Length=4}",
         "Your telephone account PIN is 9 8 9 8 7", "en-US", False, 50.0, {}),  # 5 digits
        ("Your telephone account PIN is {Digits Length=4-6}",
         "Your telephone account PIN is 9 8 9 8 7", "en-US", True, 100.0, {}),

        # ── Choice with variable capture ──────────────────────────────────────
        ("Good {Choice x=morning:1|afternoon:2|evening:3}, welcome.",
         "Good afternoon, welcome.", "en-US", True, 100.0, {"x": "2"}),
        ("Good {Choice x=morning:1|afternoon:2|evening:3}, welcome.",
         "Good morning, welcome.", "en-US", True, 100.0, {"x": "1"}),
        ("Please enter your {Choice digit=first:1|second:2|third:3} digit.",
         "Please enter your second digit.", "en-US", True, 100.0, {"digit": "2"}),
        # Choice without variable
        ("Good {Choice morning|afternoon|evening}, welcome to A B C Bank.",
         "Good afternoon, welcome to A B C Bank.", "en-US", True, 100.0, {}),
        ("Good {Choice morning|afternoon|evening}, welcome to A B C Bank.",
         "Good night, welcome to A B C Bank.", "en-US", False, None, {}),
        # Choice with dot (empty alternative)
        ("Welcome. {Choice Our office is now closed.|.} For banking, press 1.",
         "Welcome. Our office is now closed. For banking, press 1.", "en-US", True, 100.0, {}),
        ("Welcome. {Choice Our office is now closed.|.} For banking, press 1.",
         "Welcome. For banking, press 1.", "en-US", True, 100.0, {}),

        # ── BypassRecognition ─────────────────────────────────────────────────
        ("{BypassRecognition}",
         "anything at all", "en-US", True, 100.0, {}),
        ("{BypassRecognition} This is a comment about what we are bypassing",
         "some audio we don't care about", "en-US", True, 100.0, {}),
        ("Hello. {BypassRecognition} ignore rest",
         "completely different text", "en-US", True, 100.0, {}),

        # ── BargeIn ───────────────────────────────────────────────────────────
        # Text before the tag must still match normally; text after it in the
        # template is dropped — there's no audio for it past the barge-in point.
        ("Welcome to the bank. {BargeIn} Please hold while we transfer you",
         "Welcome to the bank.", "en-US", True, 100.0, {}),
        ("Welcome to the bank. {BargeIn} Please hold while we transfer you",
         "Welcome to the wrong place.", "en-US", False, 0.0, {}),
        ("Your PIN is {Digits Length=4}. {BargeIn} Thank you for calling",
         "Your PIN is 1 2 3 4.", "en-US", True, 100.0, {}),
        ("Your PIN is {Digits Length=4}. {BargeIn} Thank you for calling",
         "Your PIN is 1 2 3.", "en-US", False, 50.0, {}),
        ("Good {Choice morning|afternoon|evening}, your balance is {BargeIn} due in full",
         "Good afternoon, your balance is", "en-US", True, 100.0, {}),

        # ── Wildcard ──────────────────────────────────────────────────────────
        ("{*} Your balance is {Currency} rupees",
         "Your balance is twenty rupees", "en-US", True, 100.0, {}),
        ("Hello {*} balance is {Currency} rupees",
         "Hello sir balance is twenty rupees", "en-US", True, 100.0, {}),
        ("Call {*} on {Date} at {Time}",
         "Call us on June 4th at 9:30am", "en-US", True, 100.0, {}),
        ("Your PIN is {Digits Length=4} {*}",
         "Your PIN is 1 2 3 4 extra stuff", "en-US", True, 100.0, {}),
        ("Account {Digits Length=4} {*} expires {Date}",
         "Account 1 2 3 4 NOISE expires tomorrow", "en-US", True, 100.0, {}),

        # ── {wildcard} as alias for {*} ──────────────────────────────────────
        ("{wildcard} Your balance is {Currency} rupees",
         "Your balance is twenty rupees", "en-US", True, 100.0, {}),
        ("Hello {wildcard} balance is {Currency} rupees",
         "Hello sir balance is twenty rupees", "en-US", True, 100.0, {}),
        ("Call {Wildcard} on {Date} at {Time}",
         "Call us on June 4th at 9:30am", "en-US", True, 100.0, {}),
        ("Your PIN is {Digits Length=4} {WILDCARD}",
         "Your PIN is 1 2 3 4 extra stuff", "en-US", True, 100.0, {}),

        # ── Combinations ─────────────────────────────────────────────────────
        ("Your PIN is {Digits Length=4} and your account ends in {Digits Length=4}",
         "Your PIN is 1 2 3 4 and your account ends in 5 6 7 8", "en-US", True, 100.0, {}),
        ("Good {Choice x=morning:1|afternoon:2|evening:3}, your {Number} transactions total {Currency} as of {Date}",
         "Good evening, your five transactions total twenty dollars and zero cents as of today",
         "en-US", True, 100.0, {"x": "3"}),

        # ── Partial match ─────────────────────────────────────────────────────
        ("Your balance of {Currency} is due on {Date}",
         "Your balance of BLAH BLAH is due on tomorrow", "en-US", False, 75.0, {}),
        ("Call on {Date} at {Time}",
         "Call on NOTHING at NOTHING", "en-US", False, 50.0, {}),
    ]

    print("=" * 76)
    print("  Prompt Validator — Full Test Suite")
    print("=" * 76)

    passed = 0
    for i, row in enumerate(cases, 1):
        eth, actual, lang, exp_match, exp_pct, exp_cap = row
        result = validate(eth, actual, lang)

        match_ok = result.matched == exp_match
        pct_ok   = (exp_pct is None) or (result.match_percentage == exp_pct)
        cap_ok   = (exp_cap == {} ) or (result.captured_variables == exp_cap)
        ok       = match_ok and pct_ok and cap_ok

        if ok:
            passed += 1
        status   = PASS if ok else FAIL
        pct_note = f"{result.match_percentage:.1f}%"
        if not pct_ok:
            pct_note += f"  (expected {exp_pct}%)"
        cap_note = f"  captured={result.captured_variables}" if result.captured_variables else ""

        print(f"\n[{i:02d}] {status}  lang={lang}  full={result.matched}  pct={pct_note}{cap_note}")
        print(f"     ETH    : {eth}")
        print(f"     Actual : {actual}")
        if not ok:
            print(f"     Detail : {result.detail}")
            if not cap_ok:
                print(f"     Expected captured: {exp_cap}  got: {result.captured_variables}")
            for lbl, unit_ok, text in result.breakdown:
                icon = "✓" if unit_ok else "✗"
                snip = f' → "{text}"' if text else ""
                print(f"             {icon} {lbl}{snip}")

    print("\n" + "=" * 76)
    print(f"  Results: {passed}/{len(cases)} passed")
    print("=" * 76)


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
