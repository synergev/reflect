"""Run: python3 -B -m unittest discover -s scripts -p 'test_*.py' -v"""

from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import urllib.error

import capture
import reflect


class ReflectTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        environ = patch.dict(os.environ)
        environ.start()
        self.addCleanup(environ.stop)
        os.environ.pop("REFLECT_OPENROUTER_API_KEY", None)
        self.event = {
            "hook_event_name": "UserPromptSubmit",
            "session_id": "s1",
            "turn_id": "t1",
            "cwd": str(self.home),
            "prompt": "remember use the event table\nKeep history.",
        }

    def args(self, **changes):
        values = dict(
            session="s1", past=False, since=None, until=None, days=7, offset=0, limit=25
        )
        values.update(changes)
        return reflect.argparse.Namespace(**values)

    def jev(self, score):
        body = {"answers": {"lesson": {"type": "noul", "noul": score}}}
        return patch.object(
            capture.urllib.request,
            "urlopen",
            return_value=io.BytesIO(json.dumps(body).encode()),
        )

    def queue(self, root):
        return [
            json.loads(p.read_text()) for p in sorted((root / "queue").glob("*.json"))
        ]

    def test_exact_marker_and_verbatim_remainder(self):
        for text in [
            "remember",
            "remember   ",
            "remembering X",
            "please remember",
            "please remembering X",
            "could you please remember X",
            "I remember X",
            '"remember X"',
            "> remember X",
            "```\nremember X\n```",
        ]:
            self.assertIsNone(capture.remember_text(text), text)
        self.assertEqual(
            capture.remember_text(" \tREMEMBER use X\n  then Y  "), "use X\n  then Y  "
        )
        self.assertEqual(
            capture.remember_text(" \tPLEASE REMEMBER use X\n  then Y  "),
            "use X\n  then Y  ",
        )
        for remainder in (": X", "-X", "– X", "— X", "***", ":", "-", "\nuse X"):
            for prefix in (
                "remember",
                "remember ",
                "please remember",
                "please remember ",
            ):
                prompt = prefix + remainder
                with self.subTest(prompt=prompt):
                    expected = remainder.lstrip()
                    self.assertEqual(capture.remember_text(prompt), expected)
                    candidate = capture.Capture.from_event(
                        dict(self.event, prompt=prompt)
                    )
                    self.assertEqual(candidate.text, expected)

    def test_quiet_capture_concurrent_and_redelivery(self):
        root = self.home / "reflect"
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(lambda _: capture.capture(self.event, root), range(30)))
            list(
                pool.map(
                    lambda n: capture.capture(dict(self.event, turn_id=str(n)), root),
                    range(20),
                )
            )
        self.assertEqual(len(list((root / "queue").glob("*.json"))), 21)
        item = json.loads(
            (root / "queue" / (capture.record_id("s1", "t1") + ".json")).read_text()
        )
        self.assertEqual(item["text"], "use the event table\nKeep history.")
        with self.assertRaises(ValueError):
            capture.capture(dict(self.event, prompt="remember different"), root)

    def test_quiet_hook_skips_unmarked_input_and_fails_open(self):
        root = self.home / ".codex/reflect"
        for prompt in ("ordinary request", "remember use X"):
            out, err = io.StringIO(), io.StringIO()
            with patch(
                "sys.stdin", io.StringIO(json.dumps(dict(self.event, prompt=prompt)))
            ), patch.object(
                capture, "state_root", return_value=root
            ) as resolve, redirect_stdout(
                out
            ), redirect_stderr(
                err
            ):
                capture.main()
                self.assertEqual(resolve.call_count, int(prompt.startswith("remember")))
            self.assertEqual(out.getvalue() + err.getvalue(), "")
        self.assertEqual(len(list((root / "queue").glob("*.json"))), 1)
        with patch("sys.stdin", io.StringIO(json.dumps(self.event))), patch.object(
            capture, "state_root", return_value=root
        ), patch.object(capture, "capture", side_effect=PermissionError):
            capture.main()
        self.assertTrue((root / "capture-error.json").exists())
        with patch("sys.stdin", io.StringIO("invalid")):
            capture.main()

    def test_project_inbox_current_past_pagination_and_bad_records(self):
        root = self.home / "project/.codex/reflect"
        other = self.home / "other/.codex/reflect"
        capture.capture(self.event, root)
        capture.capture(dict(self.event, session_id="archived-task"), root)
        capture.capture(dict(self.event, session_id="other-project"), other)
        (root / "queue/broken.json").write_text("[]")
        current = reflect.sources(self.args(), root)
        self.assertEqual([c["session_id"] for c in current["captures"]], ["s1"])
        self.assertTrue(current["warnings"])
        first = reflect.sources(self.args(session=None, past=True, limit=1), root)
        second = reflect.sources(
            self.args(session=None, past=True, limit=1, offset=first["next_offset"]),
            root,
        )
        self.assertEqual(
            {c["session_id"] for c in first["captures"] + second["captures"]},
            {"s1", "archived-task"},
        )
        old = reflect.sources(
            self.args(
                session=None, past=True, since="2000-01-01Z", until="2001-01-01Z"
            ),
            root,
        )
        self.assertEqual(old["captures"], [])
        self.assertEqual(
            reflect.sources(self.args(), self.home / "empty")["captures"], []
        )

    def test_worktrees_share_main_checkout_inbox_and_preview_writes_nothing(self):
        repo = self.home / "main project"
        tree = self.home / "worktree"

        def git(*args):
            return subprocess.run(
                ["git", *map(str, args)], check=True, capture_output=True
            )

        git("init", repo)
        git(
            "-C",
            repo,
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "--allow-empty",
            "-m",
            "test",
        )
        git("-C", repo, "worktree", "add", "-b", "test", tree)
        (tree / "nested").mkdir()
        root = repo / ".codex/reflect"
        self.assertEqual(capture.state_root(str(tree / "nested")), root.resolve())
        self.assertEqual(capture.state_root(str(repo)), root.resolve())
        before = sorted(str(p) for p in repo.rglob("*"))
        result = subprocess.run(
            [
                sys.executable,
                "-B",
                str(Path(reflect.__file__)),
                "sources",
                "--past",
                "--cwd",
                str(tree),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertEqual(json.loads(result.stdout)["captures"], [])
        self.assertEqual(before, sorted(str(p) for p in repo.rglob("*")))
        for cwd, turn in ((repo, "main"), (tree, "worktree")):
            with patch(
                "sys.stdin",
                io.StringIO(json.dumps(dict(self.event, cwd=str(cwd), turn_id=turn))),
            ):
                capture.main()
        self.assertEqual(len(list((root / "queue").glob("*.json"))), 2)
        self.assertFalse((tree / ".codex/reflect").exists())
        script = tree / ".agents/skills/reflect/scripts/capture.py"
        script.parent.mkdir(parents=True)
        script.write_text(Path(capture.__file__).read_text())
        config = Path(__file__).resolve().parents[4] / ".codex/hooks.json"
        hook = json.loads(config.read_text())["hooks"]["UserPromptSubmit"][-1]["hooks"][
            0
        ]
        tools = self.home / "bin"
        tools.mkdir()
        (tools / "python3").symlink_to(sys.executable)
        (tools / "git").symlink_to(shutil.which("git"))
        for prompt in ("please remember: use X", "ordinary request"):
            result = subprocess.run(
                hook["command"],
                shell=True,
                cwd=tree,
                env=dict(os.environ, PATH=str(tools)),
                text=True,
                input=json.dumps(
                    dict(self.event, cwd=str(tree), turn_id="hook", prompt=prompt)
                ),
                capture_output=True,
                check=True,
            )
            self.assertEqual(result.stdout + result.stderr, "")
        self.assertEqual(len(list((root / "queue").glob("*.json"))), 3)
        (script.parent / "reflect.py").write_text(Path(reflect.__file__).read_text())
        start = json.loads(config.read_text())["hooks"]["SessionStart"][-1]["hooks"][0]
        result = subprocess.run(
            start["command"],
            shell=True,
            cwd=tree,
            env=dict(os.environ, PATH=str(tools)),
            text=True,
            input=json.dumps(dict(self.event, cwd=str(tree), source="startup")),
            capture_output=True,
            check=True,
        )
        self.assertIn("3 unreviewed", result.stdout)
        self.assertEqual(result.stderr, "")

    def test_review_conflicts_statuses_and_stale_update(self):
        root = self.home / "reflect"
        review = reflect.Review(
            "status-history",
            "pending",
            [capture.record_id("s1", "t1")],
            "Use event history",
            "/tmp/AGENTS.md",
            "Verified schema",
            "Two conflicting sources",
        )
        saved = reflect.save_review(root, reflect.Review.parse(reflect.asdict(review)))
        result = reflect.reviews(
            root,
            reflect.argparse.Namespace(conflicts=True, offset=0, limit=25),
        )
        self.assertEqual(len(result["reviews"]), 1)
        with self.assertRaises(ValueError):
            reflect.save_review(root, review)
        with self.assertRaises(ValueError):
            reflect.Review.parse(dict(saved, status="adopted"))
        for status in ("dismissed", "proposed", "adopted"):
            saved = reflect.save_review(
                root, reflect.Review.parse(dict(saved, status=status, conflict=None))
            )
        self.assertEqual(len(list((root / "reviews").glob("*.json"))), 1)

    def test_detection_saves_only_confident_lessons_with_redacted_text(self):
        root = self.home / "reflect"
        event = dict(
            self.event,
            prompt="Use CAST(:x AS type) with asyncpg\nexport DB_PASSWORD=hunter2",
        )
        os.environ["REFLECT_OPENROUTER_API_KEY"] = "test-key"
        with self.jev(capture.THRESHOLD - 0.01):
            capture.capture(event, root)
        self.assertFalse((root / "queue").exists())
        with self.jev(0.93) as call:
            capture.capture(event, root)
        request = call.call_args[0][0]
        self.assertEqual(request.full_url, "https://openrouter.ai/api/v1/systemone")
        self.assertEqual(request.get_header("Authorization"), "Bearer test-key")
        body = json.loads(request.data)
        self.assertEqual(body["model"], "~typesafe/jev-latest")
        self.assertEqual(body["questions"]["lesson"]["type"], "noul")
        self.assertIn("CAST(:x AS type)", body["state"])
        self.assertNotIn("hunter2", body["state"])
        [item] = self.queue(root)
        self.assertEqual((item["kind"], item["score"]), ("detected", 0.93))
        self.assertEqual(item["text"], body["state"])

    def test_api_key_prefers_environment_then_project_env_file(self):
        env = self.home / ".env"
        with patch.object(capture, "project_root", return_value=self.home):
            self.assertIsNone(capture.api_key(str(self.home)))
            env.write_text('OTHER=1\nexport REFLECT_OPENROUTER_API_KEY="from-file"\n')
            self.assertEqual(capture.api_key(str(self.home)), "from-file")
            os.environ["REFLECT_OPENROUTER_API_KEY"] = "from-env"
            self.assertEqual(capture.api_key(str(self.home)), "from-env")
        del os.environ["REFLECT_OPENROUTER_API_KEY"]
        self.assertIsNone(capture.api_key(str(self.home / "not-a-repo")))

    def test_marker_and_missing_key_never_call_jev(self):
        root = self.home / "reflect"
        with patch.object(capture.urllib.request, "urlopen") as call:
            capture.capture(self.event, root)
            capture.capture(dict(self.event, turn_id="t2", prompt="ordinary"), root)
            os.environ["REFLECT_OPENROUTER_API_KEY"] = "test-key"
            capture.capture(dict(self.event, turn_id="t3"), root)
        call.assert_not_called()
        self.assertEqual(
            [(i["kind"], i["score"]) for i in self.queue(root)],
            [("explicit", None)] * 2,
        )

    def test_redact_removes_common_secrets_and_keeps_prose(self):
        jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.c2lnbmF0dXJlLXZhbHVl"
        pem = (
            "key:\n-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEA\nxyz\n"
            "-----END RSA PRIVATE KEY-----"
        )
        for text, secret in [
            ("OPENAI_API_KEY=abc123secret", "abc123secret"),
            ("  export db_password: hunter2", "hunter2"),
            ("curl -H 'Authorization: Bearer abc.def-ghi'", "abc.def-ghi"),
            ("use sk-proj-" + "A" * 24 + " here", "A" * 24),
            ("token ghp_" + "a" * 36, "a" * 36),
            ("xoxb-1234567890-abcdefghij", "1234567890-abcdefghij"),
            ("jwt " + jwt, jwt),
            ("postgresql://postgres:s3cret@localhost:5432/db", "s3cret"),
            ('{"api_key":"supersecret"}', "supersecret"),
            ("config = {'password': 'hunter 2', 'x': 1}", "hunter 2"),
            ("client.login(user=me, token=abc123xyz)", "abc123xyz"),
            ("curl -u admin:password https://x", "password"),
            ("curl --user admin:pa55 https://x", "pa55"),
            (pem, "MIIEowIBAAKCAQEA"),
            (pem.rsplit("\n", 1)[0], "MIIEowIBAAKCAQEA"),
            ("aws key AKIAIOSFODNN7EXAMPLE", "AKIAIOSFODNN7EXAMPLE"),
        ]:
            with self.subTest(text=text):
                self.assertNotIn(secret, capture.redact(text))
        self.assertIn("localhost:5432", capture.redact("x://u:p@localhost:5432"))
        self.assertIn(
            '"region": "us"', capture.redact('{"api_key": "s", "region": "us"}')
        )
        prose = "Primary key is station_id; always use CAST(:x AS type)."
        self.assertEqual(capture.redact(prose), prose)

    def test_jev_failures_are_quiet(self):
        root = self.home / ".codex/reflect"
        os.environ["REFLECT_OPENROUTER_API_KEY"] = "test-key"
        for failure in (
            dict(side_effect=urllib.error.URLError("offline")),
            dict(
                side_effect=urllib.error.HTTPError(
                    capture.JEV_URL, 529, "Overloaded", {}, None
                )
            ),
            dict(return_value=io.BytesIO(b"{}")),
        ):
            out = io.StringIO()
            with patch(
                "sys.stdin",
                io.StringIO(json.dumps(dict(self.event, prompt="ordinary"))),
            ), patch.object(capture, "state_root", return_value=root), patch.object(
                capture.urllib.request, "urlopen", **failure
            ), redirect_stdout(
                out
            ), redirect_stderr(
                out
            ):
                capture.main()
            self.assertEqual(out.getvalue(), "")
        self.assertFalse((root / "queue").exists())
        self.assertTrue((root / "capture-error.json").exists())

    def test_captures_saved_before_detection_still_load(self):
        root = self.home / "reflect"
        old = dict(
            id="a" * 64,
            session_id="s1",
            turn_id="t1",
            captured_at="2026-10-01T00:00:00+00:00",
            cwd=str(self.home),
            text="use X",
        )
        (root / "queue").mkdir(parents=True)
        (root / "queue" / (old["id"] + ".json")).write_text(json.dumps(old))
        [item] = reflect.sources(self.args(), root)["captures"]
        self.assertEqual((item["kind"], item["score"]), ("explicit", None))

    def test_nudge_counts_unreviewed_captures_and_report(self):
        root = self.home / "reflect"
        self.assertIsNone(reflect.nudge(root))
        capture.capture(self.event, root)
        capture.capture(dict(self.event, turn_id="t2"), root)
        reflect.save_review(
            root,
            reflect.Review(
                "noise", "dismissed", [capture.record_id("s1", "t1")], "x", "none", "y"
            ),
        )
        message = reflect.nudge(root)
        self.assertIn("1 unreviewed", message)
        self.assertNotIn("report", message)
        (root / "report.md").write_text("summary")
        self.assertIn(str(root / "report.md"), reflect.nudge(root))
        for source, expected in (("resume", ""), ("startup", "1 unreviewed")):
            out = io.StringIO()
            with patch(
                "sys.stdin", io.StringIO(json.dumps(dict(self.event, source=source)))
            ), patch.object(reflect, "state_root", return_value=root), redirect_stdout(
                out
            ):
                reflect.hook_nudge()
            self.assertIn(expected, out.getvalue())
            if not expected:
                self.assertEqual(out.getvalue(), "")
        with patch("sys.stdin", io.StringIO("invalid")), redirect_stdout(out):
            reflect.hook_nudge()


if __name__ == "__main__":
    unittest.main()
