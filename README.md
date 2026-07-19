# VALORANT Ticket Watcher

A monitoring/alerting bot for VALORANT Champions Tour (VCT) ticket
announcements and on-sales, posting to a Discord channel via webhook. It
runs as a scheduled GitHub Actions job, not a persistent process.

**This bot never buys anything.** It has no code path that logs into a
ticketing account, fills out a checkout form, or touches a CAPTCHA. It
fetches public pages/feeds, diffs them against what it saw last run, and
tells you when something changed. You click "buy".

## How it works

Each run (`check.py`):

1. Loads `config.yaml` (sources + keywords) and `state/seen.json` (what was
   seen last run).
2. For every source, fetches the page/feed and extracts a generic list of
   "candidate items" - link text + link targets, plus headline/paragraph
   text for pages; title/description/url per item for JSON/RSS feeds.
3. Compares each candidate's identity against the stored seen-list for
   that source. Anything new is checked against the keyword lists.
   Identity is deliberately not a raw text hash: valorantesports.com
   renders dates in a *random language per request* (ignoring
   `Accept-Language`), so links are identified by their URL (stable across
   locales) and text-only items by their text with digits stripped -
   otherwise every locale flip would re-alert the whole page. Pure
   date/score rows are dropped entirely.
4. New items that match a keyword (or, for sources marked
   `alert_on_any_change: true`, any new item) get queued as a Discord alert.
   Matches on the `high_priority` keyword list (or sources with
   `weight: high`) are flagged 🔴 HIGH PRIORITY; everything else is 🔵.
5. All alerts are sent to the Discord webhook (batched, most-severe first).
6. The updated seen-list is written to `state/seen.json`.
7. One source failing (timeout, layout change, HTTP error) is logged and
   skipped - it never stops the other sources or crashes the run.

**First run for a new source is silent by default.** When you add a source,
the first check just records its current content as the baseline instead of
firing alerts for everything already on the page (otherwise adding one
source would dump its entire history into Discord). If you're adding a
source because something just happened and you want to see what's there
right now, either trigger a manual run and read the Action's log output
(the candidates are logged even when not alerted), or set
`baseline_silent: false` on that source temporarily.

## Setup

1. Push this repo (or your fork of it) to GitHub.
2. In your repo: **Settings → Secrets and variables → Actions → New
   repository secret**, name it `DISCORD_WEBHOOK_URL`, and paste a Discord
   channel webhook URL (channel **Settings → Integrations → Webhooks →
   New Webhook**).
3. In **Settings → Actions → General → Workflow permissions**, confirm
   "Read and write permissions" is enabled (the workflow also declares
   `permissions: contents: write` itself, but if your org enforces a
   read-only default at the repo/org level, that setting takes precedence
   and the state-commit push step will fail with a permissions error).
4. That's it - the schedule in `.github/workflows/ticket-check.yml` takes
   over from here.

### Testing without waiting for the schedule

GitHub UI: repo → **Actions** → "VALORANT Ticket Check" → **Run workflow**.

GitHub CLI: `gh workflow run ticket-check.yml`

You can also run it locally (useful while editing `config.yaml`):

```bash
pip install -r requirements.txt
# only needed if any source uses render: playwright
playwright install chromium

# without a webhook, alerts print to stdout instead of posting to Discord
python check.py

# to actually test the Discord send path:
DISCORD_WEBHOOK_URL="https://discord.com/api/webhooks/..." python check.py
```

Running it locally also writes to `state/seen.json` - remember that's the
*same* state file the Action uses, so a local test run changes what the
next Action run considers "already seen". Revert it with
`git checkout -- state/seen.json` if you don't want that.

## The cron schedule

```yaml
schedule:
  - cron: "*/15 * * * *"
```

Runs every 15 minutes. GitHub's documented minimum granularity for
scheduled workflows is 5 minutes (`*/5 * * * *`), but GitHub explicitly
does not guarantee exact timing - scheduled runs get delayed under load,
especially for low-activity repos or during peak UTC hours. Don't rely on
it firing at a precise minute; think of the schedule as "at least this
often, usually."

To change it, edit the `cron:` line in
[`.github/workflows/ticket-check.yml`](.github/workflows/ticket-check.yml)
and push. [crontab.guru](https://crontab.guru) is useful for building the
expression. Cron times are always UTC.

## Why state is committed to the repo (not `actions/cache`)

Each GitHub Actions run starts from a clean checkout, so "what did we
already alert on" has to be persisted somewhere external to the run.

Two reasonable options, and why this repo picks the first:

- **Commit `state/seen.json` back to the repo** (what this does): simple,
  visible, versioned - `git log state/seen.json` is a complete audit trail
  of every state change, and there's no cache-eviction risk. The tradeoff:
  every run that sees new content creates a small commit, so the repo's
  commit history fills up with bot commits over time (harmless, but not
  pretty - the commits are tagged `[skip ci]` so they at least don't
  self-trigger anything).
- **`actions/cache`**: no commit noise, but caches are scoped per-branch and
  can be evicted (GitHub evicts caches unused for 7 days, and caps total
  cache storage per repo at 10GB with LRU eviction) - if that happens
  silently, the bot "forgets" everything it's seen and re-alerts on all of
  it in one run. Also harder to inspect ("what did it think it already
  saw?" means downloading a cache blob instead of just reading a file).

Given this bot's whole job is "don't miss something, don't re-alert on
noise," the durability and inspectability of a committed file outweighed
the commit-log noise.

## Config: `config.yaml`

### Adding a new source

```yaml
sources:
  - id: my_new_source          # unique, stable - used as the state-file key
    name: "Human-readable name"  # shown in Discord alerts
    type: page                 # "page", "json_feed", or "rss"
    url: "https://example.com/tickets"
    render: static              # "static" or "playwright" (type: page only)
    weight: normal               # "normal" or "high"
    # baseline_silent: false     # optional, see "First run" above
    # alert_on_any_change: true  # optional, alert on any new item, not just keyword matches
    # must_match: ["valorant"]   # optional relevance gate: an item only alerts if its
    #                            # text or link contains one of these terms. Set on all
    #                            # search sources so city-keyword results (tourism pages
    #                            # etc.) can't alert - keep it when adding new searches.
```

- `type: page` works for essentially any HTML page: official news pages,
  event/schedule pages, vendor ticket pages, or a search-engine results
  page. It extracts link text+targets and headline/paragraph text
  generically - there's no page-specific scraping logic to maintain.
- `type: json_feed` is for structured feeds that return a JSON array/object
  of items with title/url/description-shaped fields - e.g. the
  `data.rito.news` feeds already configured (see below).
- `type: rss` handles RSS and Atom feeds (namespace-agnostic) - used for
  the Bing search-results feeds, and works for any vendor/news RSS you
  find later.
- `render: playwright` is **only** needed when a page's real content
  requires JavaScript to appear (confirmed React/Vue-style client-rendered
  pages, or ones that gate content behind an XHR call after load). Using it
  when not needed just slows the run down for nothing. `render: static`
  covers everything else, including the official VALORANT news/schedule
  pages and the DuckDuckGo search endpoint used for the general web-search
  checks - both are confirmed server-rendered as of 2026-07.
- Set `weight: high` for anything specifically about China / Hangzhou /
  Shanghai, so every alert from that source is flagged high-priority even
  if the page text doesn't happen to contain one of the `high_priority`
  keywords verbatim.

**A note on ticket vendors:** the config ships with a commented-out example
for Damai (大麦, `search.damai.cn`) since that's a plausible vendor for a
Shanghai/China event, but *not* enabled by default - a generic vendor
search page changes too broadly/often to diff usefully, and the correct
vendor + specific event URL for Champions 2026 Shanghai isn't public yet
(as of 2026-07, per the official site, venue/ticketing details are still
TBD). Once a vendor and event page are announced, add a source pointed at
that *specific event's* page (not a generic search page) and it'll pick up
changes the same way everything else does.

### Editing keywords

```yaml
keywords:
  high_priority: [...]  # China/Hangzhou/Shanghai terms - forces 🔴 on match
  ignore: [...]         # kill-list - matching items are NEVER alerted
  general: [...]        # ticket/on-sale/event-name terms - normal 🔵 alert
```

The `ignore` list mutes past seasons and concluded events ("2025",
"paris", ...) so a search result rotating an old article back into view
doesn't alert. Past content that's already on the watched pages never
alerts anyway (it's baselined into the seen-state); the ignore list only
exists for old items that *newly resurface*, e.g. in search results.
Maintain it as seasons roll over: add "2026" when that season ends, and
remove a city if it's announced as a future host again.

Matching is a case-insensitive substring check (Chinese terms match as-is,
since case doesn't apply). Add event names as they're announced (e.g. a
specific stage/tournament name) to catch mentions before "tickets" or
"on sale" even appears in the text.

## Sources monitored out of the box

- `data.rito.news` JSON feed - a public, non-login-walled mirror of
  official Riot/VALORANT news (see
  [Antosik/rito-news-feeds](https://github.com/Antosik/rito-news-feeds)).
  Closest available thing to an official RSS/API; Riot doesn't publish one
  directly for esports announcements.
- `valorantesports.com` news, schedule, and VCT China league pages.
- Three Bing web-search checks via Bing's RSS output (`&format=rss`):
  Hangzhou event/tickets, Shanghai Champions 2026 venue/tickets, and
  general VCT tickets-on-sale. New search results are diffed like any
  other feed. (DuckDuckGo's HTML endpoint was tried first and serves a
  bot-block page to automated requests - don't switch back to it.)

All of the above are easy to extend - see "Adding a new source".

## Limitations / things to know

- Playwright is only installed in the Action when a source in
  `config.yaml` sets `render: playwright` (checked with a `grep` step
  before the install runs). Installing Chromium adds roughly 30-60s to a
  run when it does kick in - worth it only for pages that genuinely need
  JS execution.
- Bing's RSS endpoint currently works without an API key from residential
  IPs; GitHub Actions runners *may* get blocked at some point. The run log
  makes this visible (a blocked source shows an error or a near-zero
  candidate count with a WARNING) - if it happens, alternatives are a free
  Bing/Brave search API key or dropping the search sources and relying on
  the official news feeds, which cover announcements anyway.
- A `page` source that suddenly yields fewer than 5 candidates logs a
  WARNING - that almost always means the site served a bot-block or error
  page rather than real content.
- The generic candidate-extraction approach (link text + headline/paragraph
  text) is deliberately simple so it works across arbitrary pages without
  per-site scraping code. It can occasionally pick up boilerplate or miss
  something wrapped in an unusual DOM structure - if a source turns out to
  be noisy or silent when it shouldn't be, check the Action run's log
  output (it prints candidate/new/alert counts per source every run) before
  assuming something's broken.
