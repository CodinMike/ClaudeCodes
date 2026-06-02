#!/usr/bin/env python3
"""
TrumpTweets — Clean feed of Trump's posts from multiple sources.
No reposts, endorsements, or staff-written posts. Just the man himself.

Sources (tried in order unless --source is specified):
  truthsocial  — Truth Social API  (may block cloud/VPS IPs)
  twitter      — Nitter RSS mirrors of his Twitter/X feed

Usage:
    python trump_tweets.py
    python trump_tweets.py --limit 10
    python trump_tweets.py --watch
    python trump_tweets.py --source twitter
    python trump_tweets.py --speak --elevenlabs-key YOUR_KEY
"""

import os
import re
import time
import xml.etree.ElementTree as ET
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from email.utils import parsedate_to_datetime

import click
import requests
from bs4 import BeautifulSoup
from rich.console import Console
from rich.panel import Panel
from rich.rule import Rule
from rich.table import Table
from rich.text import Text

try:
    from elevenlabs.client import ElevenLabs as _ElevenLabsClient
    from elevenlabs import play as _el_play
    HAS_ELEVENLABS = True
except ImportError:
    HAS_ELEVENLABS = False

# ── Config ────────────────────────────────────────────────────────────────────

TWITTER_HANDLE = "realDonaldTrump"
POLL_INTERVAL = 60
TRUMP_VOICE_ID_DEFAULT = os.environ.get("TRUMP_VOICE_ID", "TxGEqnHWrfWFTfGW9XjX")

TRUTH_SOCIAL_BASE = "https://truthsocial.com/api/v1"

# Nitter is an open-source Twitter frontend that exposes RSS with no auth needed.
# Trump is back on Twitter (@realDonaldTrump), so Nitter gives us his tweets.
NITTER_INSTANCES = [
    "nitter.net",
    "nitter.privacydev.net",
    "nitter.poast.org",
    "nitter.1d4.us",
    "nitter.unixfox.eu",
    "nitter.woodland.cafe",
]

HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:120.0) Gecko/20100101 Firefox/120.0",
    "Accept": "application/json, application/rss+xml, text/xml, */*",
}

STOPWORDS = {
    "the", "a", "an", "and", "or", "but", "in", "on", "at", "to", "for", "of",
    "with", "is", "are", "was", "were", "be", "been", "being", "have", "has",
    "had", "do", "does", "did", "will", "would", "could", "should", "may",
    "might", "shall", "can", "that", "this", "these", "those", "it", "its",
    "they", "them", "their", "our", "we", "you", "i", "he", "she", "his",
    "her", "not", "no", "by", "as", "so", "all", "very", "just", "from",
    "which", "who", "what", "how", "there", "here", "my", "your", "if", "up",
    "out", "about", "into", "than", "more", "also", "when", "where",
}

_cached_account_id: str | None = None


@dataclass
class SessionStats:
    source: str = ""
    fetched: int = 0
    displayed: int = 0
    filtered_repost: int = 0
    filtered_staff: int = 0
    filtered_endorsement: int = 0
    total_chars: int = 0
    exclamations: int = 0
    questions: int = 0
    caps_letters: int = 0
    total_letters: int = 0
    word_counts: Counter = field(default_factory=Counter)


# ── HTTP ──────────────────────────────────────────────────────────────────────

def _get(url: str, params: dict | None = None, accept_xml: bool = False) -> requests.Response:
    headers = dict(HEADERS)
    if accept_xml:
        headers["Accept"] = "application/rss+xml, text/xml, */*"
    try:
        resp = requests.get(url, params=params, headers=headers, timeout=8)
    except requests.exceptions.ConnectionError as e:
        raise RuntimeError(f"Connection failed: {url}") from e
    except requests.exceptions.Timeout:
        raise RuntimeError(f"Timed out: {url}")

    if resp.status_code == 429:
        time.sleep(30)
        resp = requests.get(url, params=params, headers=headers, timeout=8)

    if resp.status_code == 403:
        reason = resp.headers.get("x-deny-reason", "")
        raise RuntimeError(f"Blocked (403 {reason}): {url}")
    if resp.status_code >= 400:
        raise RuntimeError(f"HTTP {resp.status_code}: {url}")

    return resp


# ── Text utilities ────────────────────────────────────────────────────────────

def strip_html(html: str) -> str:
    text = BeautifulSoup(html, "html.parser").get_text(separator="\n")
    lines = [ln.strip() for ln in text.splitlines()]
    cleaned: list[str] = []
    prev_blank = False
    for ln in lines:
        if ln == "":
            if not prev_blank:
                cleaned.append("")
            prev_blank = True
        else:
            cleaned.append(ln)
            prev_blank = False
    return "\n".join(cleaned).strip()


def format_timestamp(iso_str: str) -> str:
    dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
    return dt.astimezone().strftime("%a %b %d  %I:%M %p").replace("  0", "  ")


# ── Source: Truth Social ──────────────────────────────────────────────────────

def _resolve_ts_account_id() -> str:
    global _cached_account_id
    if _cached_account_id:
        return _cached_account_id
    resp = _get(f"{TRUTH_SOCIAL_BASE}/accounts/lookup", {"acct": TWITTER_HANDLE})
    data = resp.json()
    if not isinstance(data, dict) or "id" not in data:
        raise RuntimeError("Could not resolve Truth Social account")
    _cached_account_id = data["id"]
    return _cached_account_id


def fetch_truth_social() -> tuple[list[dict], str]:
    account_id = _resolve_ts_account_id()
    resp = _get(
        f"{TRUTH_SOCIAL_BASE}/accounts/{account_id}/statuses",
        {"limit": 40, "exclude_replies": "true"},
    )
    posts = resp.json()
    if not isinstance(posts, list):
        raise RuntimeError("Unexpected Truth Social response format")
    return posts, "Truth Social"


# ── Source: Nitter (Twitter/X RSS) ───────────────────────────────────────────

def _parse_rss_items(xml_text: str, instance: str) -> list[dict]:
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        raise RuntimeError(f"Invalid RSS from {instance}: {e}") from e

    channel = root.find("channel")
    if channel is None:
        raise RuntimeError(f"No <channel> in RSS from {instance}")

    posts: list[dict] = []
    for item in channel.findall("item"):
        desc = item.findtext("description") or ""
        pub_date = item.findtext("pubDate") or ""
        link = item.findtext("link") or ""
        guid = item.findtext("guid") or link

        try:
            created_at = parsedate_to_datetime(pub_date).isoformat()
        except Exception:
            created_at = ""

        post_id = guid.split("/")[-1].replace("#m", "") or guid
        text = strip_html(desc)
        is_rt = text.strip().startswith("RT @")

        posts.append({
            "id": f"tw_{post_id}",
            "content": desc,
            "created_at": created_at,
            "url": link,
            "favourites_count": 0,
            "reblogs_count": 0,
            "reblog": {"_rt": True} if is_rt else None,
            "_source": "twitter",
        })

    return posts


def fetch_nitter() -> tuple[list[dict], str]:
    last_err = "No Nitter instances available"
    for instance in NITTER_INSTANCES:
        url = f"https://{instance}/{TWITTER_HANDLE}/rss"
        try:
            resp = _get(url, accept_xml=True)
            posts = _parse_rss_items(resp.text, instance)
            if posts:
                return posts, f"Twitter via {instance}"
        except RuntimeError as e:
            last_err = str(e)
            continue
    raise RuntimeError(f"All Nitter instances failed. Last error: {last_err}")


# ── Source dispatcher ─────────────────────────────────────────────────────────

def get_posts(source: str, console: Console) -> tuple[list[dict], str]:
    sources = {
        "truthsocial": [fetch_truth_social],
        "twitter": [fetch_nitter],
        "auto": [fetch_truth_social, fetch_nitter],
    }
    fetchers = sources.get(source, sources["auto"])
    errors: list[str] = []

    for fetcher in fetchers:
        try:
            return fetcher()
        except RuntimeError as e:
            errors.append(str(e))
            if len(fetchers) > 1:
                console.print(f"[color(240)]  ↳ {fetcher.__name__} failed, trying next source...[/color(240)]")

    raise RuntimeError("All sources failed:\n  " + "\n  ".join(errors))


# ── Filtering ─────────────────────────────────────────────────────────────────

DONATION_KEYWORDS = [
    "winred", "actblue", "fundrais", "donate to", "chip in",
    "split between", "click here to donate",
]
ENDORSEMENT_KEYWORDS = [
    "endorses ", "endorsed ", "for congress", "for senate",
    "for governor", "for house", "for mayor",
]
ENDORSEMENT_RE = re.compile(r"supports \w+ for ", re.IGNORECASE)
STAFF_STARTS = ("THE PRESIDENT", "PRESIDENT TRUMP")


def filter_reason(post: dict, text: str) -> str | None:
    if post.get("reblog") is not None:
        return "repost"

    upper = text.upper()
    if "FOR IMMEDIATE RELEASE" in upper:
        return "staff"
    if any(upper.lstrip().startswith(s) for s in STAFF_STARTS):
        return "staff"

    lower = text.lower()
    if any(kw in lower for kw in DONATION_KEYWORDS):
        return "endorsement"
    if any(kw in lower for kw in ENDORSEMENT_KEYWORDS):
        return "endorsement"
    if ENDORSEMENT_RE.search(text):
        return "endorsement"

    return None


# ── Stats ─────────────────────────────────────────────────────────────────────

def collect_stats(text: str, stats: SessionStats) -> None:
    stats.displayed += 1
    stats.total_chars += len(text)
    stats.exclamations += text.count("!")
    stats.questions += text.count("?")
    letters = [c for c in text if c.isalpha()]
    stats.total_letters += len(letters)
    stats.caps_letters += sum(1 for c in letters if c.isupper())
    for word in re.findall(r"[a-z]+", text.lower()):
        if word not in STOPWORDS and len(word) > 2:
            stats.word_counts[word] += 1


# ── Display ───────────────────────────────────────────────────────────────────

def render_post(post: dict, console: Console, speak_fn=None) -> None:
    text = strip_html(post.get("content", ""))
    ts = format_timestamp(post.get("created_at", ""))
    url = post.get("url", "")
    faves = post.get("favourites_count", 0)
    reblogs = post.get("reblogs_count", 0)
    src = post.get("_source", "")

    body = Text()
    body.append(ts, style="color(244)")
    if src == "twitter":
        body.append("  ·  𝕏 twitter", style="color(240)")
    body.append("\n\n")
    body.append(text, style="bright_white")
    body.append("\n\n")
    if faves:
        body.append(f"❤  {faves:,}", style="bold red")
        body.append("   ")
    if reblogs:
        body.append(f"🔁 {reblogs:,}", style="bold green")
        body.append("   ")
    if url:
        body.append(url, style=f"dim link {url}")

    console.print(
        Panel(body, border_style="color(24)", padding=(0, 1)),
        highlight=False,
    )

    if speak_fn:
        speak_fn(text)


def render_stats(stats: SessionStats, console: Console) -> None:
    filtered = stats.filtered_repost + stats.filtered_staff + stats.filtered_endorsement
    avg_len = stats.total_chars // stats.displayed if stats.displayed else 0
    caps_pct = int(100 * stats.caps_letters / stats.total_letters) if stats.total_letters else 0

    caps_vibe = (
        "practically screaming" if caps_pct > 40
        else "very Trump" if caps_pct > 20
        else "classic Trump" if caps_pct >= 10
        else "unusually calm"
    )
    exclaim_ratio = (
        f"{stats.exclamations / stats.questions:.1f}x more than questions"
        if stats.questions else "no questions, only statements"
    )
    top_words = [w for w, _ in stats.word_counts.most_common(8)]

    console.print(Rule(style="color(240)"))

    table = Table.grid(padding=(0, 2))
    table.add_column(style="color(244)", no_wrap=True)
    table.add_column(style="bright_white")

    if stats.source:
        table.add_row("Source", f"[bold]{stats.source}[/bold]")
        table.add_row("", "")
    table.add_row("Posts fetched", str(stats.fetched))
    table.add_row("Posts shown", f"[bold green]{stats.displayed}[/bold green]")
    table.add_row(
        "Filtered out",
        f"[bold]{filtered}[/bold]  "
        f"[color(240)]("
        f"{stats.filtered_repost} reposts · "
        f"{stats.filtered_endorsement} endorsements · "
        f"{stats.filtered_staff} staff)[/color(240)]"
    )
    table.add_row("", "")
    table.add_row("Avg post length", f"[bold]{avg_len:,}[/bold] chars")
    table.add_row(
        "CAPS ratio",
        f"[bold yellow]{caps_pct}%[/bold yellow]  [color(244) italic]{caps_vibe}[/color(244) italic]"
    )
    table.add_row(
        "Exclamation marks",
        f"[bold]{stats.exclamations}[/bold]  [color(244) italic]{exclaim_ratio}[/color(244) italic]"
    )
    if top_words:
        table.add_row(
            "Favourite words",
            "  ".join(f"[cyan]{w}[/cyan]" for w in top_words)
        )

    console.print(
        Panel(
            table,
            title="[color(244)] session stats [/color(244)]",
            border_style="color(240)",
            padding=(0, 1),
        ),
        highlight=False,
    )


# ── TTS ───────────────────────────────────────────────────────────────────────

def make_speak_fn(api_key: str, voice_id: str):
    if not HAS_ELEVENLABS:
        raise RuntimeError(
            "elevenlabs package not installed.\n"
            "  Install it with: pip install elevenlabs"
        )
    client = _ElevenLabsClient(api_key=api_key)
    url_re = re.compile(r"https?://\S+")

    def speak(text: str) -> None:
        clean = url_re.sub("", text).strip()
        if not clean:
            return
        try:
            audio = client.text_to_speech.convert(
                voice_id=voice_id,
                text=clean[:2500],
                model_id="eleven_multilingual_v2",
            )
            _el_play(audio)
        except Exception:
            pass

    return speak


# ── Core loop ─────────────────────────────────────────────────────────────────

def run_once(
    source: str,
    limit: int,
    console: Console,
    seen_ids: set,
    stats: SessionStats,
    speak_fn=None,
) -> set:
    posts, source_label = get_posts(source, console)
    stats.source = source_label

    new_seen = set(seen_ids)
    displayed_this_run = 0

    for post in posts:
        if displayed_this_run >= limit:
            break

        post_id = post.get("id", "")
        if post_id in seen_ids:
            continue

        new_seen.add(post_id)
        text = strip_html(post.get("content", ""))
        reason = filter_reason(post, text)

        stats.fetched += 1
        if reason == "repost":
            stats.filtered_repost += 1
        elif reason == "staff":
            stats.filtered_staff += 1
        elif reason == "endorsement":
            stats.filtered_endorsement += 1
        else:
            collect_stats(text, stats)
            render_post(post, console, speak_fn)
            displayed_this_run += 1

    if displayed_this_run == 0 and not seen_ids:
        console.print("[yellow]No posts matched filters — all were reposts, endorsements, or staff posts.[/yellow]")

    return new_seen


# ── CLI ───────────────────────────────────────────────────────────────────────

@click.command()
@click.option("--limit", default=20, show_default=True, help="Max posts to show per run.")
@click.option("--watch", is_flag=True, help=f"Poll every {POLL_INTERVAL}s for new posts.")
@click.option(
    "--source",
    type=click.Choice(["auto", "truthsocial", "twitter"], case_sensitive=False),
    default="auto",
    show_default=True,
    help="Post source. 'auto' tries Truth Social then Twitter/Nitter.",
)
@click.option("--speak", is_flag=True, help="Read posts aloud in Trump's voice (requires ElevenLabs).")
@click.option(
    "--elevenlabs-key",
    envvar="ELEVENLABS_API_KEY",
    default=None,
    help="ElevenLabs API key (or set ELEVENLABS_API_KEY). Get one at https://elevenlabs.io",
)
@click.option(
    "--voice-id",
    default=TRUMP_VOICE_ID_DEFAULT,
    show_default=True,
    help="ElevenLabs voice ID. Find Trump voices at https://elevenlabs.io/voice-library",
)
def main(
    limit: int,
    watch: bool,
    source: str,
    speak: bool,
    elevenlabs_key: str | None,
    voice_id: str,
) -> None:
    """TrumpTweets — clean filtered feed from Truth Social and Twitter."""
    console = Console()
    stats = SessionStats()
    source = source.lower()

    speak_fn = None
    if speak:
        if not elevenlabs_key:
            console.print("[bold red]Error:[/bold red] --speak requires an ElevenLabs API key.")
            console.print("  Set it with: export ELEVENLABS_API_KEY=your_key")
            console.print("  Get a key at: https://elevenlabs.io")
            raise SystemExit(1)
        speak_fn = make_speak_fn(elevenlabs_key, voice_id)

    console.print(
        Panel(
            Text.assemble(
                ("TrumpTweets", "bold bright_white"),
                ("  ·  ", "color(240)"),
                ("@", "color(244)"),
                (TWITTER_HANDLE, "color(244)"),
                ("  ·  ", "color(240)"),
                ("filtered feed", "color(244)"),
            ),
            border_style="color(24)",
            padding=(0, 2),
        ),
        highlight=False,
    )

    if watch:
        console.print(f"[color(244)]Refreshing every {POLL_INTERVAL}s — Ctrl+C to stop[/color(244)]\n")

    try:
        seen_ids: set = set()
        seen_ids = run_once(source, limit, console, seen_ids, stats, speak_fn)

        if watch:
            while True:
                time.sleep(POLL_INTERVAL)
                seen_ids = run_once(source, limit, console, seen_ids, stats, speak_fn)

    except RuntimeError as e:
        console.print(f"\n[bold red]Error:[/bold red] {e}")
        raise SystemExit(1)
    except KeyboardInterrupt:
        console.print("\n[color(244)]Stopped.[/color(244)]")
    finally:
        if stats.displayed > 0 or stats.fetched > 0:
            render_stats(stats, console)


if __name__ == "__main__":
    main()
