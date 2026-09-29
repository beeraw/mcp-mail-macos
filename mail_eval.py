"""Measures how well search finds a message, so a change can be proven.

A ranking, stemming or quote-stripping ticket claims results got better; this
tool turns the claim into numbers. It holds a list of "query -> expected
message" pairs, runs each query through the same function the MCP tool calls,
and reports where the expected message landed.

    python3 mail_eval.py --generate 200 --seed 1     # subject pairs from the index
    python3 mail_eval.py --generate 200 --body 200   # plus 200 pairs from bodies
    python3 mail_eval.py --run                       # rank, MRR, recall
    python3 mail_eval.py --run --save-baseline before
    python3 mail_eval.py --run --compare before      # deltas, regressions

The index is only ever opened read-only, and a run never triggers a sync: a
sync in the middle of a measurement would change the corpus under it.

Pairs and results hold real mail text, so they live under eval/, which is
gitignored. Each pair has a "kind": "subject" (words of the subject) or "body"
(2-3 words close together in the message's own text, quoted replies left out,
re-read from the .emlx because the full-text table is contentless). A pair
without a kind is a subject pair. Body pairs are drawn with a generator seeded
apart from the subject one, so adding --body never changes the subject pairs of
a given seed. Pairs written by hand ("source": "manual") are kept when the
automatic ones are regenerated.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import sqlite3
import sys
from typing import Any

import config


class EvalError(Exception):
    """A problem the user can act on, reported without a traceback."""


PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
EVAL_DIR = os.path.join(PROJECT_ROOT, "eval")
DEFAULT_PAIRS = os.path.join(EVAL_DIR, "pairs.json")
RESULTS_DIR = os.path.join(EVAL_DIR, "results")

# Deep enough that "rank 30" is told apart from "not found".
SEARCH_LIMIT = 50
# Larger than any real index age, so search_all never syncs during a run.
NEVER_SYNC = 10**9

MIN_WORD_LENGTH = 4
MIN_QUERY_WORDS = 2
MAX_QUERY_WORDS = 4

# French and English function words, plus the FTS5 operators: a query made of
# "and"/"or"/"not"/"near" would be parsed as syntax rather than as words.
STOPWORDS = frozenset(
    """
    the and for with from that this your you are was were will have has had not
    but all any can our out about into over than then them they their there
    what when where which who how why also just more some such only very
    near or of to in on at by an as is it be we
    les des une aux dans pour avec sans sur sous par est sont etre été ete
    que qui quoi dont son ses leur leurs cette ces cet mon mes ton tes notre
    nos votre vos vous nous elle elles ils mais donc car ainsi alors aussi
    plus moins tres très bien tout tous toute toutes comme entre vers chez
    """.split()
)
# Reply and forward markers, in the languages mail clients write them in.
REPLY_PREFIXES = frozenset(
    {"re", "tr", "fw", "fwd", "rv", "aw", "wg", "sv", "vs", "ref", "objet", "subject"}
)

WORD_PATTERN = re.compile(r"[^\W_]+", re.UNICODE)

KIND_SUBJECT = "subject"
KIND_BODY = "body"
KINDS = (KIND_SUBJECT, KIND_BODY)

# A body text shorter than this (characters of own text) says too little.
MIN_BODY_CHARS = 120
BODY_QUERY_WORDS = (2, 3)
# Words of a body query must fall within this many consecutive tokens.
BODY_WINDOW = 8
BODY_MIN_WORD_LENGTH = 5
# A word matching more messages than this is too common to discriminate.
MAX_DOCUMENT_FREQUENCY = 200

# --------------------------------------------------------------------------
# Queries
# --------------------------------------------------------------------------

def usable_words(subject: str) -> list[str]:
    """Distinctive words of a subject, in order, without duplicates.

    Drops reply prefixes, stopwords, short tokens and bare numbers (a date or a
    reference alone matches half the mailbox and says nothing about ranking).
    """
    words: list[str] = []
    seen: set[str] = set()
    for token in WORD_PATTERN.findall(subject or ""):
        word = token.lower()
        if word in REPLY_PREFIXES or word in STOPWORDS:
            continue
        if len(word) < MIN_WORD_LENGTH or word.isdigit() or word in seen:
            continue
        seen.add(word)
        words.append(word)
    return words


def derive_query(subject: str, rng: random.Random) -> str | None:
    """A query of 2-4 distinctive subject words, or None if there are too few."""
    words = usable_words(subject)
    if len(words) < MIN_QUERY_WORDS:
        return None
    count = rng.randint(MIN_QUERY_WORDS, min(MAX_QUERY_WORDS, len(words)))
    chosen = sorted(rng.sample(range(len(words)), count))
    return " ".join(words[position] for position in chosen)


def own_text(text: str) -> str:
    """The part of a message its sender wrote, without quoted replies.

    Uses the index's own quote cutter in its eager mode: for an eval, a missed
    word costs nothing, a word taken from a quote would expect the wrong message.
    """
    import mail_index  # deferred: mail_index is only needed once bodies are read

    return mail_index.strip_quotes(text, eager=True)


def derive_body_query(
    text: str,
    subject: str,
    rng: random.Random,
    is_rare: Any = None,
) -> str | None:
    """2-3 distinctive words found close together in a message's own text.

    Words of the subject are avoided (that is what subject pairs test), as are
    stopwords, short words and anything containing a digit. `is_rare(word)`
    lets the caller reject words that match too many messages. Returns None if
    the text is too short or no window holds enough acceptable words.
    """
    own = own_text(text)
    if len(own) < MIN_BODY_CHARS:
        return None
    subject_words = {token.lower() for token in WORD_PATTERN.findall(subject or "")}
    tokens = [token.lower() for token in WORD_PATTERN.findall(own)]

    def acceptable(word: str) -> bool:
        return (
            len(word) >= BODY_MIN_WORD_LENGTH
            and not any(character.isdigit() for character in word)
            and word not in STOPWORDS
            and word not in REPLY_PREFIXES
            and word not in subject_words
        )

    starts = [i for i, token in enumerate(tokens) if acceptable(token)]
    rng.shuffle(starts)
    rejected: set[str] = set()
    for start in starts:
        window: list[str] = []
        for token in tokens[start : start + BODY_WINDOW]:
            if token in window or token in rejected or not acceptable(token):
                continue
            if is_rare is not None and not is_rare(token):
                rejected.add(token)
                continue
            window.append(token)
        if len(window) >= BODY_QUERY_WORDS[0]:
            count = rng.randint(BODY_QUERY_WORDS[0], min(BODY_QUERY_WORDS[1], len(window)))
            return " ".join(window[:count])
    return None


def generate_body_pairs(
    candidates: list[tuple[int, str]],
    count: int,
    rng: random.Random,
    load_text: Any,
    is_rare: Any = None,
    taken: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Draws up to `count` body pairs from (message id, subject) candidates.

    `load_text(id)` returns the message text or None (file missing). `taken`
    holds queries already used, so no query has two expected messages.
    """
    pool = list(candidates)
    rng.shuffle(pool)
    queries = set(taken or ())
    pairs: list[dict[str, Any]] = []
    for identifier, subject in pool:
        if len(pairs) >= count:
            break
        text = load_text(identifier)
        if not text:
            continue
        query = derive_body_query(text, subject, rng, is_rare)
        if query is None or query in queries:
            continue
        queries.add(query)
        pairs.append({"query": query, "expected": identifier, "source": "auto", "kind": KIND_BODY})
    return pairs


def generate_pairs(
    candidates: list[tuple[int, str]], count: int, rng: random.Random
) -> list[dict[str, Any]]:
    """Draws up to `count` auto pairs from (message id, subject) candidates.

    Subjects yielding too few words are skipped and replaced by the next draw.
    Identical queries are kept once: two messages with the same subject would
    make each the "wrong" answer of the other.
    """
    pool = list(candidates)
    rng.shuffle(pool)
    pairs: list[dict[str, Any]] = []
    queries: set[str] = set()
    for identifier, subject in pool:
        if len(pairs) >= count:
            break
        query = derive_query(subject, rng)
        if query is None or query in queries:
            continue
        queries.add(query)
        pairs.append({"query": query, "expected": identifier, "source": "auto", "kind": KIND_SUBJECT})
    return pairs


def merge_pairs(existing: list[dict[str, Any]], generated: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Manual pairs survive; the previous auto pairs are replaced."""
    manual = [pair for pair in existing if pair.get("source") == "manual"]
    return manual + generated


# --------------------------------------------------------------------------
# Metrics
# --------------------------------------------------------------------------

def rank_of(expected: Any, found: list[int]) -> int | None:
    """1-based position of the first expected id among results, else None."""
    wanted = set(expected) if isinstance(expected, (list, tuple, set)) else {expected}
    for position, identifier in enumerate(found, start=1):
        if identifier in wanted:
            return position
    return None


def aggregate(ranks: list[int | None], errors: int = 0) -> dict[str, Any]:
    """MRR, recall@1, recall@10 and the not-found count for a list of ranks.

    `ranks` covers the pairs that ran; `errors` counts the ones whose query
    failed, kept apart so a broken query is not mistaken for a missed message.
    """
    total = len(ranks)
    if total == 0:
        return {"pairs": 0, "mrr": 0.0, "recall_at_1": 0.0, "recall_at_10": 0.0, "not_found": 0, "errors": errors}
    return {
        "pairs": total,
        "errors": errors,
        "mrr": round(sum(1 / r for r in ranks if r) / total, 4),
        "recall_at_1": round(sum(1 for r in ranks if r is not None and r <= 1) / total, 4),
        "recall_at_10": round(sum(1 for r in ranks if r is not None and r <= 10) / total, 4),
        "not_found": sum(1 for r in ranks if r is None),
    }


def pair_kind(row: dict[str, Any]) -> str:
    """Kind of a pair or result row; files from before kinds existed are subject."""
    return row.get("kind") if row.get("kind") in KINDS else KIND_SUBJECT


def aggregate_rows(rows: list[dict[str, Any]]) -> dict[str, Any]:
    ran = [row["rank"] for row in rows if not row.get("error")]
    return aggregate(ran, len(rows) - len(ran))


def aggregate_by_kind(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Aggregates per kind present in the rows, plus "all"."""
    result = {
        kind: aggregate_rows([row for row in rows if pair_kind(row) == kind])
        for kind in KINDS
        if any(pair_kind(row) == kind for row in rows)
    }
    result["all"] = aggregate_rows(rows)
    return result


def _sort_rank(rank: int | None) -> float:
    return float("inf") if rank is None else rank


def compare(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    """Deltas between two saved results, and the pairs whose rank got worse.

    Pairs are matched on (query, expected); a pair present on one side only is
    ignored, since a regenerated file would otherwise flood the list.
    """
    keys = ("mrr", "recall_at_1", "recall_at_10", "not_found", "errors", "pairs")
    deltas = {
        key: round(after["aggregate"].get(key, 0) - before["aggregate"].get(key, 0), 4) for key in keys
    }

    def index(result: dict[str, Any]) -> dict[str, int | None]:
        return {
            json.dumps([row["query"], row["expected"]], sort_keys=True): row["rank"]
            for row in result.get("pairs", [])
            if not row.get("error")
        }

    # Recomputed from the rows, so results saved before kinds existed compare too.
    old_kinds = aggregate_by_kind(before.get("pairs", [])) if before.get("pairs") else {}
    new_kinds = aggregate_by_kind(after.get("pairs", [])) if after.get("pairs") else {}
    by_kind = {
        kind: {
            key: round(new_kinds[kind].get(key, 0) - old_kinds[kind].get(key, 0), 4) for key in keys
        }
        for kind in new_kinds
        if kind in old_kinds
    }

    old, new = index(before), index(after)
    worse = []
    for key, new_rank in new.items():
        if key in old and _sort_rank(new_rank) > _sort_rank(old[key]):
            query, expected = json.loads(key)
            worse.append({"query": query, "expected": expected, "before": old[key], "after": new_rank})
    worse.sort(key=lambda row: (_sort_rank(row["after"]) - _sort_rank(row["before"])), reverse=True)
    return {"deltas": deltas, "by_kind": by_kind, "worse": worse}


# --------------------------------------------------------------------------
# Files
# --------------------------------------------------------------------------

def valid_expected(expected: Any) -> bool:
    """A message id, or a non-empty list of them."""
    if isinstance(expected, list):
        return bool(expected) and all(valid_expected(item) for item in expected)
    return isinstance(expected, int) and not isinstance(expected, bool)


def valid_pair(pair: Any) -> bool:
    return (
        isinstance(pair, dict)
        and isinstance(pair.get("query"), str)
        and bool(pair["query"].strip())
        and valid_expected(pair.get("expected"))
    )


def load_pairs(path: str) -> tuple[list[dict[str, Any]], int]:
    """Reads a pairs file: (well-formed pairs, number of malformed ones skipped).

    A missing file is an empty list; an unreadable or corrupt one is an error
    the caller reports, not a traceback.
    """
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError:
        return [], 0
    except (OSError, ValueError) as error:
        raise EvalError(f"Cannot read pairs file {path}: {error}") from error
    pairs = data.get("pairs") if isinstance(data, dict) else data
    if not isinstance(pairs, list):
        raise EvalError(f"Cannot read pairs file {path}: expected a list of pairs")
    good = [pair for pair in pairs if valid_pair(pair)]
    return good, len(pairs) - len(good)


def save_pairs(path: str, pairs: list[dict[str, Any]]) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump({"pairs": pairs}, handle, ensure_ascii=False, indent=2)
        handle.write("\n")


def result_path(name: str) -> str:
    if not re.fullmatch(r"[\w][\w.-]*", name):
        raise EvalError(f"Invalid result name: {name!r}")
    return os.path.join(RESULTS_DIR, f"{name}.json")


# --------------------------------------------------------------------------
# Against the index
# --------------------------------------------------------------------------

def open_readonly(path: str) -> sqlite3.Connection:
    """The index, opened so that no write is possible."""
    if not os.path.isfile(path):
        raise EvalError(f"No index at {path}. Build it first: python3 mail_index.py --build")
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


def load_candidates(path: str) -> list[tuple[int, str]]:
    connection = open_readonly(path)
    try:
        rows = connection.execute(
            "SELECT id, subject FROM messages WHERE subject IS NOT NULL AND subject != ''"
        ).fetchall()
    finally:
        connection.close()
    return [(int(identifier), subject) for identifier, subject in rows]


def load_body_candidates(path: str) -> list[tuple[int, str]]:
    """Every indexed message as (id, subject), subject possibly empty."""
    connection = open_readonly(path)
    try:
        rows = connection.execute("SELECT id, COALESCE(subject, '') FROM messages").fetchall()
    finally:
        connection.close()
    return [(int(identifier), subject) for identifier, subject in rows]


def body_sources(path: str) -> tuple[Any, Any, Any]:
    """(load_text, is_rare, close) for body pairs, against the real store.

    is_rare counts the messages matching a word in the full-text table, which
    is cheap, and rejects words matching more than MAX_DOCUMENT_FREQUENCY.
    """
    import mail_index  # deferred: only body generation reads .emlx files

    store = mail_index.find_store()
    connection = open_readonly(path)
    cache: dict[str, bool] = {}

    def load_text(identifier: int) -> str | None:
        emlx = mail_index.find_message_file(store, identifier)
        if emlx is None:
            return None
        return mail_index.extract_text(emlx)[1]

    def is_rare(word: str) -> bool:
        if word not in cache:
            try:
                hits = connection.execute(
                    "SELECT count(*) FROM (SELECT rowid FROM messages_fts WHERE messages_fts MATCH ? LIMIT ?)",
                    (f'"{word}"', MAX_DOCUMENT_FREQUENCY + 1),
                ).fetchone()[0]
            except sqlite3.Error:
                hits = 0
            cache[word] = 0 < hits <= MAX_DOCUMENT_FREQUENCY
        return cache[word]

    return load_text, is_rare, connection.close


def run_pairs(pairs: list[dict[str, Any]]) -> dict[str, Any]:
    """Runs every pair through search_all and ranks the expected message.

    A MailError (index missing, ...) affects every query, so it aborts the run.
    Any other failure is specific to one query and is counted as an error.
    """
    import mail_search  # deferred: pulls in the Mail tooling, not needed to generate
    from mail_tools import MailError

    rows = []
    for pair in pairs:
        row = {
            "query": pair["query"],
            "expected": pair["expected"],
            "source": pair.get("source", "auto"),
            "kind": pair_kind(pair),
        }
        try:
            answer = mail_search.search_all(
                query=pair["query"], limit=SEARCH_LIMIT, max_age_minutes=NEVER_SYNC
            )
        except MailError as error:
            if error.code == "invalid_query":
                row.update(rank=None, error=error.code)
                rows.append(row)
                continue
            raise EvalError(f"Search failed ({error.code}): {error.message} {error.hint or ''}".strip()) from error
        except sqlite3.Error as error:
            row.update(rank=None, error=error.__class__.__name__)
            rows.append(row)
            continue
        found = [message["mail_id"] for message in answer["messages"]]
        row["rank"] = rank_of(pair["expected"], found)
        rows.append(row)
    return {"aggregate": aggregate_rows(rows), "by_kind": aggregate_by_kind(rows), "pairs": rows}


# --------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------

def format_aggregate(values: dict[str, Any]) -> str:
    return (
        f"pairs {values['pairs']}   MRR {values['mrr']:.4f}   "
        f"recall@1 {values['recall_at_1']:.4f}   recall@10 {values['recall_at_10']:.4f}   "
        f"not found {values['not_found']}   errors {values.get('errors', 0)}"
    )


def format_by_kind(by_kind: dict[str, dict[str, Any]]) -> str:
    """One line per kind; nothing when only one kind ran (it would repeat "all")."""
    if len(by_kind) <= 2:
        return format_aggregate(by_kind["all"])
    return "\n".join(f"{kind:<8} {format_aggregate(by_kind[kind])}" for kind in (*KINDS, "all") if kind in by_kind)


def format_comparison(name: str, comparison: dict[str, Any]) -> str:
    deltas = comparison["deltas"]
    lines = [
        f"compared with '{name}':",
        f"  MRR {deltas['mrr']:+.4f}   recall@1 {deltas['recall_at_1']:+.4f}   "
        f"recall@10 {deltas['recall_at_10']:+.4f}   not found {deltas['not_found']:+d}   errors {deltas['errors']:+d}   "
        f"pairs {deltas['pairs']:+d}",
    ]
    for kind, kind_deltas in comparison.get("by_kind", {}).items():
        if kind == "all" or len(comparison["by_kind"]) <= 2:
            continue
        lines.append(
            f"  {kind:<8} MRR {kind_deltas['mrr']:+.4f}   recall@1 {kind_deltas['recall_at_1']:+.4f}   "
            f"recall@10 {kind_deltas['recall_at_10']:+.4f}   not found {kind_deltas['not_found']:+.0f}   "
            f"pairs {kind_deltas['pairs']:+.0f}"
        )
    lines.append(f"  {len(comparison['worse'])} pair(s) with a worse rank")
    for row in comparison["worse"][:20]:
        lines.append(f"    {row['before']} -> {row['after']}   {row['query']}")
    if len(comparison["worse"]) > 20:
        lines.append(f"    ... and {len(comparison['worse']) - 20} more (see --json)")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Evaluate search relevance against the local index.")
    parser.add_argument("--generate", type=int, metavar="N", help="build N automatic subject pairs from the index")
    parser.add_argument("--body", type=int, metavar="N", help="also build N automatic pairs from message bodies")
    parser.add_argument("--seed", type=int, help="seed for --generate, to reproduce a draw")
    parser.add_argument("--pairs", default=DEFAULT_PAIRS, help=f"pairs file (default: {DEFAULT_PAIRS})")
    parser.add_argument("--run", action="store_true", help="run every pair and report ranks")
    parser.add_argument("--json", action="store_true", help="machine-readable output for --run")
    parser.add_argument("--save-baseline", metavar="NAME", help="store this run as eval/results/NAME.json")
    parser.add_argument("--compare", metavar="NAME", help="show deltas against a stored run")
    arguments = parser.parse_args()
    try:
        return _main(parser, arguments)
    except EvalError as error:
        print(str(error), file=sys.stderr)
        return 1


def _main(parser: argparse.ArgumentParser, arguments: argparse.Namespace) -> int:

    if arguments.body is not None and arguments.generate is None:
        arguments.generate = 0
    if arguments.generate is None and not arguments.run:
        parser.print_help()
        return 2
    if (arguments.save_baseline or arguments.compare) and not arguments.run:
        parser.error("--save-baseline and --compare need --run")
    # Checked before anything runs, so a typo does not cost a whole run.
    for name in (arguments.save_baseline, arguments.compare):
        if name:
            result_path(name)

    if arguments.generate is not None:
        index_path = config.get("index_path")
        candidates = load_candidates(index_path)
        generated = generate_pairs(candidates, arguments.generate, random.Random(arguments.seed))
        wanted = arguments.generate
        if arguments.body:
            # Seeded apart: the subject draw above is the same with or without --body.
            body_rng = random.Random(None if arguments.seed is None else f"body-{arguments.seed}")
            load_text, is_rare, close = body_sources(index_path)
            try:
                generated += generate_body_pairs(
                    load_body_candidates(index_path),
                    arguments.body,
                    body_rng,
                    load_text,
                    is_rare,
                    {pair["query"] for pair in generated},
                )
            finally:
                close()
            wanted += arguments.body
        if not generated:
            print(f"No usable subject found: {arguments.pairs} left untouched.", file=sys.stderr)
            if not arguments.run:
                return 1
        else:
            existing, _ = load_pairs(arguments.pairs)
            merged = merge_pairs(existing, generated)
            save_pairs(arguments.pairs, merged)
            kept = len(merged) - len(generated)
            counts = ", ".join(
                f"{sum(1 for pair in generated if pair_kind(pair) == kind)} {kind}" for kind in KINDS
            )
            print(f"{len(generated)} auto pair(s) written ({counts}), {kept} manual kept -> {arguments.pairs}")
            if len(generated) < wanted:
                print(f"only {len(generated)} of {wanted} requested pairs had usable text")

    if not arguments.run:
        return 0

    pairs, skipped = load_pairs(arguments.pairs)
    if skipped:
        print(f"{skipped} malformed pair(s) skipped in {arguments.pairs}", file=sys.stderr)
    if not pairs:
        print(f"No pairs in {arguments.pairs}. Run --generate first.", file=sys.stderr)
        return 1
    result = run_pairs(pairs)

    comparison = None
    if arguments.compare:
        try:
            with open(result_path(arguments.compare), "r", encoding="utf-8") as handle:
                previous = json.load(handle)
        except (OSError, ValueError) as error:
            print(f"Cannot read result '{arguments.compare}': {error}", file=sys.stderr)
            return 1
        if not isinstance(previous, dict) or "aggregate" not in previous:
            print(f"Result '{arguments.compare}' is not a saved run.", file=sys.stderr)
            return 1
        comparison = compare(previous, result)

    if arguments.save_baseline:
        path = result_path(arguments.save_baseline)
        os.makedirs(RESULTS_DIR, exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(result, handle, ensure_ascii=False, indent=2)
            handle.write("\n")

    if arguments.json:
        payload = dict(result)
        if comparison:
            payload["comparison"] = comparison
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    print(format_by_kind(result["by_kind"]))
    if comparison:
        print(format_comparison(arguments.compare, comparison))
    if arguments.save_baseline:
        print(f"saved -> {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
