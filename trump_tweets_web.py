#!/usr/bin/env python3
"""
TrumpTweets Web — run this, go to http://localhost:5000

    pip install flask requests rich click beautifulsoup4
    python trump_tweets_web.py
"""

import time
import webbrowser
import threading
from flask import Flask, jsonify, render_template_string, request
from trump_tweets import (
    fetch_truth_social, fetch_nitter,
    filter_reason, strip_html, format_timestamp,
)

# ── Core logic (reuses trump_tweets.py) ───────────────────────────────────────

def _get_posts(source: str, limit: int) -> tuple[list, str, dict]:
    fetchers = (
        [fetch_truth_social]         if source == "truthsocial"
        else [fetch_nitter]          if source == "twitter"
        else [fetch_truth_social, fetch_nitter]
    )
    raw, label = None, "?"
    for fn in fetchers:
        try:
            raw, label = fn()
            break
        except Exception:
            continue
    if raw is None:
        raise RuntimeError("All sources failed — check your network.")

    posts, stats = [], {"fetched": len(raw), "shown": 0,
                        "filtered": 0, "repost": 0, "endorsement": 0, "staff": 0}
    for p in raw:
        if len(posts) >= limit:
            break
        text   = strip_html(p.get("content", ""))
        reason = filter_reason(p, text)
        if reason:
            stats["filtered"] += 1
            stats[reason]      = stats.get(reason, 0) + 1
        else:
            posts.append({
                "id":         p.get("id", ""),
                "text":       text,
                "timestamp":  format_timestamp(p.get("created_at", "")),
                "url":        p.get("url", ""),
                "favourites": p.get("favourites_count", 0),
                "reblogs":    p.get("reblogs_count", 0),
                "source":     p.get("_source", "truthsocial"),
            })

    stats["shown"] = len(posts)
    return posts, label, stats


# ── Simple per-source cache (60 s TTL) ────────────────────────────────────────

_cache: dict = {}
CACHE_TTL = 60


def get_posts_cached(source: str, limit: int):
    entry = _cache.get(source)
    if entry and time.time() - entry["ts"] < CACHE_TTL:
        return entry["posts"], entry["label"], entry["stats"]
    posts, label, stats = _get_posts(source, limit)
    _cache[source] = {"posts": posts, "label": label, "stats": stats, "ts": time.time()}
    return posts, label, stats


# ── HTML template ─────────────────────────────────────────────────────────────

HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>TrumpTweets</title>
<style>
  :root {
    --bg:          #0d1117;
    --bg-card:     #161b22;
    --border:      #30363d;
    --border-hover:#58a6ff44;
    --text:        #e6edf3;
    --muted:       #8b949e;
    --blue:        #58a6ff;
    --red:         #f85149;
    --green:       #3fb950;
    --truth-bg:    #122318;
    --truth-fg:    #3fb950;
    --twitter-bg:  #111d2e;
    --twitter-fg:  #58a6ff;
  }
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

  body {
    background: var(--bg);
    color: var(--text);
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto,
                 "Helvetica Neue", sans-serif;
    font-size: 15px;
    line-height: 1.6;
    min-height: 100vh;
  }

  /* ── Header ─────────────────────────────────────────────── */
  header {
    position: sticky; top: 0; z-index: 100;
    background: rgba(13,17,23,.92);
    backdrop-filter: blur(10px);
    -webkit-backdrop-filter: blur(10px);
    border-bottom: 1px solid var(--border);
    padding: 0 20px;
    height: 52px;
    display: flex; align-items: center; justify-content: space-between;
  }
  .logo { font-weight: 700; font-size: 17px; letter-spacing: -.3px; }
  .logo span { opacity: .55; font-weight: 400; font-size: 14px; margin-left: 8px; }
  #refresh-badge {
    font-size: 12px; color: var(--muted);
    display: flex; align-items: center; gap: 6px;
  }
  .dot {
    width: 7px; height: 7px; border-radius: 50%;
    background: var(--green);
    animation: pulse 2s ease-in-out infinite;
  }
  @keyframes pulse {
    0%,100% { opacity: 1; } 50% { opacity: .3; }
  }

  /* ── Source bar ──────────────────────────────────────────── */
  .source-bar {
    display: flex; gap: 8px;
    padding: 10px 20px;
    border-bottom: 1px solid var(--border);
    background: var(--bg);
    overflow-x: auto;
  }
  .src-btn {
    padding: 5px 16px; border-radius: 20px;
    border: 1px solid var(--border);
    background: transparent; color: var(--muted);
    cursor: pointer; font-size: 13px; font-family: inherit;
    white-space: nowrap;
    transition: color .15s, border-color .15s, background .15s;
  }
  .src-btn:hover { border-color: var(--blue); color: var(--blue); }
  .src-btn.active {
    background: var(--blue); border-color: var(--blue);
    color: #0d1117; font-weight: 600;
  }

  /* ── Feed ────────────────────────────────────────────────── */
  #feed {
    max-width: 640px; margin: 0 auto;
    padding: 18px 16px;
    display: flex; flex-direction: column; gap: 12px;
  }

  /* ── Post card ───────────────────────────────────────────── */
  .card {
    background: var(--bg-card);
    border: 1px solid var(--border);
    border-radius: 12px;
    padding: 16px 18px;
    transition: border-color .2s;
    animation: rise .25s ease both;
  }
  .card:hover { border-color: var(--border-hover); }
  @keyframes rise {
    from { opacity: 0; transform: translateY(-5px); }
    to   { opacity: 1; transform: translateY(0); }
  }

  .card-head {
    display: flex; align-items: center; justify-content: space-between;
    margin-bottom: 11px;
  }
  .card-time { font-size: 12px; color: var(--muted); }
  .badge {
    font-size: 11px; font-weight: 500;
    padding: 2px 9px; border-radius: 10px;
  }
  .badge.truth   { background: var(--truth-bg);   color: var(--truth-fg); }
  .badge.twitter { background: var(--twitter-bg); color: var(--twitter-fg); }

  .card-body {
    font-size: 15px; line-height: 1.7;
    white-space: pre-wrap; word-break: break-word;
    color: var(--text);
  }

  .card-foot {
    display: flex; align-items: center; gap: 16px;
    margin-top: 13px;
  }
  .stat { font-size: 13px; color: var(--muted); }
  .stat.h { color: var(--red); }
  .stat.r { color: var(--green); }
  .open-link {
    margin-left: auto; font-size: 12px;
    color: var(--muted); text-decoration: none;
    opacity: .7; transition: opacity .15s, color .15s;
  }
  .open-link:hover { opacity: 1; color: var(--blue); }

  /* ── States ──────────────────────────────────────────────── */
  .empty {
    text-align: center; padding: 60px 20px;
    color: var(--muted); font-size: 14px;
  }
  .spinner-wrap { text-align: center; padding: 60px 20px; }
  .spinner {
    display: inline-block; width: 28px; height: 28px;
    border: 2px solid var(--border); border-top-color: var(--blue);
    border-radius: 50%;
    animation: spin .75s linear infinite;
  }
  @keyframes spin { to { transform: rotate(360deg); } }
  .err { text-align: center; padding: 40px 20px; color: var(--red); font-size: 13px; }

  /* ── Stats footer ────────────────────────────────────────── */
  #stats-footer {
    max-width: 640px; margin: 4px auto 32px;
    padding: 12px 18px;
    background: var(--bg-card); border: 1px solid var(--border);
    border-radius: 10px; font-size: 12px; color: var(--muted);
    display: flex; flex-wrap: wrap; gap: 6px 18px;
  }
  #stats-footer span { white-space: nowrap; }
  .sf-label { opacity: .6; }
  .sf-val   { color: var(--text); font-weight: 500; }
</style>
</head>
<body>

<header>
  <div class="logo">TrumpTweets <span>filtered feed</span></div>
  <div id="refresh-badge">
    <div class="dot"></div>
    <span>refreshing in <strong id="countdown">60</strong>s</span>
  </div>
</header>

<div class="source-bar">
  <button class="src-btn active" data-s="auto"        onclick="pick('auto')">Auto</button>
  <button class="src-btn"        data-s="truthsocial" onclick="pick('truthsocial')">Truth Social</button>
  <button class="src-btn"        data-s="twitter"     onclick="pick('twitter')">&#120143; Twitter</button>
</div>

<div id="feed"><div class="spinner-wrap"><div class="spinner"></div></div></div>
<div id="stats-footer" style="display:none"></div>

<script>
  let src = 'auto', tick = 60, tickerID;

  const esc = s => s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
  const fmtN = n => n >= 1e6 ? (n/1e6).toFixed(1)+'M' : n >= 1e3 ? (n/1e3).toFixed(1)+'K' : ''+n;

  function card(p) {
    const cls   = p.source === 'twitter' ? 'twitter' : 'truth';
    const badge = p.source === 'twitter' ? '&#120143; Twitter' : 'Truth Social';
    const faves = p.favourites ? `<span class="stat h">&#10084; ${fmtN(p.favourites)}</span>` : '';
    const rbs   = p.reblogs   ? `<span class="stat r">&#128257; ${fmtN(p.reblogs)}</span>` : '';
    const link  = p.url       ? `<a class="open-link" href="${esc(p.url)}" target="_blank" rel="noopener">&#8599; open</a>` : '';
    return `<div class="card">
      <div class="card-head">
        <span class="card-time">${esc(p.timestamp)}</span>
        <span class="badge ${cls}">${badge}</span>
      </div>
      <div class="card-body">${esc(p.text)}</div>
      <div class="card-foot">${faves}${rbs}${link}</div>
    </div>`;
  }

  async function load() {
    try {
      const r    = await fetch('/api/posts?source='+src+'&limit=20');
      const data = await r.json();
      const feed = document.getElementById('feed');

      if (data.error) {
        feed.innerHTML = `<div class="err">${esc(data.error)}</div>`;
        return;
      }
      feed.innerHTML = data.posts.length
        ? data.posts.map(card).join('')
        : '<div class="empty">No posts matched filters.</div>';

      const s   = data.stats || {};
      const sf  = document.getElementById('stats-footer');
      sf.style.display = 'flex';
      sf.innerHTML = [
        ['source',       data.source_label],
        ['fetched',      s.fetched  || 0],
        ['shown',        s.shown    || 0],
        ['filtered',     s.filtered || 0],
        ['reposts',      s.repost   || 0],
        ['endorsements', s.endorsement || 0],
        ['staff posts',  s.staff    || 0],
      ].map(([l,v]) => `<span><span class="sf-label">${l} </span><span class="sf-val">${v}</span></span>`).join('');

    } catch(e) {
      document.getElementById('feed').innerHTML = `<div class="err">Failed to load: ${esc(e.message)}</div>`;
    }
  }

  function pick(s) {
    src = s;
    document.querySelectorAll('.src-btn').forEach(b =>
      b.classList.toggle('active', b.dataset.s === s)
    );
    resetTick();
    load();
  }

  function resetTick() {
    clearInterval(tickerID);
    tick = 60;
    document.getElementById('countdown').textContent = 60;
    tickerID = setInterval(() => {
      tick--;
      document.getElementById('countdown').textContent = tick;
      if (tick <= 0) { tick = 60; load(); }
    }, 1000);
  }

  load();
  resetTick();
</script>
</body>
</html>"""

# ── Flask app ─────────────────────────────────────────────────────────────────

app = Flask(__name__)
app.config["JSON_SORT_KEYS"] = False


@app.route("/")
def index():
    return render_template_string(HTML)


@app.route("/api/posts")
def api_posts():
    source = request.args.get("source", "auto").lower()
    if source not in ("auto", "truthsocial", "twitter"):
        source = "auto"
    limit = min(int(request.args.get("limit", 20)), 40)
    try:
        posts, label, stats = get_posts_cached(source, limit)
        return jsonify({"posts": posts, "source_label": label, "stats": stats, "error": None})
    except Exception as e:
        return jsonify({"posts": [], "source_label": "", "stats": {}, "error": str(e)})


if __name__ == "__main__":
    # Open browser after a short delay (server needs a moment to start)
    threading.Timer(1.0, lambda: webbrowser.open("http://localhost:5000")).start()
    print("TrumpTweets → http://localhost:5000  (Ctrl+C to stop)")
    app.run(host="0.0.0.0", port=5000, debug=False)
