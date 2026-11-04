# xweek: weekly AI-coding digest from X

Crawls the past week's posts from a list of X accounts, tags the coding-related ones with Claude, and writes a Markdown summary with topics combined across people and links to every post.

The first run, done by agents driving Chrome, took about 2 hours for 107 accounts. Each scroll returned a screenshot to a model, and 20 timelines were cut off at 8 scrolls. This version takes about 24 minutes (measured 2026-11-04):

| | Agent-driven run | xweek |
| --- | --- | --- |
| What is read | Screenshots and page text | The timeline JSON that X's own web app downloads (full text, exact time, likes, views) |
| Model in the browsing loop | Yes, on every scroll | No |
| Parallel profiles | 1 | 3 by default (`--concurrency`) |
| Images, video, fonts | Downloaded | Blocked |
| Scroll limit per profile | 8 | 40, and it stops as soon as it passes the start of the week |
| Time, 107 accounts | about 2 hours | about 24 minutes for the crawl, 16 of them pauses after X's error page, plus about 6 minutes for `summarize.sh` |

## Pipeline

```
handles.txt ──crawl──> data/posts-<since>.jsonl ──tag──> data/tags-<since>.jsonl ──digest──> data/digest-<since>.md
                       data/crawl-stats-<since>.json (per-profile status, scrolls, truncation)
```

1. **`xweek/crawl.py`** uses Playwright with your logged-in X session. It is read-only: it opens profile pages, scrolls with the mouse wheel and listens for the `UserOriginalsTimeline` responses (formerly `UserTweets`). It drops reposts and replies to other people, keeps your own threads, and ignores old pinned posts. If X renames that call, it falls back to reading the page. A `429` or X's "Something went wrong" page pauses all workers (`--backoff`, default 120 s) and retries once. A profile is only `ok` if the crawl reached the start of the week or the timeline ended; otherwise it is retried once and then reported as `incomplete`. Five profiles in a row that don't load stop the run, in case X has stopped serving the crawler. Runs can be resumed: profiles already done are skipped.
2. **`xweek/tag.py`** sends 40 posts per request, 4 requests in parallel. A tool schema forces valid JSON back. Each post gets a coding / not-coding flag, 1–3 topics from a fixed list, the products it names, and a summary of up to 30 words. Posts already tagged are skipped.
3. **`xweek/digest.py`** works out who posted most, the most-liked posts and coverage in plain Python. Then one Claude call writes the TL;DR and the themed sections. The model cites posts as `[@handle](#<post id>)` and the script swaps in the real URLs, so it cannot invent links.

## Setup on a Mac (once)

```bash
bash ~/repos/xweek/install_mac.sh
```

This installs a Python env, opens your Google Chrome on a separate profile used only by the crawler (`~/Library/Application Support/xweek-chrome`) so you can log in to X by hand, checks the login, and loads an on-demand LaunchAgent (`com.xweek.crawl`). It is a normal Chrome window, not an automated one, so X doesn't reject it as an insecure browser. The script never reads or copies your login. It reopens that profile in headless Chrome when it crawls. If X logs it out, run `python -m xweek.crawl login` again.

Only open that profile through `crawl login`, or with `--use-mock-keychain` yourself. Headless Chrome can only read cookies encrypted with Chrome's mock keychain, so both sides use it. Opening the profile without the flag makes Chrome drop the cookies it can't decrypt, and the login is lost. There is no crawl schedule on the Mac. The agent runs `run_weekly.sh`, through `trigger_check.sh`, whenever `trigger/run-request` changes. launchd's `WatchPaths` only notices the file being replaced, not written in place, so the agent also checks it every 30 seconds. The Claude scheduled task (Mondays 08:00) writes that file, waits for `data/status.json` to say `done`, then reads `data/latest.json` and writes the weekly report in the app. So the scheduled task is the only schedule.

The weekly crawl doesn't write a summary unless `ANTHROPIC_API_KEY` is in `.env`. The Claude scheduled task reads the crawl and writes the report itself. To write one on the Mac, with or without a key:

```bash
./summarize.sh                                # the latest crawl -> data/digest-<since>.md
./summarize.sh data/posts-2026-11-02.jsonl    # a particular week
```

It uses the API if `ANTHROPIC_API_KEY` is set, otherwise your logged-in Claude Code. Because posts are other people's writing, Claude Code runs with no tools, MCP servers, settings or hooks. It's safe to rerun: posts already tagged are skipped.

## Manual setup (any OS)

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
playwright install chromium          # or pass --channel chrome to use installed Google Chrome
python -m xweek.crawl login          # opens Chrome on the crawler's own profile; log in to X, then quit that Chrome
python -m xweek.crawl check          # confirms the crawler sees a logged-in timeline
export ANTHROPIC_API_KEY=...         # optional: summaries otherwise use the logged-in Claude Code CLI
```

Your X login lives only in the crawler's Chrome profile, outside the project folder. The script never sees your password or cookies. `--state x_state.json` is an older fallback that X may block.

## Weekly run

```bash
./run_weekly.sh                      # crawl the last 7 days, tag, write the digest
./run_weekly.sh --concurrency 4 --headed   # extra flags go to the crawler
```

To run the steps one at a time:

```bash
python -m xweek.crawl run --handles handles.txt --since 2026-10-14
python -m xweek.tag data/posts-2026-10-14.jsonl            # --dry-run prints a request without calling the API
python -m xweek.digest data/posts-2026-10-14.jsonl data/tags-2026-10-14.jsonl
```

Example schedules:

- On a Mac, using crontab or launchd: `0 7 * * MON cd ~/xweek && ./run_weekly.sh >> data/run.log 2>&1`.
- On the EC2 box: the same line works there. First copy `x_state.json` over from the machine where you logged in.

## Settings

- **Models.** `XWEEK_TAG_MODEL` and `XWEEK_DIGEST_MODEL` both default to `claude-sonnet-5-5`. Check that ID against Anthropic's current model list. A Haiku model is cheaper for tagging.
- **Cost.** At about $2/$10 per million input/output tokens, tagging around 1,000 posts costs roughly $1, and the digest call about $0.15.
- **`handles.txt`.** One handle per line; `#` starts a comment. The file includes the Final list: the top 100 by influence plus the official accounts.
- **Scrolling.** `--max-scrolls 40` and `--idle-rounds 4` control how far each profile is scrolled. Profiles that hit the limit are marked `truncated` in the stats, and their counts show a `+` in the digest.

## Caveats

- **X's terms.** X's terms restrict automated access. This reads at human-like volume, about 100 profiles a week, through your own session. X may still rate-limit or challenge the account. X's paid API is the sanctioned route if that becomes a problem.
- **Where to run it.** Logins from data-centre IPs such as EC2 are more likely to be challenged. If that happens, run the crawl on your Mac and send only the JSONL to the server.
- **Site changes.** X changes its web app often. The parser looks for posts anywhere in the response rather than at fixed paths, and there is a page-reading fallback. Run the tests after any breakage.
- **Two web apps.** Since about November 2026, X gives logged-out visitors a newer app that shows about 5 posts and doesn't paginate; logged-in visitors still get the classic one. The crawler can parse both, in case the newer app reaches logged-in users, but a logged-out page counts as `logged_out` and contributes no posts. `python -m xweek.crawl check` tells you which one you're getting.

## Tests

```bash
python -m unittest discover -s . -p "test_*.py"
```

These run without network access. Crawl tests answer every `x.com` request locally through Playwright routing, with a fake profile that loads more posts on scroll, a missing account, a 429 retry and a resume, plus the newer app's logged-out preview, server-rendered first page, `api.x.com` pagination and markup. Parser tests use synthetic payloads shaped like X's: pins, reposts, quote posts, long posts, self-threads and visibility wrappers.
