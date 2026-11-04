#!/usr/bin/env python3
"""Fast, read-only crawl of a list of X profiles for the past N days.

Why it is fast compared with an agent driving a browser:
  * it reads the JSON X's own web app downloads (GraphQL UserOriginalsTimeline) instead of
    the screen: full text, exact timestamps and like/repost/view counts;
  * no screenshots and no model in the loop, and images/video/fonts are blocked;
  * several profiles are crawled in parallel in one logged-in browser context;
  * it stops per profile as soon as it scrolls past the start of the window.

Usage:
  python -m xweek.crawl login                      # one-time: opens Chrome on the crawler's own profile;
                                                   #   you log in to X there, then quit that Chrome
  python -m xweek.crawl check                      # verify the login on one profile
  python -m xweek.crawl run --channel chrome       # crawl the past 7 days (handles.txt)
  python -m xweek.crawl run --since 2026-10-14 --concurrency 4 --headed

Only reads pages. It never likes, posts, follows or changes anything.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from playwright.async_api import async_playwright, BrowserContext, Page
from playwright.async_api import TimeoutError as PlaywrightTimeout

from .xparse import (DOM_JS, X, is_timeline_url, keep_for_profile, normalize_dom_record, payloads_from_html,
                     pinned_ids, records_from_payload, timeline_floor)

BLOCK_TYPES = {"image", "media", "font"}
# The crawler's own Chrome profile: kept outside the project folder, so the login stays out of the repo.
DEFAULT_PROFILE = os.path.expanduser("~/Library/Application Support/xweek-chrome")
# Posts or an empty state, in the classic app (data-testid) or the newer one (div[data-href] > article).
READY = ('article[data-testid="tweet"], [data-testid="emptyState"], [data-testid="error-detail"], '
         '[data-href*="/status/"] article')
LOGIN_PROMPTS = ('[data-testid="loginButton"], [data-testid="signupButton"], a[href="/login"], '
                 'a[href^="/i/flow/login"], a[href^="/i/jf/onboarding/"], button:has-text("Continue with")')
LOGGED_IN = 'a[href="/home"]'  # the side navigation; X shows logged-out visitors a preview without it
MAX_TIMEOUTS_IN_A_ROW = 5
AT_BOTTOM_JS = "innerHeight + scrollY >= document.documentElement.scrollHeight - 300"


def load_handles(path: Path) -> list[str]:
    out, seen = [], set()
    for line in path.read_text().splitlines():
        h = line.split("#", 1)[0].strip().lstrip("@")
        if h and h.lower() not in seen:
            seen.add(h.lower())
            out.append(h)
    return out


def utc_day(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc)


async def make_context(pw, state_file: Path | None, headed: bool, channel: str | None,
                       profile_dir: Path | None = None):
    """A logged-in browser context.

    With profile_dir: reuse a dedicated Chrome profile you logged in to by hand (see `login`).
    The script never reads or copies your login; it just opens that profile.
    Otherwise: a saved Playwright storage state (state_file).
    Returns (closer, context); call closer.close() when done.
    """
    probe_browser = await pw.chromium.launch(headless=not headed, channel=channel or None)
    # Headless Chrome announces itself in its user agent; present the normal one.
    probe = await probe_browser.new_page()
    ua = (await probe.evaluate("navigator.userAgent")).replace("HeadlessChrome", "Chrome")
    await probe_browser.close()
    opts = dict(user_agent=ua, locale="en-US", viewport={"width": 1280, "height": 2200})
    if profile_dir:
        # Keep Playwright's default --use-mock-keychain: headless Chrome can only read cookies encrypted with
        # the mock key, so `login` opens its window with the same flag. Mixing the two loses the login.
        ctx = await pw.chromium.launch_persistent_context(str(profile_dir), headless=not headed,
                                                         channel=channel or None, **opts)
        browser = ctx
    else:
        browser = await pw.chromium.launch(headless=not headed, channel=channel or None)
        ctx = await browser.new_context(storage_state=str(state_file), **opts)

    async def block(route):
        # Video also arrives as xhr, in segments, and decoding it can stall a tab for many seconds.
        if route.request.resource_type in BLOCK_TYPES or "video.twimg.com" in route.request.url:
            await route.abort()
        else:
            await route.continue_()

    await ctx.route("**/*", block)
    return browser, ctx


async def session_state(page: Page) -> str:
    """"logged_in", "logged_out", or "unknown" when the page shows neither.

    X's logged-out preview has only about 5 posts and no pagination. "logged_out" needs real evidence,
    because three of those end the run: a tab that is just slow also has no /home link yet.
    """
    if re.search(r"/(i/flow/)?login", page.url):
        return "logged_out"
    home = page.locator(LOGGED_IN)
    if await page.locator(LOGIN_PROMPTS).count():
        return "logged_in" if await home.count() else "logged_out"
    try:
        await home.first.wait_for(state="attached", timeout=10000)
        return "logged_in"
    except PlaywrightTimeout:
        return "unknown"


async def error_page_shown(page: Page) -> bool:
    """Whether X is showing "Something went wrong. Try reloading." instead of the timeline.

    Pause at once when it appears: it is what X shows when it starts refusing, and a run that kept going
    without pausing on it ended up with blank pages. The side navigation must be there, because the
    document's fallback block has the same words while the page starts, and no posts, because a post can
    have them too.
    """
    text = page.get_by_text("Something went wrong", exact=False).filter(visible=True)
    return bool(await page.locator(LOGGED_IN).count() and await text.count()
                and not await page.locator(READY).count())


async def crawl_profile(ctx: BrowserContext, handle: str, since: datetime, max_scrolls: int,
                        idle_rounds: int, ready_deadline: float = 20) -> tuple[list[dict], dict]:
    t0 = time.time()
    page = await ctx.new_page()
    got: dict[str, dict] = {}
    pins: set[str] = set()
    tasks: set[asyncio.Task] = set()
    floor: datetime | None = None  # how far back the timeline has been read
    ended = False  # X returned a timeline page with no posts: there is nothing older
    stat = {"handle": handle, "status": "ok", "scrolls": 0, "truncated": False, "rate_limited": False}

    def ingest(payload) -> None:
        nonlocal floor
        pins.update(pinned_ids(payload))
        for r in records_from_payload(payload):
            got[r["id"]] = r
        reached = timeline_floor(payload, handle, pins)
        if reached and (floor is None or reached < floor):
            floor = reached

    async def on_response(resp):
        nonlocal ended
        document = (resp.request.resource_type == "document"
                    and resp.url.split("?")[0].rstrip("/").lower().endswith(f"/{handle.lower()}"))
        if not document and not is_timeline_url(resp.url):
            return
        if document:
            stat["document_status"] = resp.status
        if resp.status == 429:
            stat["rate_limited"] = True
            stat["limit_signal"] = "http 429"
            return
        try:
            payloads = payloads_from_html(await resp.text()) if document else [await resp.json()]
        except Exception:
            return
        for payload in payloads:
            ingest(payload)
        if not document:
            ended = not records_from_payload(payloads[0])

    def handler(resp):
        t = asyncio.ensure_future(on_response(resp))
        tasks.add(t)
        t.add_done_callback(tasks.discard)

    page.on("response", handler)
    try:
        await page.goto(f"{X}/{handle}", wait_until="domcontentloaded", timeout=45000)
        deadline = time.time() + ready_deadline
        while True:  # wait for posts, an empty state, or a rate-limit signal, whichever comes first
            if stat["rate_limited"] or await page.locator(f"{READY}, {LOGIN_PROMPTS}").count():
                break
            if await error_page_shown(page):
                stat["rate_limited"] = True  # X's generic error page; usually rate limiting
                stat["limit_signal"] = "error page"
                break
            if time.time() > deadline:
                stat["status"] = "timeout"
                break
            await asyncio.sleep(0.25)
        session = await session_state(page)
        if session == "logged_out":
            stat["status"] = "logged_out"
        elif session == "unknown" and not stat["rate_limited"]:
            stat["status"] = "timeout"
        empty = await page.locator('[data-testid="emptyState"]').all_inner_texts()
        if empty and stat["status"] != "logged_out":
            txt = " ".join(empty).lower()
            stat["status"] = ("not_found" if "exist" in txt else "suspended" if "suspend" in txt
                              else "protected" if "protect" in txt else "empty")
        await page.mouse.move(640, 900)

        last_n, idle = -1, 0
        while stat["status"] in ("ok", "timeout") and stat["scrolls"] < max_scrolls:
            await asyncio.sleep(0.35)
            if floor and floor < since:
                break
            if len(got) != last_n:
                idle, last_n = 0, len(got)
            elif await page.evaluate(AT_BOTTOM_JS):  # tall posts can take many scrolls to reach the end of a page
                idle += 1
                if idle >= idle_rounds:
                    break
                await page.mouse.wheel(0, -1500)  # X loads more when the end of the list comes back into view
            if stat["rate_limited"]:
                break
            await page.mouse.wheel(0, 4000)
            stat["scrolls"] += 1
            await page.wait_for_timeout(random.randint(500, 900))
        else:
            if stat["scrolls"] >= max_scrolls:
                stat["truncated"] = True

        if tasks:
            await asyncio.gather(*list(tasks), return_exceptions=True)
        if not got and stat["status"] in ("ok", "timeout"):
            # Fallback: X renamed or restructured the timeline call. Read the DOM once.
            for r in await page.evaluate(DOM_JS):
                if not r.get("pinned"):
                    rec = normalize_dom_record(r)
                    got[rec["id"]] = rec
            stat["source"] = "dom"
        if stat["status"] == "timeout" and got and await page.locator(LOGGED_IN).count():
            stat["status"] = "ok"  # slow to render, but logged in and the timeline came through
        if (stat["status"] == "ok" and floor and floor >= since and not ended
                and not stat["truncated"] and not stat["rate_limited"]):
            stat["status"] = "incomplete"  # X stopped sending pages before the start of the window
    except Exception as e:  # keep the run going; report per profile
        stat["status"] = f"error: {type(e).__name__}: {e}"[:200]
    finally:
        if stat["status"] != "ok" or not got:  # record what X showed, to diagnose logins and blocks
            try:
                stat["url"] = page.url
                stat["page_text"] = re.sub(r"\s+", " ", await page.inner_text("body", timeout=3000))[:300]
            except Exception:
                pass
        await page.close()

    if stat["status"] == "logged_out":
        got.clear()  # a preview of a few posts must not pass for a crawled account
    kept = [r for r in got.values() if keep_for_profile(r, handle, since)]
    kept.sort(key=lambda r: r["created_at"], reverse=True)
    stat.update(captured=len(got), kept=len(kept), seconds=round(time.time() - t0, 1))
    return kept, stat


async def run(args) -> None:
    handles = load_handles(Path(args.handles))
    since = utc_day(args.since) if args.since else (datetime.now(timezone.utc) - timedelta(days=args.days)).replace(
        hour=0, minute=0, second=0, microsecond=0)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    tag = since.strftime("%Y-%m-%d")
    posts_path, stats_path = out_dir / f"posts-{tag}.jsonl", out_dir / f"crawl-stats-{tag}.json"

    stats: dict[str, dict] = json.loads(stats_path.read_text()) if stats_path.exists() else {}
    todo = [h for h in handles if h.lower() not in {k.lower() for k, v in stats.items() if v.get("status") == "ok"}]
    print(f"window: since {since.isoformat()} | {len(handles)} handles, {len(todo)} to crawl | "
          f"concurrency {args.concurrency}", file=sys.stderr)

    write_lock = asyncio.Lock()
    sem = asyncio.Semaphore(args.concurrency)
    pause_until = 0.0
    logged_out = 0
    timeouts_in_a_row = 0
    profile = Path(args.profile_dir).expanduser() if args.profile_dir else None
    if profile and not profile.is_dir():
        profile = None
    if not profile and not Path(args.state).exists():
        sys.exit(f"No X login found. Run: python -m xweek.crawl login   (expected {args.profile_dir})")

    async with async_playwright() as pw:
        try:
            browser, ctx = await make_context(pw, Path(args.state), args.headed, args.channel, profile)
        except Exception as e:
            msg = str(e).splitlines()[0][:300]
            hint = (" The crawler's Chrome profile is still open: quit that Chrome window (Cmd+Q) and retry."
                    if profile and re.search(r"in use|ProcessSingleton|SingletonLock|already running", str(e), re.I) else "")
            sys.exit(f"Could not start the browser: {msg}.{hint}")

        async def worker(h: str) -> None:
            nonlocal pause_until, logged_out, timeouts_in_a_row
            async with sem:
                if logged_out >= 3 or timeouts_in_a_row >= MAX_TIMEOUTS_IN_A_ROW:
                    return  # don't keep hitting X with a dead session, or when it has stopped serving pages
                for attempt in range(2):
                    wait = pause_until - time.time()
                    if wait > 0:
                        await asyncio.sleep(wait)
                    await asyncio.sleep(random.uniform(0.3, 1.5))
                    recs, st = await crawl_profile(ctx, h, since, args.max_scrolls, args.idle_rounds)
                    if st["rate_limited"] and attempt == 0:
                        pause_until = max(pause_until, time.time() + args.backoff)
                        print(f"  rate-limited on @{h} ({st.get('limit_signal')}); pausing {args.backoff}s", file=sys.stderr)
                        continue
                    if st["status"] in ("timeout", "incomplete") and attempt == 0:
                        print(f"  @{h} {st['status']}; retrying", file=sys.stderr)
                        continue
                    break
                if st["rate_limited"] and st["status"] == "ok":
                    st["status"] = "rate_limited"  # still limited after the retry: its posts may be incomplete
                if st["status"] == "logged_out":
                    logged_out += 1
                timeouts_in_a_row = timeouts_in_a_row + 1 if st["status"] == "timeout" else 0
                async with write_lock:
                    with posts_path.open("a") as f:
                        for r in recs:
                            f.write(json.dumps(r, ensure_ascii=False) + "\n")
                    stats[h] = st
                    stats_path.write_text(json.dumps(stats, indent=1))
                flag = " (truncated)" if st["truncated"] else ""
                print(f"  @{h}: {st['kept']} posts, {st['scrolls']} scrolls, {st['seconds']}s, "
                      f"{st['status']}{flag}", file=sys.stderr)

        t0 = time.time()
        await asyncio.gather(*(worker(h) for h in todo))
        await browser.close()

    # De-duplicate (a resumed run may append the same post twice).
    seen, rows = set(), []
    for line in posts_path.read_text().splitlines() if posts_path.exists() else []:
        r = json.loads(line)
        if r["id"] not in seen:
            seen.add(r["id"])
            rows.append(r)
    posts_path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows))
    bad = {h: s["status"] for h, s in stats.items() if s["status"] != "ok"}
    print(f"done in {time.time() - t0:.0f}s: {len(rows)} posts -> {posts_path}", file=sys.stderr)
    if bad:
        print(f"  not ok: {bad}", file=sys.stderr)
    if logged_out >= 3:
        sys.exit("X shows a logged-out page: the crawler's login expired. Run: python -m xweek.crawl login")
    if timeouts_in_a_row >= MAX_TIMEOUTS_IN_A_ROW:
        sys.exit(f"Stopped: {timeouts_in_a_row} profiles in a row didn't load, so X may be refusing the crawler. "
                 f"The next run resumes; check {stats_path} first (document_status, page_text).")


def login_chrome(args) -> None:
    """Open your normal Google Chrome (not automated) on the crawler's own profile so you can log in
    to X by hand. Nothing here reads, copies or stores your login; it stays inside that profile."""
    prof = Path(args.profile_dir).expanduser()
    prof.mkdir(parents=True, exist_ok=True)
    if sys.platform != "darwin":
        print(f'Start Chrome yourself with:  google-chrome --user-data-dir="{prof}" {X}/login', file=sys.stderr)
        return
    # --use-mock-keychain matches the headless crawl, which can't read cookies encrypted with the real keychain.
    # Only ever open this profile with that flag: Chrome drops the cookies it can't decrypt.
    subprocess.run(["open", "-na", "Google Chrome", "--args", f"--user-data-dir={prof}", "--no-first-run",
                    "--no-default-browser-check", "--use-mock-keychain", f"{X}/login"], check=True)
    print("A separate Chrome window opened with the crawler's own profile.\n"
          "1. Log in to X there.\n"
          "2. Then quit THAT Chrome instance (focus its window, press Cmd+Q). Your normal Chrome is not affected.",
          file=sys.stderr)


async def check(args) -> None:
    """Open one profile with the crawler's login and report whether X shows a logged-in timeline."""
    profile = Path(args.profile_dir).expanduser() if args.profile_dir else None
    async with async_playwright() as pw:
        browser, ctx = await make_context(pw, Path(args.state), False, args.channel,
                                          profile if profile and profile.is_dir() else None)
        since = datetime.now(timezone.utc) - timedelta(days=7)
        recs, st = await crawl_profile(ctx, args.handle, since, max_scrolls=0, idle_rounds=1)
        await browser.close()
    ok = st["status"] == "ok" and st["captured"] > 0
    print(f"{'OK' if ok else 'NOT OK'}: @{args.handle} status={st['status']} posts_seen={st['captured']}", file=sys.stderr)
    if not ok:
        print(f"  page: {st.get('url', '?')}\n  text: {st.get('page_text', '')}", file=sys.stderr)
    sys.exit(0 if ok else 1)


async def login_playwright(args) -> None:
    """Older way: log in inside an automated browser. X may refuse it as an 'insecure browser'."""
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=False, channel=args.channel or None)
        ctx = await browser.new_context()
        page = await ctx.new_page()
        await page.goto(f"{X}/login")
        print("Log in to X in the window that opened. Waiting for your home timeline...", file=sys.stderr)
        await page.wait_for_url(re.compile(r"https://(x|twitter)\.com/home"), timeout=0)
        await ctx.storage_state(path=args.state)
        os.chmod(args.state, 0o600)
        print(f"Saved session to {args.state} (keep it private; it is your X login).", file=sys.stderr)
        await browser.close()


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    lg = sub.add_parser("login", help="open Chrome on the crawler's own profile so you can log in to X by hand")
    lg.add_argument("--profile-dir", default=DEFAULT_PROFILE)
    lg.add_argument("--playwright", action="store_true", help="older way: log in inside an automated browser")
    lg.add_argument("--state", default="x_state.json")
    lg.add_argument("--channel", default=None, help="e.g. 'chrome' to use installed Google Chrome")
    ck = sub.add_parser("check", help="verify the crawler's X login on one profile")
    ck.add_argument("--handle", default="ClaudeDevs")
    ck.add_argument("--profile-dir", default=DEFAULT_PROFILE)
    ck.add_argument("--state", default="x_state.json")
    ck.add_argument("--channel", default="chrome")
    r = sub.add_parser("run", help="crawl the profiles")
    r.add_argument("--handles", default="handles.txt")
    r.add_argument("--profile-dir", default=DEFAULT_PROFILE, help="Chrome profile you logged in to (preferred)")
    r.add_argument("--state", default="x_state.json", help="fallback: Playwright storage state")
    r.add_argument("--since", help="YYYY-MM-DD (UTC); default: --days ago")
    r.add_argument("--days", type=int, default=7)
    r.add_argument("--out-dir", default="data")
    r.add_argument("--concurrency", type=int, default=3)
    r.add_argument("--max-scrolls", type=int, default=40, help="per profile; prolific accounts need more")
    r.add_argument("--idle-rounds", type=int, default=4, help="stop after this many scrolls with nothing new")
    r.add_argument("--backoff", type=int, default=120, help="seconds to pause all workers after a 429")
    r.add_argument("--headed", action="store_true")
    r.add_argument("--channel", default=None)
    args = p.parse_args()
    if args.cmd == "login":
        asyncio.run(login_playwright(args)) if args.playwright else login_chrome(args)
    elif args.cmd == "check":
        asyncio.run(check(args))
    else:
        asyncio.run(run(args))


if __name__ == "__main__":
    main()
