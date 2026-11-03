"""Full `run` over several fake profiles: parallel workers, a missing account,
one 429 rate limit with retry, output files, and resume."""
import asyncio
import json
import tempfile
import unittest
from argparse import Namespace
from datetime import timedelta
from pathlib import Path

import xweek.crawl as crawl
from tests.fixtures import NOW, paged_timeline

PAGE = """<!doctype html><html><body style="margin:0"><div id="tl"></div><script>
const H = location.pathname.slice(1);
let page = 0, loading = false, done = false;
async function load() {
  if (loading || done) return; loading = true;
  const r = await fetch(`/i/api/graphql/q1/UserTweets?variables=${page}&h=${H}`);
  if (r.status !== 200) { loading = false; return; }
  const j = await r.json();
  for (const it of j.dom) {
    const a = document.createElement('article'); a.setAttribute('data-testid', 'tweet'); a.style.height = '700px';
    a.innerHTML = `<a href="/${it.h}/status/${it.id}"><time datetime="${it.d}">t</time></a>`;
    document.getElementById('tl').appendChild(a);
  }
  if (!j.more) done = true; page++; loading = false;
}
window.addEventListener('scroll', () => { if (innerHeight + scrollY > document.body.scrollHeight - 1500) load(); });
load();
</script></body></html>"""
MISSING = """<!doctype html><html><body><div data-testid="emptyState">This account doesn’t exist</div></body></html>"""


class FakeRun(unittest.TestCase):
    def test_run_and_resume(self):
        data = {h: paged_timeline(handle=h, pages=6, per_page=6, hours_apart=10) for h in ("alice", "carol", "dave")}
        hits = {"dave429": 0}
        original = crawl.make_context

        async def patched(pw, state, headed, channel, profile_dir=None):
            browser, ctx = await original(pw, state, headed, channel, profile_dir)

            async def fake_x(route):
                url = route.request.url
                if "/i/api/graphql/" in url:
                    k, h = url.split("variables=")[1].split("&h=")
                    if h == "dave" and hits["dave429"] == 0:
                        hits["dave429"] += 1
                        return await route.fulfill(status=429, body="rate limited")
                    return await route.fulfill(status=200, content_type="application/json",
                                               body=json.dumps(data[h][int(k)]))
                path = url.split("x.com/")[1]
                await route.fulfill(status=200, content_type="text/html", body=MISSING if path == "bob" else PAGE)

            await ctx.route("https://x.com/**", fake_x)
            return browser, ctx

        crawl.make_context = patched
        try:
            with tempfile.TemporaryDirectory() as d:
                d = Path(d)
                (d / "handles.txt").write_text("# test\n@alice\ncarol  # comment\nbob\ndave\nALICE\n")
                (d / "state.json").write_text(json.dumps({"cookies": [], "origins": []}))
                since = (NOW - timedelta(days=7)).strftime("%Y-%m-%d")
                args = Namespace(handles=str(d / "handles.txt"), state=str(d / "state.json"), profile_dir=None, since=since, days=7,
                                 out_dir=str(d / "data"), concurrency=3, max_scrolls=30, idle_rounds=4,
                                 backoff=1, headed=False, channel=None)
                asyncio.run(crawl.run(args))
                posts = [json.loads(l) for l in (d / "data" / f"posts-{since}.jsonl").read_text().splitlines()]
                stats = json.loads((d / "data" / f"crawl-stats-{since}.json").read_text())

                # window starts at 00:00 UTC 7 days before NOW (12:00) = 180h back, so it holds 19 posts per account
                per = {h: sum(p["handle"] == h for p in posts) for h in ("alice", "carol", "dave")}
                self.assertEqual(per, {"alice": 19, "carol": 19, "dave": 19}, stats)
                self.assertEqual(stats["bob"]["status"], "not_found")
                self.assertEqual(hits["dave429"], 1)
                self.assertEqual(len({p["id"] for p in posts}), len(posts))

                asyncio.run(crawl.run(args))  # resume: ok profiles are skipped, nothing duplicated
                again = (d / "data" / f"posts-{since}.jsonl").read_text().splitlines()
                self.assertEqual(len(again), len(posts))
                print(f"\n  run: {len(posts)} posts; stats: " +
                      ", ".join(f"{h}={s['status']}/{s['kept']}/{s['seconds']}s" for h, s in stats.items()))
        finally:
            crawl.make_context = original


if __name__ == "__main__":
    unittest.main()
