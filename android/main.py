"""
TrumpTweets Android App
Clean filtered feed from Truth Social and Twitter.

=== HOW TO BUILD THE APK ===

1. Install Buildozer (Linux/macOS recommended):
   pip install buildozer cython

2. Install system dependencies (Ubuntu/Debian):
   sudo apt install -y git zip unzip openjdk-17-jdk python3-pip \
       autoconf libtool pkg-config zlib1g-dev libncurses5-dev \
       libncursesw5-dev libtinfo5 cmake libffi-dev libssl-dev

3. From the android/ directory, build debug APK:
   cd android/
   buildozer android debug

   First build downloads the Android SDK/NDK (~1.5GB) and takes 20-40 min.
   Subsequent builds are fast.

4. Install on your phone:
   buildozer android deploy run    # if ADB connected
   -- or --
   adb install bin/TrumpTweets-1.0.0-arm64-v8a-debug.apk

The app requires an internet connection. Truth Social works on residential
IPs; if that fails it automatically falls back to Twitter via Nitter.
"""

import re
import threading
import xml.etree.ElementTree as ET
from datetime import datetime
from email.utils import parsedate_to_datetime

import requests
from kivy.clock import Clock
from kivy.lang import Builder
from kivy.metrics import dp
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.scrollview import ScrollView
from kivy.uix.widget import Widget
from kivymd.app import MDApp
from kivymd.uix.button import MDFlatButton, MDRaisedButton
from kivymd.uix.card import MDCard
from kivymd.uix.label import MDLabel
from kivymd.uix.snackbar import Snackbar
from kivymd.uix.spinner import MDSpinner
from kivymd.uix.toolbar import MDTopAppBar

# ─── KV layout ────────────────────────────────────────────────────────────────

KV = """
<PostCard>:
    orientation: "vertical"
    adaptive_height: True
    padding: [dp(16), dp(12), dp(16), dp(12)]
    spacing: dp(6)
    radius: [14, 14, 14, 14]
    ripple_behavior: True
    md_bg_color: 0.09, 0.11, 0.16, 1

    MDLabel:
        id: header
        text: root.header_text
        adaptive_height: True
        font_style: "Caption"
        theme_text_color: "Custom"
        text_color: 0.55, 0.60, 0.68, 1

    MDLabel:
        id: body
        text: root.body_text
        adaptive_height: True
        font_style: "Body1"
        theme_text_color: "Primary"
        text_size: self.width, None

    MDLabel:
        id: footer
        text: root.footer_text
        adaptive_height: True
        font_style: "Caption"
        theme_text_color: "Hint"
"""

Builder.load_string(KV)

# ─── Config ───────────────────────────────────────────────────────────────────

HANDLE = "realDonaldTrump"
TRUTH_BASE = "https://truthsocial.com/api/v1"
NITTER_INSTANCES = [
    "nitter.net",
    "nitter.privacydev.net",
    "nitter.poast.org",
    "nitter.1d4.us",
    "nitter.unixfox.eu",
]
HTTP_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36",
    "Accept": "application/json",
}

# ─── Text helpers ─────────────────────────────────────────────────────────────

_HTML_BLOCK = re.compile(r"<br\s*/?>|</?p[^>]*>", re.IGNORECASE)
_HTML_TAG   = re.compile(r"<[^>]+>")
_ENTITIES   = [("&amp;","&"),("&lt;","<"),("&gt;",">"),("&quot;",'"'),
               ("&#39;","'"),("&nbsp;"," "),("&mdash;","—"),("&ndash;","–")]

def strip_html(html: str) -> str:
    html = _HTML_BLOCK.sub("\n", html)
    html = _HTML_TAG.sub("", html)
    for ent, ch in _ENTITIES:
        html = html.replace(ent, ch)
    html = re.sub(r"\n{3,}", "\n\n", html)
    return html.strip()


def fmt_ts(iso: str) -> str:
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone()
        return dt.strftime("%b %-d  %I:%M %p")
    except Exception:
        return iso[:10]


def fmt_num(n: int) -> str:
    if n >= 1_000_000: return f"{n/1_000_000:.1f}M"
    if n >= 1_000:     return f"{n/1_000:.1f}K"
    return str(n)

# ─── Filtering ────────────────────────────────────────────────────────────────

_DONATION  = ["winred","actblue","fundrais","donate to","chip in","split between"]
_ENDORSE   = ["endorses ","endorsed ","for congress","for senate","for governor","for house","for mayor"]
_ENDORSE_R = re.compile(r"supports \w+ for ", re.IGNORECASE)
_STAFF_PFX = ("THE PRESIDENT", "PRESIDENT TRUMP")


def filter_reason(post: dict, text: str):
    if post.get("reblog"):
        return "repost"
    up = text.upper()
    if "FOR IMMEDIATE RELEASE" in up:
        return "staff"
    if any(up.lstrip().startswith(s) for s in _STAFF_PFX):
        return "staff"
    lo = text.lower()
    if any(k in lo for k in _DONATION):
        return "endorsement"
    if any(k in lo for k in _ENDORSE):
        return "endorsement"
    if _ENDORSE_R.search(text):
        return "endorsement"
    return None

# ─── Fetching ─────────────────────────────────────────────────────────────────

_ts_id = None


def _get(url, params=None, xml=False):
    hdrs = dict(HTTP_HEADERS)
    if xml:
        hdrs["Accept"] = "application/rss+xml, text/xml"
    r = requests.get(url, params=params, headers=hdrs, timeout=10)
    if r.status_code == 403:
        reason = r.headers.get("x-deny-reason", "")
        raise RuntimeError(f"Blocked 403 {reason}")
    r.raise_for_status()
    return r


def _fetch_truth():
    global _ts_id
    if not _ts_id:
        data = _get(f"{TRUTH_BASE}/accounts/lookup", {"acct": HANDLE}).json()
        _ts_id = data["id"]
    posts = _get(f"{TRUTH_BASE}/accounts/{_ts_id}/statuses",
                 {"limit": 40, "exclude_replies": "true"}).json()
    return posts, "Truth Social"


def _parse_rss(xml_text: str):
    root = ET.fromstring(xml_text)
    chan = root.find("channel")
    posts = []
    for item in chan.findall("item"):
        desc  = item.findtext("description") or ""
        pub   = item.findtext("pubDate") or ""
        link  = item.findtext("link") or ""
        guid  = (item.findtext("guid") or link).split("/")[-1].replace("#m","")
        try:    ts = parsedate_to_datetime(pub).isoformat()
        except: ts = ""
        text = strip_html(desc)
        posts.append({
            "id": f"tw_{guid}",
            "content": desc,
            "created_at": ts,
            "url": link,
            "favourites_count": 0,
            "reblogs_count": 0,
            "reblog": {"_rt": True} if text.startswith("RT @") else None,
            "_source": "twitter",
        })
    return posts


def _fetch_nitter():
    last = "no instances worked"
    for inst in NITTER_INSTANCES:
        try:
            r = _get(f"https://{inst}/{HANDLE}/rss", xml=True)
            posts = _parse_rss(r.text)
            if posts:
                return posts, f"Twitter / {inst}"
        except Exception as e:
            last = str(e)
    raise RuntimeError(f"Nitter failed: {last}")


def load_posts(source="auto", limit=20):
    fetchers = (
        [_fetch_truth] if source == "truthsocial"
        else [_fetch_nitter] if source == "twitter"
        else [_fetch_truth, _fetch_nitter]
    )
    raw, label = None, "?"
    for fn in fetchers:
        try:
            raw, label = fn()
            break
        except Exception:
            continue
    if raw is None:
        raise RuntimeError("All sources failed — check your internet connection.")

    shown, filtered = [], {"repost": 0, "endorsement": 0, "staff": 0}
    for p in raw:
        if len(shown) >= limit:
            break
        text = strip_html(p.get("content", ""))
        reason = filter_reason(p, text)
        if reason:
            filtered[reason] = filtered.get(reason, 0) + 1
        else:
            p["_text"] = text
            p["_ts"]   = fmt_ts(p.get("created_at", ""))
            shown.append(p)

    stats = {
        "fetched":  len(raw),
        "shown":    len(shown),
        "filtered": sum(filtered.values()),
        **filtered,
    }
    return shown, label, stats

# ─── UI widgets ───────────────────────────────────────────────────────────────

class PostCard(MDCard):
    def __init__(self, post, **kwargs):
        super().__init__(**kwargs)
        src    = post.get("_source", "truthsocial")
        faves  = post.get("favourites_count", 0)
        rbs    = post.get("reblogs_count", 0)
        url    = post.get("url", "")

        src_tag = "𝕏 Twitter" if src == "twitter" else "Truth Social"

        self.header_text = f"{post.get('_ts','')}  ·  {src_tag}"
        self.body_text   = post.get("_text", "")

        parts = []
        if faves:  parts.append(f"❤  {fmt_num(faves)}")
        if rbs:    parts.append(f"🔁 {fmt_num(rbs)}")
        if url:    parts.append(url)
        self.footer_text = "   ".join(parts)


class SourceBar(BoxLayout):
    SOURCES = [("Auto", "auto"), ("Truth Social", "truthsocial"), ("𝕏 Twitter", "twitter")]
    COLOR_ON  = (0.22, 0.48, 0.93, 1)
    COLOR_OFF = (0.12, 0.14, 0.19, 1)

    def __init__(self, on_change, **kwargs):
        super().__init__(
            orientation="horizontal",
            size_hint_y=None,
            height=dp(46),
            spacing=dp(6),
            padding=[dp(10), dp(5)],
            **kwargs,
        )
        self._on_change = on_change
        self._btns = {}
        for label, key in self.SOURCES:
            btn = MDRaisedButton(
                text=label,
                size_hint_x=1,
                md_bg_color=self.COLOR_ON if key == "auto" else self.COLOR_OFF,
                elevation=0,
                on_release=lambda _, k=key: self._select(k),
            )
            self._btns[key] = btn
            self.add_widget(btn)
        self._current = "auto"

    def _select(self, key):
        if key == self._current:
            return
        self._current = key
        for k, btn in self._btns.items():
            btn.md_bg_color = self.COLOR_ON if k == key else self.COLOR_OFF
        self._on_change(key)


class FeedView(BoxLayout):
    def __init__(self, **kwargs):
        super().__init__(orientation="vertical", **kwargs)
        self._source = "auto"

        # ── toolbar
        bar = MDTopAppBar(title="TrumpTweets", md_bg_color=(0.05, 0.07, 0.11, 1))
        bar.right_action_items = [["refresh", lambda _: self.refresh()]]
        self.add_widget(bar)

        # ── source buttons
        self.add_widget(SourceBar(on_change=self._on_source))

        # ── scrollable feed
        self._scroll = ScrollView(do_scroll_x=False)
        self._list   = BoxLayout(
            orientation="vertical",
            spacing=dp(10),
            padding=[dp(10), dp(10)],
            size_hint_y=None,
        )
        self._list.bind(minimum_height=self._list.setter("height"))
        self._scroll.add_widget(self._list)
        self.add_widget(self._scroll)

        # ── spinner (centered over feed, hidden by default)
        self._spinner = MDSpinner(
            size_hint=(None, None), size=(dp(46), dp(46)),
            pos_hint={"center_x": 0.5, "center_y": 0.5},
            active=False,
        )
        self.add_widget(self._spinner)

        # ── status bar
        self._status = MDLabel(
            text="", theme_text_color="Hint", font_style="Caption",
            size_hint_y=None, height=dp(30), halign="center",
        )
        self.add_widget(self._status)

    def _on_source(self, source):
        self._source = source
        self.refresh()

    def refresh(self):
        self._spinner.active = True
        self._status.text = "Loading…"

        def _work():
            try:
                posts, label, stats = load_posts(self._source)
                Clock.schedule_once(lambda dt: self._render(posts, label, stats))
            except Exception as e:
                Clock.schedule_once(lambda dt: self._error(str(e)))

        threading.Thread(target=_work, daemon=True).start()

    def _render(self, posts, label, stats):
        self._spinner.active = False
        self._list.clear_widgets()

        if not posts:
            self._list.add_widget(MDLabel(
                text="No posts passed filters.",
                theme_text_color="Hint",
                halign="center",
                size_hint_y=None,
                height=dp(80),
            ))
        else:
            for p in posts:
                self._list.add_widget(PostCard(p))

        shown    = stats.get("shown", 0)
        filtered = stats.get("filtered", 0)
        self._status.text = f"{label}  ·  {shown} posts  ·  {filtered} filtered"

    def _error(self, msg):
        self._spinner.active = False
        self._status.text = "Failed to load"
        Snackbar(text=msg[:100], snackbar_x=dp(8), snackbar_y=dp(8)).open()

# ─── App ──────────────────────────────────────────────────────────────────────

class TrumpTweetsApp(MDApp):
    def build(self):
        self.theme_cls.theme_style    = "Dark"
        self.theme_cls.primary_palette = "Blue"
        self.title = "TrumpTweets"
        self._feed = FeedView()
        Clock.schedule_once(lambda dt: self._feed.refresh(), 0.3)
        return self._feed


if __name__ == "__main__":
    TrumpTweetsApp().run()
