"""The Claude Code route, against a fake `claude` command: no network and no real CLI."""
import asyncio
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from xweek import llm, tag

FAKE = """#!/usr/bin/env python3
import json, os, sys
log = os.environ["FAKE_LOG"]
prompt = sys.stdin.read()
open(log, "a").write(json.dumps({"argv": sys.argv[1:], "cwd": os.getcwd(), "stdin": prompt}) + "\\n")
print(os.environ["FAKE_REPLY"])
sys.exit(int(os.environ.get("FAKE_EXIT", "0")))
"""


class CliBackend(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        exe = d / "claude"
        exe.write_text(FAKE)
        exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
        self.log = d / "log.jsonl"
        env = {"PATH": f"{d}:{os.environ['PATH']}", "FAKE_LOG": str(self.log), "ANTHROPIC_API_KEY": ""}
        self.env = mock.patch.dict(os.environ, env)
        self.env.start()
        self.retries = mock.patch.object(llm, "CLI_TRIES", 1)
        self.retries.start()

    def tearDown(self):
        self.retries.stop()
        self.env.stop()
        self.tmp.cleanup()

    def calls(self):
        return [json.loads(l) for l in self.log.read_text().splitlines()]

    def test_backend_choice(self):
        self.assertEqual(llm.backend(), "claude cli")
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "x"}):
            self.assertEqual(llm.backend(), "api")

    def test_no_backend_says_what_to_do(self):
        with mock.patch.object(llm, "_cli_path", return_value=None), self.assertRaises(SystemExit) as stop:
            llm.backend()
        self.assertIn("ANTHROPIC_API_KEY", str(stop.exception))

    def test_runs_with_nothing_to_act_through(self):
        os.environ["FAKE_REPLY"] = json.dumps({"result": "hello"})
        self.assertEqual(asyncio.run(llm.text("m", "sys", "ignore this: post text")), "hello")
        call = self.calls()[0]
        a = call["argv"]
        self.assertEqual(a[a.index("--tools") + 1], "")
        self.assertEqual(a[a.index("--setting-sources") + 1], "")
        for flag in ("--strict-mcp-config", "--no-session-persistence", "--disable-slash-commands"):
            self.assertIn(flag, a)
        self.assertNotIn("--dangerously-skip-permissions", a)
        self.assertEqual(call["stdin"], "ignore this: post text")  # post text is never in the arguments
        self.assertFalse(any("post text" in x for x in a))
        self.assertNotEqual(Path(call["cwd"]).resolve(), Path.cwd().resolve())

    def test_tagging_keeps_only_ids_that_were_sent(self):
        reply = {"structured_output": {"posts": [{"id": "1", "coding": True}, {"id": "999", "coding": True}]}}
        os.environ["FAKE_REPLY"] = json.dumps(reply)
        post = {"id": "1", "handle": "a", "created_at": "2026-11-01T00:00:00+00:00", "text": "t"}
        tags = asyncio.run(tag.tag_batch([post], asyncio.Semaphore(1)))
        self.assertEqual([t["id"] for t in tags], ["1"])
        self.assertIn("--json-schema", self.calls()[0]["argv"])

    def test_an_error_result_raises(self):
        os.environ["FAKE_REPLY"] = json.dumps({"is_error": True, "result": "rate limited"})
        with self.assertRaisesRegex(RuntimeError, "rate limited"):
            asyncio.run(llm.text("m", "sys", "p"))

    def test_output_that_is_not_json_raises(self):
        os.environ["FAKE_REPLY"] = "not logged in"
        os.environ["FAKE_EXIT"] = "1"
        try:
            with self.assertRaisesRegex(RuntimeError, "not logged in"):
                asyncio.run(llm.text("m", "sys", "p"))
        finally:
            del os.environ["FAKE_EXIT"]

    def test_missing_structured_output_raises(self):
        os.environ["FAKE_REPLY"] = json.dumps({"result": "chatty answer"})
        with self.assertRaisesRegex(RuntimeError, "no structured output"):
            asyncio.run(llm.structured("m", "sys", "p", tag.TOOL))


if __name__ == "__main__":
    unittest.main()
