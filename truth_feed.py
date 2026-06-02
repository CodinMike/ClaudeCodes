#!/usr/bin/env python3
"""
truth_feed.py — Clean Trump Truth Social feed.
No reposts, endorsements, or staff-written posts. Just the man himself.

Usage:
    python truth_feed.py
    python truth_feed.py --limit 10
    python truth_feed.py --watch
    python truth_feed.py --speak --elevenlabs-key YOUR_KEY

Note: Truth Social blocks requests from cloud/VPS IPs.
Run this on your local machine with a residential connection.
"""

import os
import re
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime

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

BASE_URL = "https://truthsocial.com/api/v1"
ACCOUNT_HANDLE = "realDonaldTrump"
POLL_INTERVAL = 60

# ElevenLabs Trump voice — find IDs at https://elevenlabs.io/voice-library (search "Trump")
# Override with: export TRUMP_VOICE_ID=your_voice_id
TRUMP_VOICE_ID_DEFAULT = os.environ.get("TRUMP_VOICE_ID", "TxGEqnHWrfWFTfGW9XjX")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:120.0) Gecko/20100101 Firefox/120.0",
    "Accept": "application/json",
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

def _get(url: str, params: dict | None = None) -> dict | list:
    try:
        resp = requests.get(url, params=params, headers=HEADERS, timeout=10)
    except requests.exceptions.ConnectionError:
        raise RuntimeError(
            "Could not connect to Truth Social — check your network.\n"
            "  Tip: Truth Social blocks cloud/VPS IPs. Run this on your local machine."
        )
    except requests.exceptions.Timeout:
        raise RuntimeError("Truth Social request timed out after 10s.")

    if resp.status_code == 429:
        time.sleep(30)
        resp = requests.get(url, params=params, headers=HEADERS, timeout=10)

    if resp.status_code == 403:
        reason = resp.headers.get("x-deny-reason", "")
        raise RuntimeError(
            f"Truth Social blocked the request (403 {reason}).\n"
            "  Truth Social only allows residential IPs to access its API.\n"
            "  Run this script on your local machine, not a cloud server."
        )
    if resp.status_code == 404:
        raise RuntimeError(f"Resource not found: {url}")
    if resp.status_code >= 400:
        raise RuntimeError(f"Truth Social API returned HTTP {resp.status_code}")

    return resp.json()


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


# ── API ───────────────────────────────────────────────────────────────────────

def resolve_account_id(handle: str) -> str:
    global _cached_account_id
    if _cached_account_id is not None:
        return _cached_account_id
    data = _get(f"{BASE_URL}/accounts/lookup", {"acct": handle})
    if not isinstance(data, dict) or "id" not in data:
        raise RuntimeError(f"Could not resolve account @{handle}")
    _cached_account_id = data["id"]
    return _cached_account_id


def fetch_posts(account_id: str) -> list[dict]:
    data = _get(
        f"{BASE_URL}/accounts/{account_id}/statuses",
        {"limit": 40, "exclude_replies": "true"},
    )
    if not isinstance(data, list):
        raise RuntimeError("Unexpected response format from Truth Social API")
    return data


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


# ── Stats collection ──────────────────────────────────────────────────────────

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

    body = Text()
    body.append(f"{ts}\n\n", style="color(244)")
    body.append(text, style="bright_white")
    body.append("\n\n")
    body.append(f"❤  {faves:,}", style="bold red")
    body.append("   ")
    body.append(f"🔁 {reblogs:,}", style="bold green")
    if url:
        body.append("   ")
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
        if stats.questions
        else "no questions, only statements"
    )

    top_words = [w for w, _ in stats.word_counts.most_common(8)]

    console.print(Rule(style="color(240)"))

    table = Table.grid(padding=(0, 2))
    table.add_column(style="color(244)", no_wrap=True)
    table.add_column(style="bright_white")

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
            pass  # TTS errors shouldn't crash the feed

    return speak


# ── Main ──────────────────────────────────────────────────────────────────────

def run_once(
    limit: int,
    console: Console,
    seen_ids: set,
    stats: SessionStats,
    speak_fn=None,
) -> set:
    account_id = resolve_account_id(ACCOUNT_HANDLE)
    posts = fetch_posts(account_id)

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

        if reason == "repost":
            stats.filtered_repost += 1
            stats.fetched += 1
        elif reason == "staff":
            stats.filtered_staff += 1
            stats.fetched += 1
        elif reason == "endorsement":
            stats.filtered_endorsement += 1
            stats.fetched += 1
        else:
            stats.fetched += 1
            collect_stats(text, stats)
            render_post(post, console, speak_fn)
            displayed_this_run += 1

    if displayed_this_run == 0 and not seen_ids:
        console.print("[yellow]No posts matched filters — all were reposts, endorsements, or staff posts.[/yellow]")

    return new_seen


@click.command()
@click.option("--limit", default=20, show_default=True, help="Max posts to show per run.")
@click.option("--watch", is_flag=True, help=f"Poll every {POLL_INTERVAL}s for new posts.")
@click.option("--speak", is_flag=True, help="Read posts aloud in Trump's voice (requires ElevenLabs).")
@click.option(
    "--elevenlabs-key",
    envvar="ELEVENLABS_API_KEY",
    default=None,
    help="ElevenLabs API key (or set ELEVENLABS_API_KEY env var). Get one at https://elevenlabs.io",
)
@click.option(
    "--voice-id",
    default=TRUMP_VOICE_ID_DEFAULT,
    show_default=True,
    help="ElevenLabs voice ID. Find Trump voices at https://elevenlabs.io/voice-library",
)
def main(limit: int, watch: bool, speak: bool, elevenlabs_key: str | None, voice_id: str) -> None:
    """Clean Trump Truth Social feed — no ads, reposts, or endorsements."""
    console = Console()
    stats = SessionStats()

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
                ("@", "color(244)"),
                (ACCOUNT_HANDLE, "bold bright_white"),
                ("  ·  ", "color(240)"),
                ("Truth Social  ·  filtered feed", "color(244)"),
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
        seen_ids = run_once(limit, console, seen_ids, stats, speak_fn)

        if watch:
            while True:
                time.sleep(POLL_INTERVAL)
                seen_ids = run_once(limit, console, seen_ids, stats, speak_fn)

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
