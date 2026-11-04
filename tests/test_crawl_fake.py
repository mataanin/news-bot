"""End-to-end crawl against a fake x.com served through Playwright request routing.

No network: every request to https://x.com is answered locally. The fake profile
page loads timeline JSON with fetch() and loads the next page when you scroll
near the bottom, like the real site. Checks that crawl_profile scrolls with the
mouse wheel, reads the JSON, ignores the old pin and stops at the window start.
"""
import asyncio
import json
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

from playwright.async_api import async_playwright

from xweek.crawl import crawl_profile, make_context
from tests.fixtures import (NOW, new_article, new_payload, new_tweet, paged_timeline, payload, snowflake,
                            tanstack_html, tweet)

PAGE = """<!doctype html><html><body style="margin:0"><a href="/home">Home</a><div hidden>Something went wrong, but don't fret</div><div id="tl"></div><script>
let page = 0, loading = false, done = false;
async function load() {
  if (loading || done) return; loading = true;
  const r = await fetch('/i/api/graphql/q1/UserTweets?variables=' + page);
  const j = await r.json();
  for (const it of j.dom) {
    const a = document.createElement('article'); a.setAttribute('data-testid', 'tweet'); a.style.height = '700px';
    a.innerHTML = `<a href="/${it.h}/status/${it.id}"><time datetime="${it.d}">t</time></a><div data-testid="tweetText">${it.t}</div>`;
    document.getElementById('tl').appendChild(a);
  }
  if (!j.more) done = true; page++; loading = false;
}
window.addEventListener('scroll', () => { if (innerHeight + scrollY > document.body.scrollHeight - 1500) load(); });
load();
</script></body></html>"""


class FakeXCrawl(unittest.TestCase):
    def test_crawl_stops_at_window_start(self):
        pages = paged_timeline(pages=6, per_page=6, hours_apart=10)
        served = []

        async def go():
            async with async_playwright() as pw:
                with tempfile.TemporaryDirectory() as d:
                    state = Path(d) / "state.json"
                    state.write_text(json.dumps({"cookies": [], "origins": []}))
                    browser, ctx = await make_context(pw, state, headed=False, channel=None)

                    async def fake_x(route):
                        url = route.request.url
                        if "/i/api/graphql/" in url:
                            k = int(url.split("variables=")[1])
                            served.append(k)
                            await route.fulfill(status=200, content_type="application/json",
                                                body=json.dumps(pages[k]))
                        else:
                            await route.fulfill(status=200, content_type="text/html", body=PAGE)

                    await ctx.route("https://x.com/**", fake_x)  # registered last, so it runs first
                    since = NOW - timedelta(days=7)
                    recs, stat = await crawl_profile(ctx, "alice", since, max_scrolls=30, idle_rounds=4)
                    await browser.close()
                    return recs, stat

        recs, stat = asyncio.run(go())
        # posts every 10h from NOW; the 7-day window holds posts 0..16 (16*10h = 160h < 168h)
        self.assertEqual(len(recs), 17, stat)
        self.assertEqual(stat["status"], "ok")
        self.assertFalse(stat["truncated"])
        self.assertFalse(stat["rate_limited"])  # X's hidden fallback text isn't an error
        self.assertNotIn("1", {r["id"] for r in recs})  # the 300-day-old pin is ignored
        self.assertLess(max(served), 5, f"should stop once past the window, served pages {served}")
        print(f"\n  fake crawl: {stat}, pages served {served}")



LOGGED_OUT = """<!doctype html><html><body><a href="/login">Log in</a><div>Sign up to see posts</div></body></html>"""


class ProfileModeAndLogout(unittest.TestCase):
    def test_persistent_profile_and_logged_out(self):
        pages = paged_timeline(pages=3, per_page=6, hours_apart=10)

        async def go():
            async with async_playwright() as pw:
                with tempfile.TemporaryDirectory() as prof:
                    closer, ctx = await make_context(pw, None, headed=False, channel=None, profile_dir=Path(prof))

                    async def fake_x(route):
                        url = route.request.url
                        if "/i/api/graphql/" in url:
                            k = int(url.split("variables=")[1])
                            return await route.fulfill(status=200, content_type="application/json",
                                                       body=json.dumps(pages[min(k, 2)]))
                        body = LOGGED_OUT if url.rstrip("/").endswith("/bob") else PAGE
                        await route.fulfill(status=200, content_type="text/html", body=body)

                    await ctx.route("https://x.com/**", fake_x)
                    since = NOW - timedelta(days=7)
                    ok = await crawl_profile(ctx, "alice", since, max_scrolls=30, idle_rounds=4)
                    out = await crawl_profile(ctx, "bob", since, max_scrolls=30, idle_rounds=4)
                    await closer.close()
                    return ok, out

        (recs, st), (_, st_out) = asyncio.run(go())
        self.assertEqual(st["status"], "ok")
        self.assertGreater(len(recs), 10)
        self.assertEqual(st_out["status"], "logged_out")
        self.assertEqual(st_out["scrolls"], 0)


HOME = '<a href="/home">Home</a>'
LOGIN_BAR = ('<a href="/i/jf/onboarding/web?mode=login&redirect_after_login=%2Falice">Log in</a>'
             '<a href="/i/jf/onboarding/web?mode=signup&redirect_after_login=%2Falice">Sign up</a>'
             '<button>Continue with Apple</button>')


def fake_crawl(routes, handle="alice", max_scrolls=8, idle_rounds=2, ready_deadline=None):
    """Crawl one profile; routes maps a URL substring to (content type, body), checked in order."""

    async def go():
        async with async_playwright() as pw:
            with tempfile.TemporaryDirectory() as prof:
                closer, ctx = await make_context(pw, None, headed=False, channel=None, profile_dir=Path(prof))

                async def fake_x(route):
                    for part, (ctype, body) in routes.items():
                        if part in route.request.url:
                            return await route.fulfill(status=200, content_type=ctype, body=body)
                    await route.fulfill(status=404, body="")

                await ctx.route("https://x.com/**", fake_x)
                await ctx.route("https://api.x.com/**", fake_x)
                extra = {"ready_deadline": ready_deadline} if ready_deadline else {}
                out = await crawl_profile(ctx, handle, NOW - timedelta(days=7), max_scrolls, idle_rounds, **extra)
                await closer.close()
                return out

    return asyncio.run(go())


class NewAppCrawl(unittest.TestCase):
    posts = [new_tweet(snowflake(NOW - timedelta(hours=h + 1)), "alice", NOW - timedelta(hours=h + 1), f"post {h}")
             for h in range(5)]
    old = new_tweet(snowflake(NOW - timedelta(days=9)), "alice", NOW - timedelta(days=9), "before the window")

    def test_logged_out_preview_is_not_a_crawl(self):
        """The new logged-out page has real posts in it, but it must count as logged out, with no posts."""
        body = LOGIN_BAR + "".join(new_article(t["rest_id"], "alice", "x") for t in self.posts)
        recs, st = fake_crawl({"/alice": ("text/html", tanstack_html(new_payload(self.posts), body))})
        self.assertEqual(st["status"], "logged_out")
        self.assertEqual((recs, st["captured"], st["scrolls"]), ([], 0, 0))
        self.assertIn("Sign up", st["page_text"])

    def test_missing_home_link_alone_is_not_logged_out(self):
        """A slow tab has no /home link yet either, and three logged_out results end the run."""
        tid = snowflake(NOW - timedelta(hours=1))
        page = tanstack_html(new_payload([new_tweet(tid, "alice", NOW - timedelta(hours=1), "x")]), new_article(tid, "alice", "x"))
        _, st = fake_crawl({"/alice": ("text/html", page)}, max_scrolls=0)
        self.assertEqual(st["status"], "timeout")  # not ok, so a preview without login links can't pass either

    def test_slow_page_with_a_timeline_is_ok(self):
        """Nothing matches READY in time, but the JSON arrived and the page is logged in."""
        posts = json.dumps(new_payload(self.posts + [self.old]))
        page = f"""<html><body>{HOME}<script>fetch('/i/api/graphql/q/UserOriginalsTimeline?variables=1')</script>"""
        recs, st = fake_crawl({"UserOriginalsTimeline": ("application/json", posts), "/alice": ("text/html", page)},
                              max_scrolls=1, idle_rounds=1, ready_deadline=2)
        self.assertEqual((st["status"], len(recs)), ("ok", 5), st)

    def test_logged_in_server_rendered_first_page(self):
        body = HOME + "".join(new_article(t["rest_id"], "alice", "x") for t in self.posts)
        recs, st = fake_crawl({"/alice": ("text/html", tanstack_html(new_payload(self.posts + [self.old]), body))})
        self.assertEqual((st["status"], len(recs)), ("ok", 5), st)
        self.assertEqual(recs[0]["text"], "post 0")
        self.assertEqual(recs[0]["views"], 1234)
        self.assertFalse(st["truncated"])

    def test_api_x_com_pagination_after_the_server_rendered_page(self):
        older = [new_tweet(snowflake(NOW - timedelta(days=d)), "alice", NOW - timedelta(days=d), f"old {d}") for d in (2, 3, 9)]
        script = """<script>let n = 0; addEventListener('scroll', () => { if (n++ == 0)
          fetch('https://api.x.com/graphql/q9/UserOriginalsTimeline?variables=1') });</script>"""
        tall = '<div style="height:6000px"></div>'
        page = tanstack_html(new_payload(self.posts), HOME + new_article(1, "alice", "x") + tall + script)
        recs, st = fake_crawl({"api.x.com/graphql/": ("application/json", json.dumps(new_payload(older))),
                               "/alice": ("text/html", page)})
        self.assertEqual((st["status"], len(recs)), ("ok", 7), st)  # 5 + 2 in the window; the 9-day-old one is out
        self.assertFalse(st["truncated"])

    def test_dom_fallback_reads_new_markup(self):
        tid = snowflake(NOW - timedelta(hours=2))
        recs, st = fake_crawl({"/alice": ("text/html", f"<html><body>{HOME}<ul>{new_article(tid, 'alice', 'from the dom')}</ul>")})
        self.assertEqual((st["status"], st["source"], len(recs)), ("ok", "dom", 1), st)
        r = recs[0]
        self.assertEqual((r["text"], r["likes"], r["replies"], r["reposts"], r["views"]), ("from the dom", 5659, 1, 2, 621327))
        self.assertEqual(r["url"], f"https://x.com/alice/status/{tid}")
        self.assertEqual(r["created_at"], (NOW - timedelta(hours=2)).isoformat())


class ErrorPage(unittest.TestCase):
    tid = snowflake(NOW - timedelta(hours=1))

    def test_fallback_shown_during_startup_is_not_a_rate_limit(self):
        """X's fallback block is visible for a moment, before the app replaces it."""
        page = f"""<html><body><div id="boot">Something went wrong, but don't fret</div><script>
          setTimeout(() => {{ document.body.innerHTML = '{HOME}<ul>' + {json.dumps(new_article(self.tid, "alice", "x")).replace("<", "\\x3C")} + '</ul>' }}, 1200);
        </script>"""
        _, st = fake_crawl({"/alice": ("text/html", page)})
        self.assertFalse(st["rate_limited"], st)

    def test_error_inside_the_app_is_a_rate_limit(self):
        page = f"<html><body>{HOME}<div>Something went wrong. Try reloading.</div>"
        _, st = fake_crawl({"/alice": ("text/html", page)})
        self.assertEqual((st["rate_limited"], st["limit_signal"]), (True, "error page"), st)

    def test_error_words_in_a_post_are_not_a_rate_limit(self):
        page = f"<html><body>{HOME}<ul>{new_article(self.tid, 'alice', 'Something went wrong with my deploy')}</ul>"
        _, st = fake_crawl({"/alice": ("text/html", page)})
        self.assertFalse(st["rate_limited"], st)


class TallPosts(unittest.TestCase):
    def test_scrolling_through_a_tall_page_is_not_idle(self):
        """The next page only loads near the bottom, many scrolls below the first posts."""
        posts = [new_tweet(snowflake(NOW - timedelta(hours=h + 1)), "alice", NOW - timedelta(hours=h + 1), "x") for h in range(5)]
        older = [new_tweet(snowflake(NOW - timedelta(days=9)), "alice", NOW - timedelta(days=9), "old")]
        page = f"""<html><body>{HOME}<article data-testid="tweet"></article><div style="height:40000px"></div><script>
          const get = n => fetch('/i/api/graphql/q/UserOriginalsTimeline?v=' + n); get(0); let more = true;
          addEventListener('scroll', () => {{ if (more && innerHeight + scrollY > document.body.scrollHeight - 1500) {{ more = false; get(1) }} }});
        </script>"""
        recs, st = fake_crawl({"?v=0": ("application/json", json.dumps(new_payload(posts))),
                               "?v=1": ("application/json", json.dumps(new_payload(older))),
                               "/alice": ("text/html", page)}, max_scrolls=30, idle_rounds=3)
        self.assertEqual((st["status"], len(recs)), ("ok", 5), st)
        self.assertGreater(st["scrolls"], 5)


class Completeness(unittest.TestCase):
    """`ok` means the crawl reached the start of the window, or the timeline ended."""
    posts = [new_tweet(snowflake(NOW - timedelta(hours=h + 1)), "alice", NOW - timedelta(hours=h + 1), f"post {h}")
             for h in range(5)]
    page = f"""<html><body>{HOME}<article data-testid="tweet"></article><div style="height:5000px"></div><script>
      let n = 0, last = 0;
      const get = () => fetch('/i/api/graphql/q/UserOriginalsTimeline?v=' + n++);
      get();
      addEventListener('scroll', () => {{ const up = scrollY < last; last = scrollY; if (up) get(); }});
    </script>"""

    def test_stalled_timeline_is_incomplete(self):
        recs, st = fake_crawl({"?v=0": ("application/json", json.dumps(new_payload(self.posts))),
                               "/alice": ("text/html", self.page)})
        self.assertEqual((st["status"], len(recs)), ("incomplete", 5), st)
        self.assertFalse(st["truncated"])  # that stays reserved for --max-scrolls

    def test_slow_and_stalled_is_still_incomplete(self):
        slow = self.page.replace('<article data-testid="tweet"></article>', '')
        recs, st = fake_crawl({"?v=0": ("application/json", json.dumps(new_payload(self.posts))),
                               "/alice": ("text/html", slow)}, ready_deadline=2)
        self.assertEqual((st["status"], len(recs)), ("incomplete", 5), st)

    def test_end_of_timeline_is_ok(self):
        """A new account: every post is in the window, and X's next page has none."""
        recs, st = fake_crawl({"?v=0": ("application/json", json.dumps(new_payload(self.posts))),
                               "?v=": ("application/json", json.dumps(new_payload([]))),
                               "/alice": ("text/html", self.page)})
        self.assertEqual((st["status"], len(recs)), ("ok", 5), st)

    def test_nudging_the_scroll_loads_the_next_page(self):
        """This page only loads more when the end comes back into view, so wheel-down alone would stall."""
        older = [new_tweet(snowflake(NOW - timedelta(days=d)), "alice", NOW - timedelta(days=d), "x") for d in (2, 9)]
        recs, st = fake_crawl({"?v=0": ("application/json", json.dumps(new_payload(self.posts))),
                               "?v=": ("application/json", json.dumps(new_payload(older))),
                               "/alice": ("text/html", self.page)})
        self.assertEqual((st["status"], len(recs)), ("ok", 6), st)


class ClassicAppCrawl(unittest.TestCase):
    def test_old_thread_root_does_not_stop_the_scroll(self):
        """Page 1 has this week's reply to a 90-day-old root; the crawl must still read page 2."""
        h = "alice"
        module = {"entryId": "profile-conversation-1", "content": {"items": [
            {"item": {"itemContent": {"tweet_results": {"result": tweet(1, h, NOW - timedelta(days=90), "old root")}}}},
            {"item": {"itemContent": {"tweet_results": {"result": tweet(2, h, NOW - timedelta(days=1), "reply", reply_to=h)}}}}]}}
        next_page = payload([{"entryId": "t", "content": {"itemContent": {"tweet_results": {
            "result": tweet(3, h, NOW - timedelta(days=2), "older post")}}}}, {"entryId": "u", "content": {"itemContent": {
                "tweet_results": {"result": tweet(4, h, NOW - timedelta(days=30), "past the window")}}}}], more=False)
        first = payload([module])
        first["dom"] = [{"h": h, "id": "2", "d": "2026-10-20T00:00:00Z", "t": "reply"}]
        script_page = PAGE.replace("/i/api/graphql/q1/UserTweets?variables=' + page", "/i/api/graphql/q1/UserOriginalsTimeline?v=' + page")
        script_page = script_page.replace('<div id="tl"></div>', '<div id="tl"></div><div style="height:5000px"></div>')
        recs, st = fake_crawl({"UserOriginalsTimeline?v=0": ("application/json", json.dumps(first)),
                               "UserOriginalsTimeline?v=1": ("application/json", json.dumps(next_page)),
                               "/alice": ("text/html", script_page)})
        self.assertEqual(sorted(r["id"] for r in recs), ["2", "3"], st)


if __name__ == "__main__":
    unittest.main()
