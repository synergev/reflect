"""Quiet UserPromptSubmit capture.

Marked prompts are saved locally. Other prompts are sent, with obvious secrets
redacted, to TypeSafe's Jev classifier via OpenRouter only when
REFLECT_OPENROUTER_API_KEY is set in the environment or the main checkout's .env.
"""

import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import urllib.request
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Optional

JEV_URL = "https://openrouter.ai/api/v1/systemone"
JEV_MODEL = "~typesafe/jev-latest"
KEY_ENV = "REFLECT_OPENROUTER_API_KEY"
THRESHOLD = 0.8
MAX_CHARS = 8000
LESSON = {
    "type": "noul",
    "instructions": (
        "Does this message from a developer to their coding agent state a reusable"
        " lesson for future work in this project, rather than a one-off task request?"
    ),
    "criteria": {
        "true": "A correction of the agent's approach, a stated preference, a fact"
        " about the project, or a workflow rule that should apply next time.",
        "false": "A task request, question, or reply that only matters right now.",
    },
}
# Best-effort: covers common pasted formats, not every credential shape.
APP_CONTEXT = re.compile(
    r"<in-app-browser-context\b[^>]*>[\s\S]*?</in-app-browser-context>\s*"
)
SECRET_NAME = r"[\w.-]*(?:key|token|secret|passw(?:or)?d)[\w.-]*"
SECRETS = [
    (
        re.compile(
            r"-----BEGIN[A-Z ]*PRIVATE KEY-----[\s\S]*?"
            r"(?:-----END[A-Z ]*PRIVATE KEY-----|\Z)"
        ),
        "[REDACTED]",
    ),
    # Inline pairs in JSON, YAML, dicts, and call arguments.
    (
        re.compile(
            r"(?i)(\b" + SECRET_NAME + r"[\"']?[ \t]*[=:][ \t]*)"
            r"(\"[^\"\n]*\"|'[^'\n]*'|[^\s,;})\]]+)"
        ),
        r"\1[REDACTED]",
    ),
    # Whole-line values, which may contain spaces.
    (
        re.compile(
            r"(?im)^([ \t]*(?:export[ \t]+)?" + SECRET_NAME + r"[ \t]*[=:][ \t]*)\S.*$"
        ),
        r"\1[REDACTED]",
    ),
    (re.compile(r"(?i)\bbearer\s+[\w.~+/=-]+"), "Bearer [REDACTED]"),
    (re.compile(r"(?m)((?:^|\s)(?:-u|--user)[ \t=]+[^\s:]+:)\S+"), r"\1[REDACTED]"),
    (
        re.compile(
            r"\b(?:sk|pk|rk)-[\w-]{16,}|\bgh[pousr]_\w{20,}|\bxox[abpr]-[\w-]{10,}"
            r"|\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"
        ),
        "[REDACTED]",
    ),
    (re.compile(r"\beyJ[\w-]+\.[\w-]+\.[\w-]+"), "[REDACTED]"),
    (re.compile(r"(\b[a-z][\w+.-]*://[^\s:/@]+:)[^\s@]+@", re.I), r"\1[REDACTED]@"),
]


def user_text(prompt: str) -> str:
    """Drop context the Codex app adds to a prompt; it isn't the user's feedback."""
    prompt = APP_CONTEXT.sub("", prompt)
    return re.sub(r"\A\s*## My request:\s*", "", prompt)


def remember_text(prompt: str) -> Optional[str]:
    match = re.match(
        r"\A\s*(?:please\s+)?remember\b\s*(\S[\s\S]*)\Z", prompt, re.IGNORECASE
    )
    return match[1] if match else None


def redact(text: str) -> str:
    for pattern, replacement in SECRETS:
        text = pattern.sub(replacement, text)
    return text


def classify(text: str, key: str) -> float:
    """Return Jev's calibrated probability that the text states a reusable lesson."""
    request = urllib.request.Request(
        JEV_URL,
        data=json.dumps(
            {"model": JEV_MODEL, "state": text, "questions": {"lesson": LESSON}}
        ).encode(),
        headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=8) as response:
        return float(json.load(response)["answers"]["lesson"]["noul"])


def project_root(cwd: str) -> Path:
    result = subprocess.run(
        ["git", "-C", cwd, "worktree", "list", "--porcelain", "-z"],
        capture_output=True,
        text=True,
        check=True,
        timeout=2,
    )
    first = result.stdout.split("\0\0", 1)[0].split("\0")
    if not first[0].startswith("worktree ") or "bare" in first:
        raise ValueError("Reflect requires a main project checkout")
    return Path(first[0][len("worktree ") :]).resolve()


def state_root(cwd: str) -> Path:
    return project_root(cwd) / ".codex" / "reflect"


def api_key(cwd: str) -> Optional[str]:
    """Hooks don't load .env, so fall back to the main checkout's .env file."""
    if os.environ.get(KEY_ENV):
        return os.environ[KEY_ENV]
    try:
        lines = (project_root(cwd) / ".env").read_text().splitlines()
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    for line in lines:
        name, _, value = line.strip().removeprefix("export ").partition("=")
        value = value.strip().strip("\"'")
        if name.strip() == KEY_ENV and value:
            return value
    return None


def record_id(session_id: str, turn_id: str) -> str:
    return hashlib.sha256((session_id + "\0" + turn_id).encode()).hexdigest()


def ensure_state(root: Path) -> None:
    """Create the state folder, ignored by Git so feedback is never committed."""
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    ignore = root / ".gitignore"
    if not ignore.exists():
        ignore.write_text("*\n")


def atomic_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=".reflect-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


@dataclass
class Capture:
    id: str
    session_id: str
    turn_id: str
    captured_at: str
    cwd: str
    text: str
    transcript_path: Optional[str] = None
    kind: str = "explicit"
    score: Optional[float] = None

    @classmethod
    def from_event(cls, event: dict) -> Optional["Capture"]:
        if (
            not isinstance(event, dict)
            or event.get("hook_event_name") != "UserPromptSubmit"
        ):
            return None
        prompt = event.get("prompt")
        if not isinstance(prompt, str):
            raise ValueError("Hook prompt is missing or is not text")
        prompt = user_text(prompt)
        text, kind, score = remember_text(prompt), "explicit", None
        if text is None:
            key = api_key(str(event.get("cwd") or ""))
            if not key:
                return None
            text, kind = redact(prompt)[:MAX_CHARS], "detected"
            score = classify(text, key)
            if score < THRESHOLD:
                return None
        for key in ("session_id", "turn_id", "cwd"):
            if not isinstance(event.get(key), str) or not event[key].strip():
                raise ValueError("Hook is missing " + key)
        transcript = event.get("transcript_path")
        if transcript is not None and not isinstance(transcript, str):
            raise ValueError("Invalid transcript reference")
        return cls(
            record_id(event["session_id"], event["turn_id"]),
            event["session_id"],
            event["turn_id"],
            datetime.now(timezone.utc).isoformat(),
            event["cwd"],
            text,
            transcript,
            kind,
            score,
        )


def capture(event: dict, root: Path) -> None:
    candidate = Capture.from_event(event)
    if candidate is None:
        return
    path = root / "queue" / (candidate.id + ".json")
    # A hard link publishes a complete record only if the turn does not exist.
    # Unlike replace, concurrent redelivery cannot overwrite the first capture.
    ensure_state(root)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=".capture-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(asdict(candidate), stream, ensure_ascii=False)
        try:
            os.link(name, path)
        except FileExistsError:
            existing = json.loads(path.read_text())
            if existing.get("text") != candidate.text:
                raise ValueError(
                    "Different feedback received for an already captured turn"
                )
    finally:
        os.unlink(name)


def main() -> None:
    root = None
    try:
        event = json.load(sys.stdin)
        prompt = event.get("prompt")
        # Skip prompts that can never be captured.
        if not isinstance(prompt, str) or (
            remember_text(user_text(prompt)) is None and not api_key(event["cwd"])
        ):
            return
        root = state_root(event["cwd"])
        capture(event, root)
    except Exception as exc:
        # Never interrupt the agent or echo private input.
        if root is None:
            return
        try:
            ensure_state(root)
            atomic_json(
                root / "capture-error.json",
                {
                    "at": datetime.now(timezone.utc).isoformat(),
                    "error": type(exc).__name__,
                    "message": "Capture failed; some feedback may be missing.",
                },
            )
        except OSError:
            pass


if __name__ == "__main__":
    main()
