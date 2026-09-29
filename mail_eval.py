"""Measures how well search finds a message, so a change can be proven.

A ranking, stemming or quote-stripping ticket claims results got better; this
tool turns the claim into numbers. It holds a list of "query -> expected
message" pairs, runs each query through the same function the MCP tool calls,
and reports where the expected message landed.

    python3 mail_eval.py --generate 200 --seed 1     # pairs from the index
    python3 mail_eval.py --run                       # rank, MRR, recall
    python3 mail_eval.py --run --save-baseline before
    python3 mail_eval.py --run --compare before      # deltas, regressions

The index is only ever opened read-only, and a run never triggers a sync: a
sync in the middle of a measurement would change the corpus under it.

Pairs and results hold real mail subjects, so they live under eval/, which is
gitignored. Generated pairs use the subject only: the body is not stored (the
full-text table is contentless), so body queries would mean re-reading every
.emlx, which v1 does not do. Pairs written by hand ("source": "manual") are
kept when the automatic ones are regenerated.
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
        pairs.append({"query": query, "expected": identifier, "source": "auto"})
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

    old, new = index(before), index(after)
    worse = []
    for key, new_rank in new.items():
        if key in old and _sort_rank(new_rank) > _sort_rank(old[key]):
            query, expected = json.loads(key)
            worse.append({"query": query, "expected": expected, "before": old[key], "after": new_rank})
    worse.sort(key=lambda row: (_sort_rank(row["after"]) - _sort_rank(row["before"])), reverse=True)
    return {"deltas": deltas, "worse": worse}


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


def run_pairs(pairs: list[dict[str, Any]]) -> dict[str, Any]:
    """Runs every pair through search_all and ranks the expected message.

    A MailError (index missing, ...) affects every query, so it aborts the run.
    Any other failure is specific to one query and is counted as an error.
    """
    import mail_search  # deferred: pulls in the Mail tooling, not needed to generate
    from mail_tools import MailError

    rows = []
    for pair in pairs:
        row = {"query": pair["query"], "expected": pair["expected"], "source": pair.get("source", "auto")}
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
    ran = [row["rank"] for row in rows if not row.get("error")]
    errors = len(rows) - len(ran)
    return {"aggregate": aggregate(ran, errors), "pairs": rows}


# --------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------

def format_aggregate(values: dict[str, Any]) -> str:
    return (
        f"pairs {values['pairs']}   MRR {values['mrr']:.4f}   "
        f"recall@1 {values['recall_at_1']:.4f}   recall@10 {values['recall_at_10']:.4f}   "
        f"not found {values['not_found']}   errors {values.get('errors', 0)}"
    )


def format_comparison(name: str, comparison: dict[str, Any]) -> str:
    deltas = comparison["deltas"]
    lines = [
        f"compared with '{name}':",
        f"  MRR {deltas['mrr']:+.4f}   recall@1 {deltas['recall_at_1']:+.4f}   "
        f"recall@10 {deltas['recall_at_10']:+.4f}   not found {deltas['not_found']:+d}   errors {deltas['errors']:+d}   "
        f"pairs {deltas['pairs']:+d}",
        f"  {len(comparison['worse'])} pair(s) with a worse rank",
    ]
    for row in comparison["worse"][:20]:
        lines.append(f"    {row['before']} -> {row['after']}   {row['query']}")
    if len(comparison["worse"]) > 20:
        lines.append(f"    ... and {len(comparison['worse']) - 20} more (see --json)")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Evaluate search relevance against the local index.")
    parser.add_argument("--generate", type=int, metavar="N", help="build N automatic pairs from the index")
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
        candidates = load_candidates(config.get("index_path"))
        generated = generate_pairs(candidates, arguments.generate, random.Random(arguments.seed))
        if not generated:
            print(f"No usable subject found: {arguments.pairs} left untouched.", file=sys.stderr)
            if not arguments.run:
                return 1
        else:
            existing, _ = load_pairs(arguments.pairs)
            merged = merge_pairs(existing, generated)
            save_pairs(arguments.pairs, merged)
            kept = len(merged) - len(generated)
            print(f"{len(generated)} auto pair(s) written, {kept} manual kept -> {arguments.pairs}")
            if len(generated) < arguments.generate:
                print(f"only {len(generated)} of {arguments.generate} messages had a usable subject")

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

    print(format_aggregate(result["aggregate"]))
    if comparison:
        print(format_comparison(arguments.compare, comparison))
    if arguments.save_baseline:
        print(f"saved -> {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
