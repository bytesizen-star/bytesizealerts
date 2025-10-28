# country_alerts.py  (Blogger-free)
# Unified agent: RSS + Google CSE -> (Gemini ByteSize-style summary ->) Discord
# Windows-safe UTF-8 logging; smart filter removes "playlist" noise but keeps "history".

import os, sys, json, io
from datetime import datetime as dt, timezone, timedelta
from typing import List, Dict, Tuple
import requests
import feedparser
from bs4 import BeautifulSoup
from html import escape

# ---------- Windows-safe stdout/stderr (avoids cp1252/charmap issues) ----------
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

# =========================
# CONFIG
# =========================

# Expanded roster: top artists, rising names, and general topics
DEFAULT_ARTISTS = [
    # Core mainstream
    "Morgan Wallen","Luke Combs","Lainey Wilson","Carrie Underwood","Zach Bryan",
    "Cody Johnson","Kane Brown","Jelly Roll","Kelsea Ballerini","Miranda Lambert",
    "Reba McEntire","Eric Church","Hardy","Thomas Rhett","Luke Bryan","Chris Stapleton",
    # Traditional & legacy
    "George Strait","Alan Jackson","Brooks and Dunn","Garth Brooks","Trisha Yearwood",
    "Vince Gill","Shania Twain","Martina McBride","Faith Hill","Tim McGraw",
    # 90s/2000s resurgence & icons
    "Randy Travis","Clint Black","Toby Keith","Patty Loveless","The Chicks","Dwight Yoakam",
    # Modern storytellers / Americana
    "Tyler Childers","Sturgill Simpson","Jason Isbell","Ashley McBryde","Margo Price",
    "Caitlyn Smith","Brent Cobb","Parker McCollum","Megan Moroney","Ernest","Bailey Zimmerman",
    # Rising & viral
    "Alexandra Kay","Maggie Baugh","Tigirlily Gold","Lauren Watkins","Chayce Beckham","Corey Kent",
    # Cultural / industry keywords
    "Country Music","Nashville","CMA Awards","ACM Awards","Grand Ole Opry",
    "Country Radio","Country Charts","Billboard Country","Country Music Hall of Fame",
    "Country Touring","Country Festival","AI Country Music","Country TikTok",
    "Country Scandal","Country Lawsuit","Country Collaboration","Country Tribute Show"
]

# Allow CLI override for quick tests: python country_alerts.py "Luke Combs"
ARTISTS = [sys.argv[1]] if len(sys.argv) > 1 else DEFAULT_ARTISTS

# Time/volume knobs
LOOKBACK_HOURS_RSS = 24
CSE_RESULTS_PER_ARTIST = 8
DEBUG_PRINT = True

# Sources
RSS_FEEDS = [
    "https://www.billboard.com/feed/",
    "https://www.rollingstone.com/music/music-country/feed/",
    "https://tasteofcountry.com/rss.xml",
    "https://www.theboot.com/rss",
    "https://www.countrynow.com/feed/",
    "https://variety.com/v/music/news/feed/",
    "https://consequence.net/music/feed/",
]
TRUSTED_HOSTS = (
    "billboard.com","rollingstone.com","variety.com",
    "countrynow.com","theboot.com","consequence.net","tasteofcountry.com"
)

# =========================
# ENV / KEYS
# =========================
# (Blogger removed)
CSE_API_KEY = os.getenv("CSE_API_KEY")
CSE_CX      = os.getenv("CSE_CX")
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL")

# Optional Gemini (auto-fallback if unavailable)
GEMINI_KEY   = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "models/gemini-2.5-flash")
USE_GEMINI   = bool(GEMINI_KEY)
genai = None
if USE_GEMINI:
    try:
        import google.generativeai as genai  # type: ignore
        genai.configure(api_key=GEMINI_KEY)
        if DEBUG_PRINT: print(f"[init] Gemini model: {GEMINI_MODEL}")
    except Exception as e:
        print(f"[warn] Gemini unavailable ({e}); falling back to rules.")
        USE_GEMINI = False

# ---------- Discord ----------
def send_discord(message: str):
    url = DISCORD_WEBHOOK_URL
    if not url:
        return
    try:
        requests.post(url, json={"content": message[:1900]}, timeout=15)
    except Exception as e:
        print(f"[warn] Discord send failed: {e}")

# =========================
# SMART FILTER
# =========================
PLAYLIST_TERMS = ("playlist","playlists","spotify playlist","apple music playlist","best playlist","curated playlist")

def is_noise_playlist(title: str, snippet: str, url: str) -> bool:
    text = f"{title} {snippet} {url}".lower()
    if "history" in text:
        return False
    return any(term in text for term in PLAYLIST_TERMS)

# =========================
# RETRIEVAL: RSS + CSE
# =========================
def recent_rss_hits() -> List[Dict]:
    since = dt.now(timezone.utc) - timedelta(hours=LOOKBACK_HOURS_RSS)
    hits = []
    for url in RSS_FEEDS:
        try:
            feed = feedparser.parse(url)
            for e in feed.entries[:60]:
                pub = getattr(e, "published_parsed", None) or getattr(e, "updated_parsed", None)
                if not pub:
                    continue
                pub_dt = dt(*pub[:6], tzinfo=timezone.utc)
                if pub_dt < since:
                    continue
                title = e.title or ""
                summary = BeautifulSoup(getattr(e, "summary",""), "html.parser").get_text(" ", strip=True)
                link = e.link
                if is_noise_playlist(title, summary, link):
                    continue
                text = (title + " " + summary).lower()
                for a in ARTISTS:
                    if a.lower() in text:
                        hits.append({
                            "source":"rss",
                            "artist":a,
                            "title":title,
                            "url":link,
                            "snippet":summary,
                            "published":pub_dt.isoformat(),
                            "trusted": any(h in link for h in TRUSTED_HOSTS)
                        })
        except Exception:
            continue
    return hits

def google_search_news(query: str, num: int = 6) -> List[Dict]:
    if not CSE_API_KEY or not CSE_CX:
        return []
    try:
        r = requests.get(
            "https://www.googleapis.com/customsearch/v1",
            params={"key":CSE_API_KEY,"cx":CSE_CX,"q":query,"num":num,"safe":"active","hl":"en"},
            timeout=20
        )
        r.raise_for_status()
        items = r.json().get("items", []) or []
        out = []
        for it in items:
            title = it.get("title","")
            link = it.get("link","")
            snippet = it.get("snippet","")
            if is_noise_playlist(title, snippet, link):
                continue
            out.append({
                "source":"cse",
                "artist":None,
                "title":title,
                "url":link,
                "snippet":snippet,
                "published":None,
                "trusted": any(h in link for h in TRUSTED_HOSTS)
            })
        return out
    except Exception:
        return []

def cse_hits_for_artists() -> List[Dict]:
    hits = []
    for a in ARTISTS:
        q = (f'"{a}" (news OR announces OR reveals OR controversy OR tour OR release OR update '
             f'OR statement OR apology OR backlash OR lawsuit) '
             f'site:(billboard.com OR rollingstone.com OR variety.com OR countrynow.com OR '
             f'tasteofcountry.com OR theboot.com OR consequence.net)')
        items = google_search_news(q, num=CSE_RESULTS_PER_ARTIST)
        for it in items:
            it["artist"] = a
        hits.extend(items)
    return hits

# =========================
# BYTE SIZE STYLE RENDER
# =========================
def bisoz_card(headline: str, artist: str, dek: str, links: List[Tuple[str,str]]) -> str:
    headline = headline.strip() or f"{artist} Update"
    dek = escape((dek or "").strip())
    date_str = dt.now().strftime("%b %d, %Y %I:%M %p")
    link_items = "".join(
        f'<li><a href="{escape(u)}" rel="noopener" target="_blank">{escape(t or "Source")}</a></li>'
        for (t,u) in links if u
    )
    return f"""
<article itemscope itemtype="https://schema.org/NewsArticle" style="font-family: system-ui, -apple-system, Segoe UI, Roboto, Arial; line-height:1.5;">
  <p style="color:#555;font-size:0.9rem;"><em>Updated: {date_str}</em></p>
  <h2 style="margin:.15rem 0 0 0;font-size:1.25rem;">{escape(headline)}</h2>
  <p style="margin:.35rem 0 .75rem 0;color:#444;"><strong>{escape(artist or "Country Music")}</strong></p>
  {"<p style='margin:.5rem 0 1rem 0;'>"+dek+"</p>" if dek else ""}
  <h4 style="margin:.5rem 0 .35rem 0; font-size:1rem;">Sources</h4>
  <ul style="margin:.25rem 0 .5rem 1rem; padding:0;">
    {link_items or "<li>No sources listed</li>"}
  </ul>
</article>
""".strip()

def html_to_text(html: str, max_len: int = 1500) -> str:
    try:
        soup = BeautifulSoup(html or "", "html.parser")
        text = soup.get_text(" ", strip=True)
        return text[:max_len]
    except Exception:
        return (html or "")[:max_len]

# =========================
# ALERT BUILDERS
# =========================
def build_rule_alerts(hits: List[Dict]) -> List[Dict]:
    by_artist: Dict[str, List[Dict]] = {}
    seen = set()
    for h in hits:
        if h["url"] in seen:
            continue
        seen.add(h["url"])
        by_artist.setdefault(h["artist"], []).append(h)

    alerts = []
    for artist, items in by_artist.items():
        if not items:
            continue
        items.sort(key=lambda x: (not x["trusted"], (x["published"] or "")))
        top = items[:5]
        links = [(BeautifulSoup(x["title"], "html.parser").get_text() or x["url"], x["url"]) for x in top]
        headline = f"{artist} — Recent Mentions"
        dek = "A concise roundup of credible mentions in the last 24 hours."
        html = bisoz_card(headline, artist, dek, links)
        alerts.append({"title": headline, "html": html, "labels": ["Alert","Country Music","News"]})
    return alerts

# =========================
# MAIN
# =========================
def mask(s: str) -> str:
    if not s:
        return "<unset>"
    return s[:6] + "..." + s[-4:]

def run():
    if DEBUG_PRINT:
        print("[env]", "CSE_API_KEY:", mask(CSE_API_KEY), "CSE_CX:", CSE_CX,
              "GEMINI:", "ON" if USE_GEMINI else "OFF")

    rss = recent_rss_hits()
    cse = cse_hits_for_artists()
    merged = rss + cse

    if DEBUG_PRINT:
        print(f"[retrieval] RSS:{len(rss)} CSE:{len(cse)} Artists:{len(ARTISTS)}")
        for h in (rss[:3] + cse[:3]):
            print(" -", h.get("artist"), "|", h.get("title"), "|", h.get("url"))

    if not merged:
        print("[info] No fresh signals found.")
        send_discord("CountryMusicAgent: no fresh signals in this cycle.")
        return

    by_artist: Dict[str, List[Dict]] = {}
    for h in merged:
        by_artist.setdefault(h["artist"], []).append({
            "title": h["title"], "url": h["url"], "snippet": h["snippet"],
            "source": h["source"], "published": h["published"], "trusted": h["trusted"]
        })

    alerts: List[Dict] = []
    if USE_GEMINI:
        alerts = gemini_alerts_from_evidence(by_artist)
        if not alerts:
            alerts = build_rule_alerts(merged)
            print("[info] Gemini returned no structured alerts; used fallback.")
    else:
        alerts = build_rule_alerts(merged)

    if not alerts:
        print("[info] No alerts triggered.")
        send_discord("CountryMusicAgent: no alerts triggered this run.")
        return

    # Discord-only notifications
    for a in alerts:
        plain = html_to_text(a.get("html", ""))
        msg = f"📰 {a.get('title', 'Alert')}\n{plain}"
        send_discord(msg)

    print(f"Sent {len(alerts)} alert(s) to Discord.")


if __name__ == "__main__":
    run()