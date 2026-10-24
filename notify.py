#!/usr/bin/env python3
"""CAS news to the phone - runs in the cloud (GitHub Actions), not on the Mac.

Every few minutes: reads the market news feeds, keeps the headlines about
the shares he holds and watches, plus the few stories that move the whole
market, and sends each new one to his ntfy app as its own notification.
Tapping it opens the article. Nothing is kept anywhere: what was already
sent is read back from ntfy itself (it remembers the last 12 hours).

Settings come from the repository's secrets (set by the dashboard):
  NTFY_TOPIC  the ntfy name his phone is subscribed to
  WATCH       JSON {"held": [[SYMBOL, term, ...], ...],
                    "watch": [[SYMBOL, term, ...], ...], "quiet": [22, 7]}
  GEMINI_KEY  optional: picks the market-wide stories worth a notification
Python 3 standard library only.
"""
import datetime
import html
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import email.utils

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 " \
     "(KHTML, like Gecko) Chrome/120 Safari/537.36"
NTFY = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
FEEDS = [
    ("Economic Times", "https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms"),
    ("Economic Times", "https://economictimes.indiatimes.com/markets/stocks/news/rssfeeds/2146842.cms"),
    ("Livemint", "https://www.livemint.com/rss/markets"),
    ("Business Standard", "https://www.business-standard.com/rss/markets-106.rss"),
    ("Moneycontrol", "https://www.moneycontrol.com/rss/latestnews.xml"),
    ("Moneycontrol", "https://www.moneycontrol.com/rss/business.xml"),
    ("NDTV Profit", "https://feeds.feedburner.com/ndtvprofit-latest"),
]
MAX_AGE_H = 3            # a headline older than this is not news any more
DAY_CAP = 60             # notifications in 12 hours, at most
PER_RUN = 5              # notifications from one run, at most (20.10: the
                         # first run sent 36 at once, and the 12-hour cap
                         # then held back everything else that day)
MARKET_STYLE = (
    "You pick headlines for an Indian share trader's phone. From the "
    "numbered headlines below, give the numbers of the few (at most 4) that "
    "would move the whole Indian stock market or a whole sector today: RBI "
    "or Fed decisions, big government policy, budget or tax changes, sharp "
    "moves in crude, the rupee or global markets, war or crisis, major "
    "index news. Not single-company news, not opinion, not explainers or "
    "'stocks to buy' lists. Answer with the numbers separated by commas, or "
    "NONE.\n\n")


def log(*a):
    # the log of a public repository is public: never the topic, never the
    # list of shares - only counts
    print(*a, flush=True)


def fetch(url, timeout=15):
    req = urllib.request.Request(url, headers={"User-Agent": UA,
                                               "Accept": "*/*"})
    return urllib.request.urlopen(req, timeout=timeout).read()


def age_hours(pub):
    try:
        d = email.utils.parsedate_to_datetime(pub)
        if d.tzinfo is None:
            d = d.replace(tzinfo=datetime.timezone.utc)
        return (datetime.datetime.now(datetime.timezone.utc) - d) \
            .total_seconds() / 3600
    except Exception:
        return None


def clean(t):
    t = html.unescape(re.sub(r"<[^>]+>", " ", t or ""))
    return re.sub(r"\s+", " ", t).strip()


def key(t, size=120):
    t = re.sub(r"[^a-z0-9 ]", " ", t.lower())
    return " ".join(w for w in t.split() if len(w) > 2)[:size]


def rss(url, source):
    out = []
    root = ET.fromstring(fetch(url))
    for it in root.iter("item"):
        t = clean(it.findtext("title"))
        link = (it.findtext("link") or "").strip()
        src = source
        s = it.find("source")
        if s is not None and s.text:
            src = s.text.strip()
            t = re.sub(r"\s+-\s+" + re.escape(src) + r"$", "", t)
        age = age_hours(it.findtext("pubDate") or "")
        if t and link.startswith("http"):
            out.append({"title": t, "link": link, "source": src, "age": age})
    return out


def google(q):
    url = ("https://news.google.com/rss/search?q=" +
           urllib.parse.quote(q + " when:1d") + "&hl=en-IN&gl=IN&ceid=IN:en")
    return rss(url, "Google News")


def matcher(groups):
    """[(SYMBOL, compiled pattern)] for each share's names."""
    out = []
    for g in groups:
        if not g:
            continue
        sym, terms = str(g[0]).upper(), [str(x) for x in g[1:] if x] or [g[0]]
        pats = []
        for t in terms:
            body = re.escape(t).replace(r"\ ", r"[\s.]+")
            # a ticker-like word (all capitals, short) must match exactly
            flags = 0 if (t.isupper() and len(t) <= 4) else re.I
            pats.append(re.compile(r"(?<![A-Za-z])" + body + r"(?![A-Za-z])", flags))
        out.append((sym, pats))
    return out


MARKET_WORD = re.compile(
    r"\b(shares?|stocks?|ltd|limited|q[1-4]|results?|profit|revenue|target|"
    r"buy|sell|dividend|bonus|split|order|stake|rating|nse|bse|sensex|nifty|"
    r"rally|slump|surge|jump|falls?|rises?|gains?|drops?)\b", re.I)


def which(title, groups):
    out = []
    for s, pats in groups:
        for p in pats:
            m = p.search(title)
            if not m:
                continue
            # a short capital-letter name (ITC, SBI) is also an everyday
            # abbreviation (ITC = input tax credit): it counts only in a
            # headline that is about shares
            if len(m.group(0)) <= 4 and m.group(0).isupper() and \
                    not MARKET_WORD.search(title):
                continue
            out.append(s)
            break
    return out


def digest_body(rows):
    """Headlines in one notification, newest last, kept under ntfy's 4 KB
    (beyond that it would arrive as a file)."""
    lines, size = [], 0
    for it, lab, _p in reversed(rows):
        ln = "%s: %s (%s)" % (lab, it["title"], it["source"])
        if size + len(ln.encode("utf-8")) + 2 > 3800:
            break
        lines.insert(0, ln)
        size += len(ln.encode("utf-8")) + 2
    return "\n\n".join(lines)


def ntfy_poll(topic):
    """What this topic already carries (ntfy keeps 12 hours)."""
    try:
        raw = fetch("%s/%s/json?poll=1&since=12h" % (NTFY, topic), 20)
    except Exception as e:
        log("ntfy poll failed:", type(e).__name__)
        return None
    msgs = []
    for ln in raw.decode("utf-8", "replace").splitlines():
        try:
            m = json.loads(ln)
        except ValueError:
            continue
        if m.get("event") == "message":
            msgs.append(m)
    return msgs


def ntfy_send(topic, title, body, click=None, priority=3, tags="newspaper"):
    hdr = {"Title": title.encode("utf-8").decode("latin-1", "ignore")[:250],
           "Priority": str(priority), "Tags": tags}
    if click:
        hdr["Click"] = click
    req = urllib.request.Request("%s/%s" % (NTFY, topic),
                                 data=body.encode("utf-8"), headers=hdr)
    try:
        urllib.request.urlopen(req, timeout=15).read()
        return True
    except Exception as e:
        log("send failed:", type(e).__name__)
        return False


def ask_gemini(key_, prompt):
    for model in ("gemini-flash-latest", "gemini-2.5-flash",
                  "gemini-flash-lite-latest"):
        url = ("https://generativelanguage.googleapis.com/v1beta/models/%s:"
               "generateContent?key=%s" % (model, urllib.parse.quote(key_)))
        body = json.dumps({"contents": [{"parts": [{"text": prompt}]}]})
        req = urllib.request.Request(url, data=body.encode(), headers={
            "Content-Type": "application/json"})
        try:
            d = json.loads(urllib.request.urlopen(req, timeout=40).read())
            return d["candidates"][0]["content"]["parts"][0]["text"]
        except Exception:
            continue
    return ""


def main():
    topic = os.environ.get("NTFY_TOPIC", "").strip()
    if not topic:
        log("NTFY_TOPIC is not set")
        return 1
    try:
        cfg = json.loads(os.environ.get("WATCH") or "{}")
    except ValueError:
        cfg = {}
    held = matcher(cfg.get("held") or [])
    watch = matcher(cfg.get("watch") or [])
    quiet = cfg.get("quiet") or [22, 7]
    now = datetime.datetime.now(IST)
    h = now.hour
    night = (h >= quiet[0] or h < quiet[1]) if quiet[0] > quiet[1] \
        else quiet[0] <= h < quiet[1]
    if night:
        log("quiet hours - nothing sent")
        return 0
    sent = ntfy_poll(topic)
    if sent is None:
        return 0                         # better nothing than repeats
    # the first run after the night (however late GitHub starts it): nothing
    # sent yet since the quiet hours ended - the night's news is gathered
    wake = now.replace(hour=quiet[1], minute=0, second=0, microsecond=0)
    morning = not any((m.get("time") or 0) >= wake.timestamp() for m in sent)
    max_age = ((24 - quiet[0] + quiet[1]) % 24 + 1) if morning else MAX_AGE_H
    done = set(key(m.get("message", "")) for m in sent)
    done |= set(key(m.get("title", "")) for m in sent)
    # the headlines inside a digest ("Overnight", "More") count as sent too
    digests = " ".join(key(m.get("message", ""), 10 ** 6) for m in sent
                       if re.match(r"(Overnight|More)\b", m.get("title") or ""))
    budget = DAY_CAP - len(sent)
    if budget <= 0:
        log("daily cap reached")
        return 0

    items, seen = [], set()
    for src, url in FEEDS:
        try:
            got = rss(url, src)
        except Exception as e:
            log("feed failed:", src, type(e).__name__)
            continue
        for it in got:
            k = key(it["title"])
            if k and k not in seen:
                seen.add(k)
                items.append(it)
    # the whole market, through Google News too (a news site may refuse
    # GitHub's computers; Google News does not)
    for q in ("Sensex Nifty", "RBI OR SEBI OR \"stock market\" India"):
        try:
            for it in google(q):
                k = key(it["title"])
                if k and k not in seen:
                    seen.add(k)
                    items.append(it)
        except Exception:
            pass
    # the held shares by name, a few names to a search
    names = [g for g in (cfg.get("held") or []) if g]
    for i in range(0, len(names), 6):
        # the company's full name searches best ("Ashok Leyland", not
        # ASHOKLEY)
        q = " OR ".join('"%s"' % next((x for x in g[1:] if " " in str(x)),
                                      g[1] if len(g) > 1 else g[0])
                        for g in names[i:i + 6])
        try:
            for it in google(q):
                k = key(it["title"])
                if k and k not in seen:
                    seen.add(k)
                    items.append(it)
        except Exception:
            pass
        time.sleep(0.5)
    fresh = [it for it in items if it["age"] is not None and
             0 <= it["age"] <= max_age and key(it["title"]) not in done and
             not (digests and key(it["title"]) in digests)]
    log("headlines read: %d, fresh: %d" % (len(items), len(fresh)))

    mine, others = [], []
    for it in fresh:
        hs, ws = which(it["title"], held), which(it["title"], watch)
        if hs:
            mine.append((it, hs, 4))
        elif ws:
            mine.append((it, ws, 3))
        else:
            others.append(it)
    market = []
    gk = os.environ.get("GEMINI_KEY", "").strip()
    if gk and others:
        others = others[:60]
        ans = ask_gemini(gk, MARKET_STYLE + "\n".join(
            "%d. %s" % (n + 1, it["title"]) for n, it in enumerate(others)))
        for n in re.findall(r"\d+", ans or ""):
            n = int(n)
            if 1 <= n <= len(others) and others[n - 1] not in market:
                market.append(others[n - 1])
        market = market[:4]

    out = [(it, ", ".join(syms[:3]), pr) for it, syms, pr in mine] + \
          [(it, "MARKET", 3) for it in market]
    out.sort(key=lambda x: x[0]["age"], reverse=True)      # oldest first
    if morning and len(out) > 6:
        # the night's news in one notification, then normal again
        ntfy_send(topic, "Overnight: %d headlines" % len(out), digest_body(out))
        log("overnight digest sent: %d" % len(out))
        return 0
    room = min(PER_RUN, max(0, budget))
    rest = []
    if len(out) > room and room >= 2:
        # the newest one by one; the others together in one notification
        rest, out = out[:len(out) - room + 1], out[len(out) - room + 1:]
    n = 0
    if rest:
        if ntfy_send(topic, "More: %d headlines" % len(rest), digest_body(rest)):
            n += 1
        time.sleep(1.2)
    for it, lab, pr in out[:room]:
        if ntfy_send(topic, "%s · %s" % (lab, it["source"]), it["title"],
                     click=it["link"], priority=pr):
            n += 1
        time.sleep(1.2)
    log("sent: %d (%d in one 'More')" % (n, len(rest)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
