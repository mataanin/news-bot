"""Claude calls for tagging and the digest.

Uses the API when ANTHROPIC_API_KEY is set, otherwise the logged-in Claude Code CLI, so summaries work
without a key. The prompts hold other people's posts, so the CLI runs with no tools, MCP servers, settings,
hooks or saved session, from an empty folder: text in a post has nothing to act through.
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import tempfile
from pathlib import Path

CLI_TIMEOUT = 900  # seconds per call
CLI_TRIES = 3


def _cli_path() -> str | None:
    fallback = Path.home() / ".local/bin/claude"  # launchd's PATH doesn't include it
    return shutil.which("claude") or (str(fallback) if os.access(fallback, os.X_OK) else None)


def backend() -> str:
    if os.environ.get("ANTHROPIC_API_KEY"):
        return "api"
    if _cli_path():
        return "claude cli"
    raise SystemExit("No way to call Claude: set ANTHROPIC_API_KEY (or put it in .env), or install and log in to Claude Code.")


def cli_args(exe: str, model: str, system: str, schema: dict | None) -> list[str]:
    args = [exe, "-p", "--model", model, "--tools", "", "--strict-mcp-config", "--setting-sources", "",
            "--no-session-persistence", "--disable-slash-commands", "--output-format", "json", "--system-prompt", system]
    return args + ["--json-schema", json.dumps(schema)] if schema else args


async def _cli_once(args: list[str], prompt: str) -> dict:
    env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}  # it can only be empty here
    with tempfile.TemporaryDirectory() as empty:
        proc = await asyncio.create_subprocess_exec(*args, cwd=empty, env=env, stdin=asyncio.subprocess.PIPE,
                                                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            out, err = await asyncio.wait_for(proc.communicate(prompt.encode()), CLI_TIMEOUT)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            raise RuntimeError(f"claude timed out after {CLI_TIMEOUT}s")
    try:
        result = json.loads(out)
    except ValueError:
        raise RuntimeError(f"claude exited {proc.returncode}: {(err or out).decode(errors='replace')[:300]}")
    if result.get("is_error") or proc.returncode:
        raise RuntimeError(f"claude failed: {str(result.get('result') or result.get('subtype'))[:300]}")
    return result


async def _cli(model: str, system: str, prompt: str, schema: dict | None) -> dict:
    args = cli_args(_cli_path(), model, system, schema)
    for attempt in range(CLI_TRIES):
        try:
            result = await _cli_once(args, prompt)
            if schema and not isinstance(result.get("structured_output"), dict):
                raise RuntimeError("claude returned no structured output")
            return result
        except RuntimeError:
            if attempt == CLI_TRIES - 1:
                raise
            await asyncio.sleep(5 * 2 ** attempt)
    raise AssertionError("unreachable")


async def structured(model: str, system: str, prompt: str, tool: dict) -> dict:
    """The arguments Claude gives `tool`, always valid against its schema."""
    if backend() == "claude cli":
        return (await _cli(model, system, prompt, tool["input_schema"]))["structured_output"]
    import anthropic
    msg = await anthropic.AsyncAnthropic(max_retries=6).messages.create(
        model=model, max_tokens=8000,
        system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
        tools=[tool], tool_choice={"type": "tool", "name": tool["name"]},
        messages=[{"role": "user", "content": prompt}])
    return next((b.input for b in msg.content if b.type == "tool_use"), {})


async def text(model: str, system: str, prompt: str) -> str:
    if backend() == "claude cli":
        return (await _cli(model, system, prompt, None))["result"]
    import anthropic
    msg = await anthropic.AsyncAnthropic(max_retries=6).messages.create(
        model=model, max_tokens=12000, system=system, messages=[{"role": "user", "content": prompt}])
    return "".join(b.text for b in msg.content if b.type == "text")
