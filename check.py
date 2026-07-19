#!/usr/bin/env python3
"""
VALORANT ticket/announcement watcher.

Single "check once and exit" run, meant to be invoked on a schedule by
GitHub Actions (see .github/workflows/ticket-check.yml). It is a MONITORING
tool only: it never logs into anything, never touches a checkout/cart flow,
and never solves or bypasses a CAPTCHA. It fetches public pages/feeds,
diffs them against the last-seen state committed in state/seen.json, and
posts new keyword-matching items to a Discord webhook.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time
import traceback
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin

import requests
import yaml
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.yaml"
STATE_PATH = ROOT / "state" / "seen.json"

HTTP_TIMEOUT = 20
USER_AGENT = (
    "Mozilla/5.0 (compatible; ValorantTicketWatcher/1.0; "
    "+https://github.com/) personal ticket-alert monitor"
)
# Accept-Language must be pinned: valorantesports.com serves dates in a
# random language per request without it, which makes every date row hash
# as "new" content on every run.
BASE_HEADERS = {"User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.9"}

# A "page" source yielding fewer candidates than this almost certainly got
# a bot-block / error page instead of real content - worth a loud warning.
MIN_PLAUSIBLE_PAGE_CANDIDATES = 5

# Generous cap: text-only rows can still accumulate one dedup id per site
# locale (month names differ per language even with digits stripped), and
# evicting a still-live id would cause a re-alert. ~100KB of JSON worst
# case across all sources - cheap insurance.
MAX_SEEN_IDS_PER_SOURCE = 2000
MIN_CANDIDATE_TEXT_LEN = 8
BOILERPLATE_EXACT = {
    "read more",
    "privacy notice",
    "privacy policy",
    "terms of service",
    "terms of use",
    "accessibility",
    "cookie policy",
    "cookie settings",
    "sign in",
    "log in",
    "login",
    "home",
    "news",
    "schedule",
    "standings",
    "next",
    "previous",
    "share",
}

DISCORD_EMBED_BATCH = 10  # Discord max embeds per message


def log(msg: str) -> None:
    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


# --------------------------------------------------------------------------
# Config / state
# --------------------------------------------------------------------------

def load_config() -> dict:
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_state() -> dict:
    if not STATE_PATH.exists():
        return {"sources": {}}
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
            data.setdefault("sources", {})
            return data
    except (json.JSONDecodeError, OSError):
        log("WARNING: state/seen.json is unreadable, starting fresh.")
        return {"sources": {}}


def save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False, sort_keys=True)
        f.write("\n")


def make_id(*parts: str) -> str:
    h = hashlib.sha1()
    for p in parts:
        h.update(p.encode("utf-8", errors="ignore"))
        h.update(b"\x00")
    return h.hexdigest()


# --------------------------------------------------------------------------
# Fetching
# --------------------------------------------------------------------------

def fetch_static_html(url: str) -> str:
    resp = requests.get(url, headers=BASE_HEADERS, timeout=HTTP_TIMEOUT)
    resp.raise_for_status()
    return resp.text


def fetch_playwright_html(url: str) -> str:
    # Imported lazily: only sources with render: playwright need the
    # browser binary, which the workflow only installs when required.
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            page = browser.new_page(
                user_agent=USER_AGENT,
                locale="en-US",
                extra_http_headers={"Accept-Language": BASE_HEADERS["Accept-Language"]},
            )
            page.goto(url, wait_until="networkidle", timeout=30_000)
            return page.content()
        finally:
            browser.close()


def fetch_json(url: str) -> object:
    resp = requests.get(
        url, headers={**BASE_HEADERS, "Accept": "application/json"},
        timeout=HTTP_TIMEOUT,
    )
    resp.raise_for_status()
    return resp.json()


def fetch_text(url: str) -> str:
    resp = requests.get(url, headers=BASE_HEADERS, timeout=HTTP_TIMEOUT)
    resp.raise_for_status()
    return resp.text


# --------------------------------------------------------------------------
# Candidate extraction
# --------------------------------------------------------------------------

def _clean(text: str) -> str:
    return " ".join(text.split()).strip()


def _text_fingerprint(text: str) -> str:
    """Identity for text-only candidates: lowercased with digits removed.
    valorantesports.com renders dates in a random locale per request
    (ignoring Accept-Language), so raw text like '2026年7月8日...' vs
    'June 8, 2026...' would hash as different items every run. Digits are
    the volatile part shared by every locale's date format (and by scores/
    countdowns), so they're excluded from identity. Display text is kept
    as-is - this only affects dedup."""
    return "".join(ch for ch in text.lower() if not ch.isdigit())


def extract_page_candidates(html: str, base_url: str) -> list[dict]:
    """Pull a generic list of {text, url} candidate items out of an
    arbitrary HTML page: link text + href, plus standalone headline/paragraph
    text. Works across news pages, schedule pages, and search-result pages
    without any page-specific scraping rules."""
    soup = BeautifulSoup(html, "html.parser")

    for tag in soup(["script", "style", "noscript", "nav", "footer", "header"]):
        tag.decompose()

    candidates: dict[str, dict] = {}

    for a in soup.find_all("a"):
        text = _clean(a.get_text())
        href = (a.get("href") or "").strip()
        if not text or len(text) < MIN_CANDIDATE_TEXT_LEN:
            continue
        if text.lower() in BOILERPLATE_EXACT:
            continue
        if href.startswith(("javascript:", "mailto:", "#")):
            href = ""
        url = urljoin(base_url, href).split("#", 1)[0] if href else None
        # Identity is the URL when there is one: link text on
        # valorantesports.com embeds a date whose language changes per
        # request, but the article URL is stable. Text-only links fall
        # back to the digit-stripped text fingerprint.
        key = make_id("url", url) if url else make_id("text", _text_fingerprint(text))
        candidates.setdefault(key, {"text": text, "url": url})

    for tag_name in ("h1", "h2", "h3", "h4", "p", "li"):
        for el in soup.find_all(tag_name):
            text = _clean(el.get_text())
            if not text or len(text) < MIN_CANDIDATE_TEXT_LEN:
                continue
            if text.lower() in BOILERPLATE_EXACT:
                continue
            fp = _text_fingerprint(text)
            # After digit-stripping, pure date/score rows ('2026年7月8日')
            # collapse to almost nothing - drop them, they're layout, not news.
            if len(fp.replace(" ", "")) < 6:
                continue
            key = make_id("text", fp)
            candidates.setdefault(key, {"text": text, "url": None})

    return [{"id": k, **v} for k, v in candidates.items()]


def extract_json_feed_candidates(data: object) -> list[dict]:
    """Normalize a json_feed source's payload into {id, text, url} items.
    Handles a plain list of item dicts (rito.news style) or a dict with an
    'items'/'entries' list."""
    if isinstance(data, dict):
        items = data.get("items") or data.get("entries") or data.get("data") or []
    elif isinstance(data, list):
        items = data
    else:
        items = []

    candidates = []
    for item in items:
        if not isinstance(item, dict):
            continue
        title = _clean(str(item.get("title") or item.get("name") or ""))
        desc = _clean(str(item.get("description") or item.get("summary") or ""))
        url = item.get("url") or item.get("link")
        uid = str(item.get("uid") or item.get("id") or url or title)
        if not title and not desc:
            continue
        text = f"{title} - {desc}" if desc else title
        candidates.append({"id": make_id(uid), "text": text, "url": url})
    return candidates


def _local_tag(el) -> str:
    """Element tag without XML namespace prefix."""
    return el.tag.rsplit("}", 1)[-1].lower()


def extract_rss_candidates(xml_text: str) -> list[dict]:
    """Normalize an RSS <item> or Atom <entry> feed into {id, text, url}
    items. Namespace-agnostic so it handles both formats (e.g. Bing's
    search-results RSS, or any vendor/news RSS you add later)."""
    root = ET.fromstring(xml_text)
    candidates = []
    for el in root.iter():
        if _local_tag(el) not in ("item", "entry"):
            continue
        title = desc = url = uid = None
        for child in el:
            tag = _local_tag(child)
            text = (child.text or "").strip()
            if tag == "title":
                title = text
            elif tag in ("description", "summary", "content"):
                desc = text
            elif tag == "link":
                # RSS: text content; Atom: href attribute
                url = text or child.get("href")
            elif tag in ("guid", "id"):
                uid = text

        # Descriptions in search-result feeds are usually HTML - strip tags.
        if desc:
            desc = _clean(BeautifulSoup(desc, "html.parser").get_text())
        title = _clean(title or "")
        if not title and not desc:
            continue
        text = f"{title} - {desc}" if desc else title
        candidates.append(
            {"id": make_id(uid or url or text), "text": text, "url": url}
        )
    return candidates


# --------------------------------------------------------------------------
# Keyword matching
# --------------------------------------------------------------------------

def match_keywords(text: str, high_kw: list[str], general_kw: list[str]):
    text_l = text.lower()
    matched_high = [kw for kw in high_kw if kw.lower() in text_l]
    matched_general = [kw for kw in general_kw if kw.lower() in text_l]
    return matched_high, matched_general


# --------------------------------------------------------------------------
# Per-source processing
# --------------------------------------------------------------------------

def process_source(source: dict, state: dict, keywords: dict) -> list[dict]:
    """Returns a list of alert dicts for newly-seen, keyword-matching (or
    alert_on_any_change) items. Mutates `state` in place with new seen ids."""
    src_id = source["id"]
    name = source.get("name", src_id)
    src_type = source.get("type", "page")
    weight = source.get("weight", "normal")
    baseline_silent = source.get("baseline_silent", True)
    alert_on_any_change = source.get("alert_on_any_change", False)
    # Relevance gate: when set, an item must contain at least one of these
    # terms to alert at all. Essential for search sources, where results
    # matching the query's city terms (tourism pages etc.) have nothing to
    # do with VALORANT.
    must_match = [kw.lower() for kw in source.get("must_match", [])]

    src_state = state["sources"].setdefault(
        src_id, {"seen_ids": [], "last_checked": None}
    )
    is_first_run = not src_state["seen_ids"] and src_state["last_checked"] is None
    seen_ids = set(src_state["seen_ids"])

    url = source["url"]
    if src_type == "page":
        render = source.get("render", "static")
        html = fetch_playwright_html(url) if render == "playwright" else fetch_static_html(url)
        candidates = extract_page_candidates(html, url)
        if len(candidates) < MIN_PLAUSIBLE_PAGE_CANDIDATES:
            log(
                f"  WARNING: {name} returned only {len(candidates)} candidate(s) - "
                "this usually means a bot-block or error page, not real content. "
                "Check the URL in a browser."
            )
    elif src_type == "json_feed":
        data = fetch_json(url)
        candidates = extract_json_feed_candidates(data)
    elif src_type == "rss":
        candidates = extract_rss_candidates(fetch_text(url))
    else:
        raise ValueError(f"Unknown source type: {src_type!r}")

    alerts = []
    new_ids = []
    for cand in candidates:
        cid = cand["id"]
        if cid in seen_ids:
            continue
        new_ids.append(cid)

        text_l = cand["text"].lower()
        # A search result's link URL often carries the game name when the
        # snippet doesn't (e.g. vlr.gg/... paths) - check both.
        gate_text = text_l + " " + (cand.get("url") or "").lower()
        if must_match and not any(kw in gate_text for kw in must_match):
            continue  # already recorded as seen above, but never alertable
        ignored = [kw for kw in keywords.get("ignore", []) if kw.lower() in text_l]
        matched_high, matched_general = match_keywords(
            cand["text"], keywords.get("high_priority", []), keywords.get("general", [])
        )
        matched = matched_high + matched_general
        should_alert = (bool(matched) or alert_on_any_change) and not ignored
        if is_first_run and baseline_silent:
            should_alert = False

        if should_alert:
            is_high = weight == "high" or bool(matched_high)
            alerts.append(
                {
                    "source_name": name,
                    "source_id": src_id,
                    "text": cand["text"][:300],
                    "url": cand.get("url") or url,
                    "matched": matched,
                    "high_priority": is_high,
                }
            )

    log(
        f"  {name}: {len(candidates)} candidates, {len(new_ids)} new, "
        f"{len(alerts)} alert(s){' [baseline run, no alerts]' if is_first_run and baseline_silent and new_ids else ''}"
    )

    all_ids = src_state["seen_ids"] + new_ids
    src_state["seen_ids"] = all_ids[-MAX_SEEN_IDS_PER_SOURCE:]
    src_state["last_checked"] = datetime.now(timezone.utc).isoformat()

    return alerts


def dedupe_alerts(alerts: list[dict]) -> list[dict]:
    """The same article often surfaces as both a link candidate and a
    headline text-node candidate. Within one run, drop an alert whose text
    is contained in (or contains) another alert's text from the same
    source, keeping the longer (more informative) one."""
    kept: list[dict] = []
    for a in alerts:
        fp_a = _text_fingerprint(a["text"])
        dup_of = None
        for k in kept:
            if a["source_id"] != k["source_id"]:
                continue
            fp_k = _text_fingerprint(k["text"])
            if fp_a in fp_k or fp_k in fp_a:
                dup_of = k
                break
        if dup_of is None:
            kept.append(a)
        elif len(a["text"]) > len(dup_of["text"]):
            kept[kept.index(dup_of)] = a
    return kept


# --------------------------------------------------------------------------
# Discord
# --------------------------------------------------------------------------

def build_embed(alert: dict) -> dict:
    high = alert["high_priority"]
    prefix = "🔴 HIGH PRIORITY" if high else "🔵 Update"
    color = 0xE74C3C if high else 0x3498DB

    matched = ", ".join(alert["matched"]) if alert["matched"] else "content change"
    description = f"**Matched:** {matched}\n\n{alert['text']}"
    if len(description) > 4000:
        description = description[:4000] + "..."

    embed = {
        "title": f"{prefix} - {alert['source_name']}"[:256],
        "description": description,
        "color": color,
        "footer": {"text": f"source: {alert['source_id']}"},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    if alert["url"]:
        embed["url"] = alert["url"]
    return embed


def send_discord_alerts(alerts: list[dict], webhook_url: str | None, discord_cfg: dict) -> None:
    if not alerts:
        log("No alerts to send.")
        return

    if not webhook_url:
        log(f"DISCORD_WEBHOOK_URL not set - printing {len(alerts)} alert(s) instead of sending:")
        for a in alerts:
            log(f"  [{'HIGH' if a['high_priority'] else 'normal'}] {a['source_name']}: {a['text'][:120]}")
        return

    # High priority first.
    alerts_sorted = sorted(alerts, key=lambda a: not a["high_priority"])
    username = discord_cfg.get("username", "VALORANT Ticket Watcher")

    for i in range(0, len(alerts_sorted), DISCORD_EMBED_BATCH):
        batch = alerts_sorted[i : i + DISCORD_EMBED_BATCH]
        payload = {
            "username": username,
            "embeds": [build_embed(a) for a in batch],
        }
        try:
            resp = requests.post(webhook_url, json=payload, timeout=HTTP_TIMEOUT)
            if resp.status_code == 429:
                retry_after = resp.json().get("retry_after", 1)
                time.sleep(float(retry_after) + 0.5)
                resp = requests.post(webhook_url, json=payload, timeout=HTTP_TIMEOUT)
            resp.raise_for_status()
            log(f"Sent {len(batch)} alert(s) to Discord.")
        except requests.RequestException as e:
            log(f"ERROR sending Discord alert batch: {e}")
        time.sleep(1)  # stay well under Discord's per-webhook rate limit


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def main() -> int:
    config = load_config()
    state = load_state()
    webhook_url = os.environ.get("DISCORD_WEBHOOK_URL", "").strip() or None

    sources = config.get("sources", [])
    keywords = config.get("keywords", {})
    discord_cfg = config.get("discord", {})

    log(f"Checking {len(sources)} source(s)...")

    all_alerts: list[dict] = []
    failures: list[str] = []

    for source in sources:
        src_id = source.get("id", "<unknown>")
        try:
            alerts = process_source(source, state, keywords)
            all_alerts.extend(alerts)
        except Exception as e:  # noqa: BLE001 - one bad source must not kill the run
            failures.append(src_id)
            log(f"ERROR checking source '{src_id}': {e}")
            traceback.print_exc()

    save_state(state)
    all_alerts = dedupe_alerts(all_alerts)
    send_discord_alerts(all_alerts, webhook_url, discord_cfg)

    log(
        f"Done. {len(all_alerts)} alert(s) sent, {len(failures)} source(s) failed"
        + (f" ({', '.join(failures)})" if failures else "") + "."
    )
    # Never fail the job just because a source errored - that's expected
    # transient noise (rate limits, layout changes, timeouts), and would
    # otherwise stop state from being committed by the workflow's next step.
    return 0


if __name__ == "__main__":
    sys.exit(main())
