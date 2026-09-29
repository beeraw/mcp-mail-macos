"""Gmail-style search operators, parsed off the front of a search_all query.

The parser runs BEFORE the FTS5 part: it pulls the `key:value` tokens out of the
query string, turns them into SQL conditions on messages / locations /
recipients, and hands only the remaining free text on to the FTS rewrite
(mail_stem) and the quoted-terms retry. Nothing here touches Mail or the FTS
index; it only builds SQL fragments and validates values.

An operator is `key:value` with NO space after the colon (`to:jane`), the value
optionally quoted (`from:"jane doe"`), and a leading `-` negates it. With a
space (`to: jane`) or a brace set (`{to cc}: jane`) the token is left alone and
stays an FTS5 column filter, so the To/Cc columns remain searchable. Text inside
a quoted phrase is never read as an operator. Unknown keys stay free text.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable

from mail_tools import MailError

OPERATORS = (
    "from", "to", "cc", "has", "filename", "larger", "smaller",
    "older_than", "newer_than", "after", "before", "is", "in",
)

# A quoted phrase is skipped whole (so its content is never an operator); an
# operator must start a token; its value is a quoted string or a run of
# non-blank characters.
_TOKEN = re.compile(
    r'"[^"]*"'
    r'|(?<![^\s(])(?P<neg>-?)(?P<key>' + "|".join(OPERATORS) + r'):(?P<value>"[^"]*"|[^\s")(]+)',
    re.IGNORECASE,
)

_UNITS_SECONDS = {"d": 86400, "w": 7 * 86400, "m": 30 * 86400, "y": 365 * 86400}
_SIZE_UNITS = {"": 1, "k": 1024, "m": 1024**2, "g": 1024**3}
_IS_VALUES = ("unread", "read", "flagged", "starred", "bulk")


@dataclass(frozen=True)
class Filter:
    key: str
    value: str
    negated: bool = False


@dataclass(frozen=True)
class ParsedQuery:
    free_text: str
    filters: list[Filter]


def _bad(key: str, value: str, expected: str) -> MailError:
    return MailError(
        "invalid_operator",
        f"Unusable value for {key}: {value!r}.",
        f"{key}: expects {expected}. Write it without a space after the colon.",
    )


def _not_combinable(key: str) -> MailError:
    return MailError(
        "invalid_operator",
        f"Operators cannot be combined with OR, or grouped with other text ({key}:).",
        "Operators combine with AND only: use -key:value to exclude, and run two "
        "searches for alternatives.",
    )


def parse(query: str) -> ParsedQuery:
    """Splits a query into operator filters and the free text left over.

    Values are validated here, so a bad date or size fails before any SQL runs.
    `NOT key:value` is `-key:value`; parentheses wrapping only operators are
    dropped; OR next to an operator, or an operator sharing a parenthesised
    group with OR or free text, is refused rather than silently read as AND.
    """
    keep = [True] * len(query)
    quoted = [False] * len(query)
    found: list[tuple[re.Match, bool]] = []
    for match in _TOKEN.finditer(query):
        if match.group("key") is None:
            for index in range(match.start(), match.end()):
                quoted[index] = True
            continue
        found.append((match, False))

    filters: list[Filter] = []
    starts: list[int] = []
    for match, _ in found:
        key = match.group("key").lower()
        value = match.group("value")
        if value.startswith('"'):
            value = value[1:-1]
        value = value.strip()
        if not value:
            raise _bad(key, value, "a value")
        before = query[:match.start()]
        after = query[match.end():]
        if re.search(r"(?:^|[\s(])OR\s*$", before) or re.match(r"\s*OR(?=[\s)]|$)", after):
            raise _not_combinable(key)
        negated = bool(match.group("neg"))
        word = re.search(r"(?:^|(?<=[\s(]))NOT\s+$", before)
        start = match.start()
        if word:
            if negated:
                raise _not_combinable(key)
            if re.search(r"(?:^|[\s(])NOT\s*$", before[:word.start()]):
                raise _not_combinable(key)
            negated = True
            start = word.start()
        for index in range(start, match.end()):
            keep[index] = False
        starts.append(match.start())
        filters.append(_validated(Filter(key, value, negated)))

    if filters:
        _drop_operator_groups(query, keep, quoted, starts)
    free_text = " ".join("".join(c for c, k in zip(query, keep) if k).split()) if filters else query
    if filters:
        free_text = _tidy_connectors(free_text)
    return ParsedQuery(free_text, filters)


def _drop_operator_groups(query: str, keep: list[bool], quoted: list[bool], starts: list[int]) -> None:
    """Removes parentheses that wrap operators only; refuses any other mix."""
    stack: list[int] = []
    pairs: list[tuple[int, int]] = []
    for index, char in enumerate(query):
        if quoted[index]:
            continue
        if char == "(":
            stack.append(index)
        elif char == ")" and stack:
            pairs.append((stack.pop(), index))
    for opening, closing in pairs:  # inner groups close first
        if not any(opening < start < closing for start in starts):
            continue
        inside = "".join(query[i] for i in range(opening + 1, closing) if keep[i])
        inside = re.sub(r"\bAND\b", " ", inside).strip()
        if inside:
            raise _not_combinable("(...)")
        if re.search(r"(?:^|[\s(])(?:OR|NOT)\s*$|-$", "".join(query[i] for i in range(opening) if keep[i])) or re.match(
            r"\s*OR(?=[\s)]|$)", "".join(query[i] for i in range(closing + 1, len(query)) if keep[i])
        ):
            raise _not_combinable("(...)")
        keep[opening] = keep[closing] = False


def _tidy_connectors(text: str) -> str:
    """Drops AND / OR / NOT left dangling where an operator was removed."""
    previous = None
    while previous != text:
        previous = text
        text = re.sub(r"^(?:AND|OR)\s+", "", text)
        text = re.sub(r"\s+(?:AND|OR|NOT)$", "", text)
        text = re.sub(r"\b(AND|OR)\s+(?:AND|OR)\b", r"\1", text)
        text = re.sub(r"^(?:AND|OR|NOT)$", "", text)
    return text.strip()


def _validated(item: Filter) -> Filter:
    key, value = item.key, item.value
    if key in ("larger", "smaller"):
        _size_bytes(key, value)
    elif key in ("older_than", "newer_than"):
        _age_seconds(key, value)
    elif key in ("after", "before"):
        return Filter(key, _date_text(key, value), item.negated)
    elif key == "has":
        if value.lower() != "attachment":
            raise _bad(key, value, "attachment")
        return Filter(key, "attachment", item.negated)
    elif key == "is":
        if value.lower() not in _IS_VALUES:
            raise _bad(key, value, " or ".join(_IS_VALUES))
        return Filter(key, value.lower(), item.negated)
    elif key in ("from", "to", "cc"):
        if value.lower() == "me":
            return Filter(key, "me", item.negated)
    return item


def _size_bytes(key: str, value: str) -> int:
    match = re.fullmatch(r"(\d+(?:\.\d+)?)([kmg]?)b?", value.strip().lower())
    if not match:
        raise _bad(key, value, "a size such as 500K, 2M or 1048576")
    return int(float(match.group(1)) * _SIZE_UNITS[match.group(2)])


def _age_seconds(key: str, value: str) -> int:
    match = re.fullmatch(r"(\d+)([dwmy])", value.strip().lower())
    if not match:
        raise _bad(key, value, "a number and a unit d, w, m or y (e.g. 30d, 6m, 1y)")
    return int(match.group(1)) * _UNITS_SECONDS[match.group(2)]


def _date_stamp(key: str, value: str) -> int:
    text = value.strip()
    for pattern in ("%Y-%m-%d", "%Y/%m/%d"):
        try:
            return int(datetime.strptime(text, pattern).timestamp())
        except ValueError:
            continue
    raise _bad(key, value, "a date, YYYY-MM-DD or YYYY/MM/DD")


def _date_text(key: str, value: str) -> str:
    return datetime.fromtimestamp(_date_stamp(key, value)).strftime("%Y-%m-%d")


def _like(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _sender_condition(value: str) -> tuple[str, list[Any]]:
    """`from:` on messages.sender, stored as `Name <address>` or a bare address."""
    text = value.lower()
    column = "lower(coalesce(m.sender, ''))"
    if text.startswith("@") and len(text) > 1:
        domain = _like(text[1:])
        return (
            f"({column} LIKE ? ESCAPE '\\' OR {column} LIKE ? ESCAPE '\\'"
            f" OR {column} LIKE ? ESCAPE '\\' OR {column} LIKE ? ESCAPE '\\')",
            [f"%@{domain}>", f"%@{domain}", f"%@%.{domain}>", f"%@%.{domain}"],
        )
    if "@" in text:
        address = _like(text)
        return (
            f"({column} = ? OR {column} LIKE ? ESCAPE '\\')",
            [text, f"%<{address}>"],
        )
    return f"{column} LIKE ? ESCAPE '\\'", [f"%{_like(text)}%"]


def _recipient_condition(kind: str, value: str) -> tuple[str, list[Any]]:
    """`to:` / `cc:` on the recipients table (lower-cased address and domain)."""
    text = value.lower()
    head = "EXISTS (SELECT 1 FROM recipients r WHERE r.message = m.id AND r.kind = ? AND "
    if text.startswith("@") and len(text) > 1:
        domain = text[1:]
        return (
            head + "(r.domain = ? OR r.domain LIKE ? ESCAPE '\\'))",
            [kind, domain, f"%.{_like(domain)}"],
        )
    if "@" in text:
        return head + "r.address = ?)", [kind, text]
    pattern = f"%{_like(text)}%"
    return (
        head + "(r.address LIKE ? ESCAPE '\\' OR lower(coalesce(r.name, '')) LIKE ? ESCAPE '\\'))",
        [kind, pattern, pattern],
    )


def _location_exists(condition: str, *parameters: Any) -> tuple[str, list[Any]]:
    return f"EXISTS (SELECT 1 FROM locations l WHERE l.message = m.id AND {condition})", list(parameters)


def _mailbox_condition(value: str) -> tuple[str, list[Any]]:
    """`in:` matches a mailbox name exactly or as its last path segment."""
    text = value.lower()
    return _location_exists(
        "(lower(l.mailbox) = ? OR lower(l.mailbox) LIKE ? ESCAPE '\\')",
        text,
        f"%/{_like(text)}",
    )


def _own_condition(
    key: str, own_addresses: Callable[[], list[str]]
) -> tuple[str, list[Any]]:
    addresses = own_addresses()
    if not addresses:
        raise MailError(
            "no_own_address",
            f"{key}:me needs the addresses of your accounts, and Mail returned none.",
            "Use the address itself instead, e.g. from:jane@example.com.",
        )
    parts: list[str] = []
    parameters: list[Any] = []
    for address in addresses:
        if key == "from":
            sql, values = _sender_condition(address)
        else:
            sql, values = _recipient_condition(key, address)
        parts.append(sql)
        parameters.extend(values)
    return "(" + " OR ".join(parts) + ")", parameters


def filter_condition(
    item: Filter,
    now: int,
    own_addresses: Callable[[], list[str]],
) -> tuple[str, list[Any]] | None:
    """The SQL condition of one filter, over aliases m (messages).

    Returns None for `filename:`, which is an FTS clause (see filename_terms).
    A negation is the plain NOT of the positive condition, so with a message in
    several mailboxes "is:unread" means "unread in at least one" and "-is:unread"
    means "unread in none".
    """
    key, value = item.key, item.value
    if key == "filename":
        return None
    if key in ("from", "to", "cc"):
        if value == "me":
            sql, parameters = _own_condition(key, own_addresses)
        elif key == "from":
            sql, parameters = _sender_condition(value)
        else:
            sql, parameters = _recipient_condition(key, value)
    elif key == "has":
        sql, parameters = "m.has_attachment = 1", []
    elif key == "larger":
        sql, parameters = "coalesce(m.size, 0) > ?", [_size_bytes(key, value)]
    elif key == "smaller":
        sql, parameters = "coalesce(m.size, 0) < ?", [_size_bytes(key, value)]
    elif key == "older_than":
        sql, parameters = "coalesce(m.date_received, 0) < ?", [now - _age_seconds(key, value)]
    elif key == "newer_than":
        sql, parameters = "coalesce(m.date_received, 0) >= ?", [now - _age_seconds(key, value)]
    elif key == "after":
        sql, parameters = "coalesce(m.date_received, 0) >= ?", [_date_stamp(key, value)]
    elif key == "before":
        sql, parameters = "coalesce(m.date_received, 0) < ?", [_date_stamp(key, value)]
    elif key == "in":
        sql, parameters = _mailbox_condition(value)
    else:  # is
        if value == "unread":
            sql, parameters = _location_exists("l.read = 0")
        elif value == "read":
            sql, parameters = _location_exists("l.read = 1")
        elif value in ("flagged", "starred"):
            sql, parameters = _location_exists("l.flagged = 1")
        else:
            sql, parameters = "m.is_bulk = 1", []
    if item.negated:
        sql = f"NOT ({sql})"
    return sql, parameters


def filename_match(value: str) -> str:
    """FTS5 clause for a file name: raw attachments column OR its stem twin."""
    import mail_stem

    raw = value.replace('"', " ").strip()
    stem = mail_stem.stem_text(raw)
    clause = f'attachments : "{raw}"'
    if stem:
        clause = f'({clause} OR attachments_stem : "{stem}")'
    return clause


def build_conditions(
    filters: list[Filter],
    own_addresses: Callable[[], list[str]],
    now: int | None = None,
) -> tuple[list[str], list[Any], list[str], list[str]]:
    """SQL conditions and parameters, plus the positive and negated file names."""
    moment = int(time.time()) if now is None else now
    conditions: list[str] = []
    parameters: list[Any] = []
    wanted: list[str] = []
    unwanted: list[str] = []
    for item in filters:
        built = filter_condition(item, moment, own_addresses)
        if built is None:
            (unwanted if item.negated else wanted).append(item.value)
            continue
        conditions.append(built[0])
        parameters.extend(built[1])
    return conditions, parameters, wanted, unwanted


def describe(filters: list[Filter]) -> dict[str, list[str]]:
    """The filters as the caller should read them: `-key` for a negation."""
    described: dict[str, list[str]] = {}
    for item in filters:
        name = ("-" if item.negated else "") + item.key
        described.setdefault(name, []).append(item.value)
    return described
