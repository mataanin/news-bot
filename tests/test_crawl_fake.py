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
from tests.fixtures import NOW, paged_timeline

PAGE = """<!doctype html><html><body style="margin:0"><div id="tl"></div><script>
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


if __name__ == "__main__":
    unittest.main()
