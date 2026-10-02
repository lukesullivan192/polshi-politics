"""Text normalisation for fuzzy-matching event titles and market outcomes."""

import re
import unicodedata

STATES = {
    "al": "alabama", "ak": "alaska", "az": "arizona", "ar": "arkansas", "ca": "california",
    "co": "colorado", "ct": "connecticut", "de": "delaware", "fl": "florida", "ga": "georgia",
    "hi": "hawaii", "id": "idaho", "il": "illinois", "in": "indiana", "ia": "iowa",
    "ks": "kansas", "ky": "kentucky", "la": "louisiana", "me": "maine", "md": "maryland",
    "ma": "massachusetts", "mi": "michigan", "mn": "minnesota", "ms": "mississippi",
    "mo": "missouri", "mt": "montana", "ne": "nebraska", "nv": "nevada", "nh": "new hampshire",
    "nj": "new jersey", "nm": "new mexico", "ny": "new york", "nc": "north carolina",
    "nd": "north dakota", "oh": "ohio", "ok": "oklahoma", "or": "oregon", "pa": "pennsylvania",
    "ri": "rhode island", "sc": "south carolina", "sd": "south dakota", "tn": "tennessee",
    "tx": "texas", "ut": "utah", "vt": "vermont", "va": "virginia", "wa": "washington",
    "wv": "west virginia", "wi": "wisconsin", "wy": "wyoming",
}
STATE_NAMES = sorted(set(STATES.values()), key=len, reverse=True)

# Filler words that differ between the venues' title styles but carry no meaning.
STOPWORDS = {
    "will", "the", "be", "who", "what", "which", "of", "in", "a", "an", "to", "by", "for",
    "winner", "election", "elections", "general", "win", "wins", "next", "person",
    "midterms", "midterm", "us", "u", "s", "how", "many", "do", "does", "is", "are", "on", "at",
}

# Words that change what kind of market an event is. Two events only match if they
# have the same set (so "margin of victory" never pairs with "winner").
KIND_WORDS = {
    "margin": "margin", "turnout": "turnout", "percent": "percent", "share": "percent",
    "seats": "seats", "popular": "popular", "combo": "combo", "nominee": "nominee",
    "primary": "primary", "lieutenant": "lieutenant", "vice": "vice", "approval": "approval",
    "runoff": "runoff", "round": "round", "sweep": "sweep", "county": "county", "place": "place", "state": "state",
}

PARTIES = {
    "democratic party": "democrat", "democrats": "democrat", "democratic": "democrat",
    "dems": "democrat", "dem": "democrat",
    "republican party": "republican", "republicans": "republican", "reps": "republican",
    "rep": "republican", "gop": "republican",
}

# Outcomes like "Yes" or "By December 31, 2026" say nothing about which market it is;
# single-market events with such outcomes are paired on the event title alone.
GENERIC_OUTCOME = re.compile(
    r"^(yes|no|before .*|by .*|"
    r"(january|february|march|april|may|june|july|august|september|october|november|december) \d+ \d{4})$"
)


def _ascii(text: str) -> str:
    return unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower()


def title(text: str) -> str:
    """Normalise an event title: expand state/district codes, drop punctuation and filler words."""
    t = _ascii(text)
    # "FL-21" -> "florida district 21"; at-large seats ("WY-AL") are the whole state -> "wyoming"
    t = re.sub(
        r"\b([a-z]{2})-(\d{1,2}|al)\b",
        lambda m: STATES.get(m.group(1), m.group(1))
        + (f" district {int(m.group(2))}" if m.group(2).isdigit() else ""),
        t,
    )
    t = re.sub(r"'s\b", "", t)
    t = re.sub(r"\b(\d+)(st|nd|rd|th) district\b", r"district \1", t)  # "1st District" -> "district 1"
    t = re.sub(r"\bvp\b", "vice", t)
    t = re.sub(r"[^a-z0-9 ]", " ", t)
    return " ".join(w for w in t.split() if w not in STOPWORDS)


def kinds(normalized_title: str) -> frozenset[str]:
    return frozenset(KIND_WORDS[w] for w in normalized_title.split() if w in KIND_WORDS)


def places(normalized_title: str) -> frozenset[str]:
    """States and district numbers named in a title; two events only match if these agree."""
    padded = f" {normalized_title} "
    found = {s for s in STATE_NAMES if f" {s} " in padded}
    found |= {f"district {n}" for n in re.findall(r"\bdistrict (\d+)\b", normalized_title)}
    return frozenset(found)


def outcome(text: str) -> str:
    """Normalise an outcome: drop party tags like "(D)", unify party names, strip accents."""
    o = _ascii(text)
    o = re.sub(r"\((d|r|i|ind|lib|grn)\)", "", o)
    o = re.sub(r"[^a-z0-9%+<> ]", " ", o)
    o = " ".join(o.split())
    for name, canonical in PARTIES.items():
        o = re.sub(rf"\b{name}\b", canonical, o)
    return o


def is_generic_outcome(normalized_outcome: str) -> bool:
    return bool(GENERIC_OUTCOME.match(normalized_outcome))
