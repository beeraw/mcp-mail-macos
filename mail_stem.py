"""Light French stemming for the full-text index, and the matching query rewrite.

Why this exists: the index is an FTS5 table, and Python's sqlite3 cannot register
a custom FTS5 tokenizer. Stemming is therefore done outside SQLite: the text is
turned into stems *before* it is inserted, into extra columns that sit next to
the raw ones, and the words of a query go through the very same function before
they are handed to MATCH. "facture" and "factures" both become "factur", so
either finds the other; "relance", "relancé" and "relancer" all become "relanc".
The raw columns stay as they were, which keeps exact matching (quotes) and lets
the ranking put the exact form above its variants (the weights are
BM25_WEIGHTS in mail_search).

The stemmer is deliberately light, in the spirit of Savoy's French light stemmer:
it strips plural marks, the feminine -e and a handful of very regular verb
endings, and stops there. It is neither Snowball nor a lemmatiser. A wrong merge
only costs a little precision, while a missed merge costs a message the user
knew existed, so the rules lean on the safe side: short words and numbers are
left alone, and every rule keeps a minimum stem length.

Tokenising follows unicode61, the tokenizer FTS5 uses on the same columns:
lowercase, accents dropped, split on anything that is not a letter or a digit
(the underscore counts as a separator). One input word gives exactly one output
word, so phrase adjacency and word positions are unchanged.

Only the columns holding running text (subject, attachment names, body) have a
stem twin. Sender, To and Cc are raw only, because addresses and names must
match exactly.

English goes through the same rules. They mostly strip a plural -s or -es, and
because the query is stemmed the same way, an over-eager cut ("server" ->
"serv") is harmless as long as it is consistent. The guards keep the usual
victims safe ("-ss", "-us", "-is" endings such as "address", "status", "devis").
"""

from __future__ import annotations

import re
import unicodedata
from typing import Iterable

# --------------------------------------------------------------------------
# Folding and tokenising (compatible with unicode61)
# --------------------------------------------------------------------------


class _Fold(dict):
    """Character table that drops accents and case, one character for one.

    unicode61 folds "é" and "E" onto "e". Doing it per character keeps the
    folded text the same length as the original, so an offset found in one is
    valid in the other.
    """

    def __missing__(self, code: int) -> str:
        char = chr(code)
        folded = unicodedata.normalize("NFKD", char)[:1].lower()
        if len(folded) != 1:
            folded = char
        self[code] = folded
        return folded


_FOLD = _Fold()

# A word: letters and digits, no underscore (unicode61 splits on it).
TOKEN = re.compile(r"[^\W_]+")


def fold_text(text: str) -> str:
    """Lowercase, accents dropped, same length as the input once composed.

    The text is put in NFC first: macOS file names are often NFD, where "é" is
    an "e" and a combining mark, and tokenising that would cut the word in two.
    Callers that map offsets back onto the original must compose it too.
    """
    return unicodedata.normalize("NFC", text).translate(_FOLD)


# --------------------------------------------------------------------------
# The stemmer
# --------------------------------------------------------------------------

# Words up to this length are returned as they are ("les", "des", "est", "the").
MIN_WORD_LENGTH = 4
# Longer than any real word: hashes, encoded blobs, run-together identifiers.
MAX_WORD_LENGTH = 40
# A rule never leaves a stem shorter than this.
MIN_STEM = 3
# Verb endings that also end many nouns ("-ez", "-ent", "-ait") ask for more.
MIN_STEM_STRICT = 4

# Plural forms that no rule gets right.
_IRREGULAR = {
    "travaux": "travail",
    "vitraux": "vitrail",
    "yeux": "oeil",
}

# The final -s is kept after these: "-ss" (address), "-us" (status, nous),
# "-is" (devis, avis), and "-ais"/"-ois" (francais, mois), which are the same
# in the singular. It costs the plural of a few "-i" and "-u" nouns.
_KEEP_S = ("ss", "us", "is")

# Regular verb endings, longest first, for the forms that are not a plain -e:
# infinitive, second person plural, third person plural, imperfect, future.
# (endings, minimum stem). The first match wins.
_VERB_ENDINGS: tuple[tuple[str, int], ...] = (
    ("eraient", MIN_STEM_STRICT),
    ("eront", MIN_STEM_STRICT),
    ("erait", MIN_STEM_STRICT),
    ("erais", MIN_STEM_STRICT),
    ("erons", MIN_STEM_STRICT),
    ("erez", MIN_STEM_STRICT),
    ("aient", MIN_STEM_STRICT),
    ("erai", MIN_STEM_STRICT),
    ("ait", MIN_STEM_STRICT),
    ("era", MIN_STEM_STRICT),
    ("ent", MIN_STEM_STRICT),
    ("ez", MIN_STEM_STRICT),
    ("er", MIN_STEM),
)


def _strip_verb_ending(word: str) -> str | None:
    for ending, minimum in _VERB_ENDINGS:
        if word.endswith(ending) and len(word) - len(ending) >= minimum:
            # "-ment" is a noun or adverb ending far more often than a verb one.
            if ending == "ent" and word.endswith("ment"):
                continue
            return word[: -len(ending)]
    return None


def _stem_folded(word: str) -> str:
    """Stems one already folded, lowercase word."""
    if len(word) < MIN_WORD_LENGTH or len(word) > MAX_WORD_LENGTH:
        return word
    # Identifiers, amounts, dates, reference codes: untouched.
    if any(char.isdigit() for char in word):
        return word
    if word in _IRREGULAR:
        return _IRREGULAR[word]

    # 1. Plural. "-aux" becomes "-al" ("chevaux"), "-eaux" and "-oux" just lose
    # the x ("bureaux"), and an -s follows the guard above.
    if word.endswith("x"):
        if len(word) >= 6 and word.endswith("aux") and not word.endswith("eaux"):
            word = word[:-3] + "al"
        else:
            word = word[:-1]
    elif word.endswith("s") and not word.endswith(_KEEP_S) and not word.endswith(("ais", "ois")):
        word = word[:-1]

    # 2. Verb endings, or else 3. the feminine / participle -e.
    changed = False
    stripped = _strip_verb_ending(word)
    if stripped is not None:
        word, changed = stripped, True
    elif word.endswith("ee") and len(word) - 2 >= MIN_STEM:
        word, changed = word[:-2], True
    elif word.endswith("e") and len(word) - 1 >= MIN_STEM:
        word, changed = word[:-1], True

    # 4. "appelle" and "appeler" both end up as "appel".
    if changed and len(word) > MIN_STEM and word[-1] == word[-2] and word[-1] in "lnt":
        word = word[:-1]
    return word


class _Cache(dict):
    """Word -> stem, filled on demand.

    A mailbox repeats the same few hundred thousand words millions of times, so
    remembering the answer is what keeps a full build from being slower.
    """

    LIMIT = 500_000

    def __missing__(self, word: str) -> str:
        if len(self) >= self.LIMIT:
            self.clear()
        stem = self[word] = _stem_folded(word)
        return stem


_CACHE = _Cache()


def stem_word(word: str) -> str:
    """Stem of one word, in any case and with or without accents."""
    return _CACHE[fold_text(word)]


def stem_tokens(text: str) -> list[str]:
    """The stems of every word of `text`, in order."""
    return [_CACHE[token] for token in TOKEN.findall(fold_text(text))]


def stem_text(text: str) -> str:
    """The text as space-separated stems: what goes into the text columns."""
    if not text:
        return ""
    return " ".join(stem_tokens(text))


# --------------------------------------------------------------------------
# Query rewriting
# --------------------------------------------------------------------------

RAW_COLUMNS = ("subject", "sender", "to", "cc", "attachments", "body")
# Raw column -> its stem twin, for the columns that hold running text.
STEM_COLUMNS = {
    "subject": "subject_stem",
    "attachments": "attachments_stem",
    "body": "body_stem",
}
_COLUMNS = frozenset(RAW_COLUMNS)

_QUERY_TOKEN = re.compile(
    r"""
      (?P<near>NEAR(?:/\d+)?\s*\((?:"[^"]*"|[^()"])*\))
    | (?P<filter>-?(?:\{[^{}]*\}|[A-Za-z_][A-Za-z0-9_]*)\s*:)
    | (?P<space>\s+)
    | (?P<phrase>"[^"]*"\*?)
    | (?P<open>\()
    | (?P<close>\))
    | (?P<word>[^\s"(){}^+,]+)
    | (?P<other>.)
    """,
    re.VERBOSE | re.DOTALL,
)
_OPERATORS = frozenset({"AND", "OR", "NOT"})


def _quote(tokens: Iterable[str], prefix: bool = False) -> str:
    return '"' + " ".join(tokens) + '"' + ("*" if prefix else "")


def _column_set(columns: Iterable[str]) -> str:
    return "{" + " ".join(columns) + "}"


def _filter_columns(text: str) -> frozenset[str] | None:
    """Columns named by a filter token such as "subject:" or "{to cc}:".

    None when it names something FTS5 does not know, or is negated ("-subject:").
    """
    if text.startswith("-"):
        return None
    name = text.rstrip().rstrip(":").rstrip().strip()
    names = name[1:-1].split() if name.startswith("{") else [name]
    if not names or any(item.lower() not in _COLUMNS for item in names):
        return None
    return frozenset(item.lower() for item in names)


# Stands for "the term or group that follows a negated filter": it is passed
# through as written, since the exclusion must keep applying to exactly it.
_UNTOUCHED: frozenset[str] = frozenset({"<untouched>"})


def _rewrite_term(
    tokens: list[str], prefix: bool, columns: frozenset[str] | None, expand: bool, phrase: bool
) -> str:
    """One word or phrase, as the FTS5 expression to search it.

    `tokens` are the folded words as typed. A phrase is searched in the raw
    columns only: exact. A word (or a prefix) is searched as typed in the raw
    columns OR as its stem in the stem columns. With `expand` False (NEAR
    arguments, after "^" or "+") no OR is produced: the raw form only.
    """
    if not expand:
        return _quote(tokens, prefix)
    raw = [column for column in RAW_COLUMNS if columns is None or column in columns]
    parts = [f"{_column_set(raw)}: {_quote(tokens, prefix)}"]
    if not phrase:
        stems = [_CACHE[token] for token in tokens]
        twins = [STEM_COLUMNS[column] for column in raw if column in STEM_COLUMNS]
        if twins:
            parts.append(f"{_column_set(twins)}: {_quote(stems, prefix)}")
    return parts[0] if len(parts) == 1 else "(" + " OR ".join(parts) + ")"


def _rewrite_near(text: str) -> str:
    """NEAR(a b, 5): the words inside are searched as typed, the distance is kept."""
    head, _, rest = text.partition("(")
    inner = rest[:-1]
    distance = ""
    found = re.search(r",\s*\d+\s*$", inner)
    if found:
        inner, distance = inner[: found.start()], found.group(0)
    return head + "(" + _rewrite(inner, expand=False) + distance + ")"


def _rewrite(query: str, expand: bool = True) -> str:
    """The rewrite itself. `expand` False is the NEAR() mode: words as typed, no
    OR groups and no explicit ANDs, which NEAR does not accept."""
    out: list[str] = []
    position = 0
    # One pending column set per open parenthesis, plus the one waiting for the
    # next term ("subject: facture", "subject: (a OR b)").
    context: list[frozenset[str] | None] = [None]
    pending: frozenset[str] | None = None
    # Where the pending filter was written, so it can be dropped when the term
    # it applies to is expanded (the expansion names its own columns) or kept
    # when the term is written raw ("subject:^word").
    filter_slot: int | None = None
    joined = False  # the previous token was "^" or "+", which must touch the next term
    # An expanded term is a parenthesised group, and FTS5 does not take two
    # operands side by side unless they are plain phrases: the implicit AND
    # between them is written out.
    ends_operand = False

    def begin_operand() -> None:
        nonlocal ends_operand
        if ends_operand and expand:
            tail = next((piece for piece in reversed(out) if piece), "")
            out.append("AND " if tail[-1:].isspace() else " AND ")
        ends_operand = False

    def drop_filter() -> None:
        nonlocal filter_slot
        if filter_slot is not None:
            out[filter_slot] = ""
        filter_slot = None

    def emit_term(text: str, body: str, prefix: bool, phrase: bool, columns, next_is_plus: bool) -> None:
        nonlocal pending, filter_slot
        begin_operand()
        pending = None
        if columns is _UNTOUCHED:
            out.append(text)
            filter_slot = None
            return
        words = TOKEN.findall(fold_text(body))
        if not words:
            out.append(text)
            filter_slot = None
            return
        # Next to "^" or "+" the term must stay a plain phrase: no OR group.
        together = expand and not joined and not next_is_plus
        if together:
            drop_filter()
        filter_slot = None
        out.append(_rewrite_term(words, prefix, columns, together, phrase))

    for match in _QUERY_TOKEN.finditer(query):
        out.append(query[position:match.start()])
        position = match.end()
        kind, text = match.lastgroup, match.group(0)
        if kind == "space":
            out.append(text)
            continue
        if kind == "near":
            begin_operand()
            out.append(_rewrite_near(text))
            pending = None
            ends_operand = True
        elif kind == "filter":
            columns = _filter_columns(text)
            if columns is None and text.startswith("-") and text[1:].strip():
                # A negated filter: what it applies to is left exactly as written.
                begin_operand()
                out.append(text)
                pending = _UNTOUCHED
                filter_slot = None
            elif columns is None:
                name = text.rstrip().rstrip(":").strip()
                if name.startswith("{"):
                    begin_operand()
                    out.append(text)
                    pending = None
                    filter_slot = None
                else:
                    # Not a column ("re:", "ref:"): a word followed by a colon,
                    # searched as the word, like the quoted-terms retry would.
                    emit_term(text, name, False, True, context[-1], False)
                    ends_operand = True
            else:
                begin_operand()
                out.append(text)
                filter_slot = len(out) - 1
                pending = columns
        elif kind == "open":
            begin_operand()
            drop_filter()
            out.append(text)
            context.append(pending if pending is not None else context[-1])
            pending = None
        elif kind == "close":
            out.append(text)
            if len(context) > 1:
                context.pop()
            pending = None
            ends_operand = True
        elif kind in ("phrase", "word"):
            if text in _OPERATORS:
                out.append(text)
                ends_operand = False
                continue
            columns = pending if pending is not None else context[-1]
            if kind == "phrase":
                # "a b"* is FTS5 for a prefix on the last word.
                prefix = text.endswith("*")
                body, phrase = (text[1:-2] if prefix else text[1:-1]), True
            else:
                prefix = text.endswith("*")
                body = text[:-1] if prefix else text
                # A word FTS5 would refuse ("l'entreprise", "F-2025-001",
                # "12/2025") is read as the phrase of the words it splits into.
                phrase = not re.fullmatch(r"[A-Za-z0-9_\u0080-\U0010ffff]+", body)
            plus = re.match(r"\s*\+", query[match.end():]) is not None
            emit_term(text, body, prefix, phrase, columns, plus)
            ends_operand = True
        else:
            out.append(text)
            ends_operand = False
            joined = text in ("^", "+")
            continue
        joined = False
    out.append(query[position:])
    return "".join(out)


def rewrite_query(query: str) -> str:
    """Rewrites an FTS5 query so it finds the raw and the stemmed columns.

    - A bare word is searched as typed in the raw columns OR as its stem in the
      stem columns (subject, attachments, body): "facture" finds "factures", and
      messages holding the exact word rank higher.
    - A quoted phrase is exact: raw columns only, no inflection.
    - "word*" is a prefix on the raw columns OR on the stem columns (a prefix
      shorter than the stem, "fact*", matches there; a longer one only matches raw).
    - Column filters keep their meaning: "subject:word" searches the raw column OR
      its stem twin; "sender:", "to:", "cc:" and "{to cc}:" are raw only, so names
      and addresses stay exact. AND, OR, NOT, parentheses and NEAR() pass through
      (NEAR words are raw).
    - Anything that is not a valid bareword or phrase is left as it is, so FTS5
      rejects it as before and the caller's fallback still applies.
    """
    return _rewrite(query)
