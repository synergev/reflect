"""Read this project's captured feedback and maintain its review ledger."""

import argparse
from dataclasses import asdict, dataclass, fields
from datetime import datetime, timedelta, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import subprocess
from typing import Optional

# Preview must not create bytecode files.
sys.dont_write_bytecode = True

from capture import Capture, atomic_json, record_id, state_root


def time_value(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.astimezone()


def sources(args: argparse.Namespace, root: Path) -> dict:
    session = args.session or (None if args.past else os.environ.get("CODEX_THREAD_ID"))
    if not args.past and not session:
        raise ValueError("Current task ID unavailable; pass --session explicitly")
    since = (
        time_value(args.since)
        if args.since
        else datetime.now(timezone.utc) - timedelta(days=args.days)
    )
    until = time_value(args.until) if args.until else datetime.now(timezone.utc)
    if args.past and since >= until:
        raise ValueError("--since must precede --until (exclusive)")
    values, warnings = [], []
    for path in sorted((root / "queue").glob("*.json")):
        try:
            item = Capture(**json.loads(path.read_text()))
            if session and item.session_id != session:
                continue
            if args.past and not since <= time_value(item.captured_at) < until:
                continue
            values.append(asdict(item))
        except (OSError, ValueError, TypeError) as exc:
            warnings.append(str(path) + ": " + type(exc).__name__)
    if (root / "capture-error.json").exists():
        warnings.append("A capture failed; inspect " + str(root / "capture-error.json"))
    end = args.offset + args.limit
    return {
        "captures": values[args.offset : end],
        "next_offset": end if end < len(values) else None,
        "since": since.isoformat() if args.past else None,
        "until": until.isoformat() if args.past else None,
        "warnings": warnings,
    }


@dataclass
class Review:
    key: str
    status: str
    source_ids: list[str]
    lesson: str
    destination: str
    evidence: str
    conflict: Optional[str] = None
    pr_url: Optional[str] = None
    revision: str = ""

    @classmethod
    def parse(cls, value: dict) -> "Review":
        if not isinstance(value, dict) or set(value) - {
            field.name for field in fields(cls)
        }:
            raise ValueError("Unknown review fields")
        review = cls(**value)
        for field in ("key", "status", "lesson", "destination", "evidence"):
            if (
                not isinstance(getattr(review, field), str)
                or not getattr(review, field).strip()
            ):
                raise ValueError("Missing review " + field)
        if review.status not in ("pending", "dismissed", "proposed", "adopted"):
            raise ValueError("Unsupported status")
        if (
            not isinstance(review.source_ids, list)
            or not review.source_ids
            or any(
                not isinstance(v, str) or not re.fullmatch(r"[0-9a-f]{64}", v)
                for v in review.source_ids
            )
        ):
            raise ValueError("source_ids must contain capture IDs")
        if not isinstance(review.revision, str):
            raise ValueError("Invalid revision")
        for value in (review.conflict, review.pr_url):
            if value is not None and not isinstance(value, str):
                raise ValueError("Invalid optional review text")
        if review.conflict and review.status != "pending":
            raise ValueError("Unresolved conflicts must stay pending")
        return review


def save_review(root: Path, review: Review) -> dict:
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = root / "reviews" / (record_id("lesson", review.key) + ".json")
    # Serializes only explicit ledger writes; capture uses independent files.
    with (root / ".review.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        current = json.loads(path.read_text()) if path.exists() else {}
        if review.revision != current.get("revision", ""):
            raise ValueError("Review changed since it was read; reread before updating")
        value = asdict(review)
        value["revision"] = hashlib.sha256(
            json.dumps(value, sort_keys=True).encode()
        ).hexdigest()
        atomic_json(path, value)
    return value


def reviews(root: Path, args: argparse.Namespace) -> dict:
    values, warnings = [], []
    for path in sorted((root / "reviews").glob("*.json")):
        try:
            value = asdict(Review.parse(json.loads(path.read_text())))
            if args.conflicts and not value["conflict"]:
                continue
            values.append(value)
        except (OSError, ValueError, TypeError) as exc:
            warnings.append(str(path) + ": " + str(exc))
    end = args.offset + args.limit
    return {
        "reviews": values[args.offset : end],
        "next_offset": end if end < len(values) else None,
        "warnings": warnings,
    }


def nudge(root: Path) -> Optional[str]:
    reviewed, pending = set(), []
    for path in (root / "reviews").glob("*.json"):
        try:
            reviewed.update(Review.parse(json.loads(path.read_text())).source_ids)
        except (OSError, ValueError, TypeError):
            pass
    for path in (root / "queue").glob("*.json"):
        try:
            item = Capture(**json.loads(path.read_text()))
        except (OSError, ValueError, TypeError):
            continue
        if item.id not in reviewed:
            pending.append(item.captured_at)
    parts = []
    if pending:
        parts.append(
            "%d unreviewed lesson candidate(s) since %s"
            % (len(pending), min(pending)[:10])
        )
    if (root / "report.md").exists():
        parts.append("daily report at " + str(root / "report.md"))
    if not parts:
        return None
    return (
        "Reflect: "
        + "; ".join(parts)
        + ". Mention this to the user once, briefly, and offer to reflect on past"
        " conversations."
    )


def hook_nudge() -> None:
    """SessionStart hook: stdout becomes developer context for the new task."""
    try:
        event = json.load(sys.stdin)
        if event.get("source") != "startup":
            return
        message = nudge(state_root(event["cwd"]))
    except Exception:
        # Never interrupt session start.
        return
    if message:
        print(message)


def positive(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("Must be positive")
    return number


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    source = sub.add_parser(
        "sources", help="Read captured feedback; current task by default"
    )
    choice = source.add_mutually_exclusive_group()
    choice.add_argument("--session")
    choice.add_argument("--past", action="store_true")
    source.add_argument("--days", type=positive, default=7)
    source.add_argument("--since", help="Inclusive ISO timestamp")
    source.add_argument("--until", help="Exclusive ISO timestamp")
    ledger = sub.add_parser("reviews", help="Read saved lesson decisions")
    ledger.add_argument("--conflicts", action="store_true")
    save = sub.add_parser(
        "save-review", help="Save a decision; never use during preview"
    )
    save.add_argument("input", type=Path)
    sub.add_parser(
        "nudge", help="SessionStart hook: mention unreviewed captures and reports"
    )
    for command in (source, ledger, save):
        command.add_argument(
            "--cwd",
            default=os.getcwd(),
            help="Any directory in this project's checkout or worktree",
        )
    for command in (source, ledger):
        command.add_argument("--offset", type=int, default=0)
        command.add_argument("--limit", type=positive, default=25)
    args = parser.parse_args()
    if args.command == "nudge":
        hook_nudge()
        return
    try:
        if getattr(args, "offset", 0) < 0:
            raise ValueError("Offset must be nonnegative")
        root = state_root(args.cwd)
        if args.command == "sources":
            value = sources(args, root)
        elif args.command == "reviews":
            value = reviews(root, args)
        else:
            value = save_review(root, Review.parse(json.loads(args.input.read_text())))
        print(json.dumps(value, ensure_ascii=False, indent=2))
    except (
        OSError,
        ValueError,
        TypeError,
        KeyError,
        subprocess.SubprocessError,
    ) as exc:
        parser.exit(1, "reflect: " + str(exc) + "\n")


if __name__ == "__main__":
    main()
