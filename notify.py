#!/usr/bin/env python3
"""CAS news to the phone - runs in the cloud (GitHub Actions), not on the Mac.

20.11: the same five kinds of news as the dashboard, in his order of
importance:
  1 MARKET   what is moving the whole Indian market today
  2 KOLKATA  Kolkata and West Bengal (Bengali papers translated to English)
  3 <SHARE>  news that is really about a share he holds or is working on
  4 POLICY   Indian economic and market policy (RBI, SEBI, government, tax)
  5 GLOBAL   big world market and economy news
Each one is its own notification - never bundled - sent as soon as it is
found. One run stays on for about 25 minutes and looks every 5 minutes, and
GitHub starts a new run every 15, so the news arrives steadily instead of in
bursts when GitHub's timer is late. Gemini checks every candidate headline
("is this really about the listed company?", "does this move the whole
market?") and translates the Bengali ones; without Gemini, stricter word
rules are used. Notifications use normal priority, so the phone treats them
like any other app's (its own sound, silent switch and Focus settings).

Settings come from the repository's secrets (set by the dashboard):
  NTFY_TOPIC  the ntfy name the phone is subscribed to
  WATCH       JSON {"held": [[SYMBOL, term, ...], ...], "watch": [...],
                    "names": {SYMBOL: company name}, "quiet": [22, 7],
                    "alerts": [[SYMBOL, price, until, price then, note]]}
  GEMINI_KEY  the Gemini key (relevance checks and translation)
What was sent and what was already judged is kept between runs in a small
file (state.json, through GitHub's cache), and what ntfy itself remembers.
Python 3 standard library only.
"""
import datetime
import email.utils
import html
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 " \
     "(KHTML, like Gecko) Chrome/120 Safari/537.36"
NTFY = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
RUN_MINUTES = float(os.environ.get("RUN_MINUTES") or 25)
CHECK_EVERY = float(os.environ.get("CHECK_EVERY") or 300)   # seconds
STATE_FILE = os.environ.get("STATE_FILE") or "state.json"
MAX_AGE_H = 3            # a headline older than this is not news any more
ORDER = {"MARKET": 0, "KOLKATA": 1, "SHARE": 2, "POLICY": 3, "GLOBAL": 4}

INDIAN_FEEDS = [
    ("Economic Times", "https://economictimes.indiatimes.com/markets/stocks/rssfeeds/2146842.cms"),
    ("ET Markets", "https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms"),
    ("Moneycontrol", "https://www.moneycontrol.com/rss/marketreports.xml"),
    ("Moneycontrol", "https://www.moneycontrol.com/rss/business.xml"),
    ("Moneycontrol", "https://www.moneycontrol.com/rss/latestnews.xml"),
    ("Moneycontrol", "https://www.moneycontrol.com/rss/economy.xml"),
    ("Business Standard", "https://www.business-standard.com/rss/markets-106.rss"),
    ("BusinessLine", "https://www.thehindubusinessline.com/markets/feeder/default.rss"),
    ("Zee Business", "https://www.zeebiz.com/rss/india-markets.xml"),
    ("Zee Business", "https://www.zeebiz.com/rss/india-economy-infra.xml"),
    ("Mint", "https://www.livemint.com/rss/markets"),
    ("Mint", "https://www.livemint.com/rss/economy"),
    ("Business Today", "https://www.businesstoday.in/rssfeeds/?id=home"),
    ("NDTV Profit", "https://feeds.feedburner.com/ndtvprofit-latest"),
]
GLOBAL_FEEDS = [
    ("CNBC", "https://www.cnbc.com/id/100727362/device/rss/rss.html"),
    ("CNBC", "https://www.cnbc.com/id/20910258/device/rss/rss.html"),
    ("CNBC", "https://www.cnbc.com/id/19836768/device/rss/rss.html"),
    ("Investing.com", "https://www.investing.com/rss/news_25.rss"),
    ("MarketWatch", "https://feeds.content.dowjones.io/public/rss/mw_topstories"),
]


def _gn_site(site, words, lang):
    hl = {"bn": ("bn", "IN:bn")}.get(lang, ("en-IN", "IN:en"))
    q = "site:%s%s when:1d" % (site, (" " + words) if words else "")
    return ("https://news.google.com/rss/search?q=" + urllib.parse.quote(q) +
            "&hl=%s&gl=IN&ceid=%s" % hl)


# Kolkata and West Bengal: the city pages of the English papers, and the
# Bengali papers through Google News (their own feeds often refuse GitHub)
LOCAL_FEEDS = [
    ("TOI Kolkata", "https://timesofindia.indiatimes.com/rssfeeds/-2128830821.cms"),
    ("HT Kolkata", "https://www.hindustantimes.com/feeds/rss/cities/kolkata-news/rssfeed.xml"),
    ("Indian Express Kolkata", "https://indianexpress.com/section/cities/kolkata/feed/"),
    ("Millennium Post", "https://www.millenniumpost.in/rss/kolkata.xml"),
    ("The Statesman", "https://www.thestatesman.com/category/bengal/feed"),
    ("Telegraph India", _gn_site("telegraphindia.com", "Kolkata OR Bengal", "en")),
    ("Anandabazar", _gn_site("anandabazar.com", "", "bn")),
    ("Bartaman", _gn_site("bartamanpatrika.com", "", "bn")),
    ("Ei Samay", _gn_site("eisamay.com", "", "bn")),
    ("Sangbad Pratidin", _gn_site("sangbadpratidin.in", "", "bn")),
    ("Aajkaal", _gn_site("aajkaal.in", "", "bn")),
]
LOCAL_SOURCES = {s for s, _u in LOCAL_FEEDS}

# words that make a headline worth a look for the market, policy and global
# kinds (the Gemini check then decides); used alone when there is no Gemini
MARKET_WORDS = re.compile(
    r"\b(sensex|nifty|market|stocks?|shares|rbi|repo|sebi|fed|federal reserve|"
    r"fomc|inflation|cpi|gdp|budget|gst|tax|tariff|trade deal|crude|brent|oil|"
    r"opec|rupee|dollar|fii|fpi|foreign investors|bond yield|yields|rate cut|"
    r"rate hike|policy|ministry|government|cabinet|subsidy|scheme|ban|war|"
    r"ceasefire|sanction|china|recession|gold|wall street|dow|nasdaq|"
    r"s&p|ipo|monsoon|election)\b", re.I)
BIG_WORDS = re.compile(
    r"\b(repo rate|rbi policy|monetary policy|rate cut|rate hike|federal "
    r"reserve|fomc|union budget|gst council|sebi board|tariffs?|trade war|"
    r"sanctions|opec|hormuz|declares war|invasion|ceasefire|airstrike|"
    r"election results?|exit poll|fii|foreign investors|rupee (hits|falls|"
    r"record)|crude (surges|jumps|crashes)|sensex (crashes|tanks|plunges|"
    r"soars|surges)|nifty (crashes|tanks|plunges|soars|surges))\b", re.I)
GLOBAL_WORDS = re.compile(
    r"\b(fed|federal reserve|wall street|dow|nasdaq|s&p 500|treasury|ecb|boj|"
    r"opec|oil|china|tariffs?|trade war|recession|sanctions)\b", re.I)
POLICY_WORDS = re.compile(
    r"\b(rbi|sebi|ministry|minister|gst|income tax|budget|cabinet|policy|"
    r"regulator|circular|notification|scheme)\b", re.I)
SHARE_CONTEXT = re.compile(
    r"\b(shares?|stocks?|ltd|limited|q[1-4]|results?|profit|revenue|target|"
    r"buy|sell|dividend|bonus|split|orders?|stake|rating|nse|bse|sensex|nifty|"
    r"rally|slump|surges?|jumps?|falls?|rises?|gains?|drops?|crore|deal|"
    r"acquires?|ipo|ceo|md|board|sebi|plant|contract)\b", re.I)

STYLE = (
    "You choose news notifications for an Indian share trader in Kolkata. "
    "Today is %s. For each numbered headline below decide its kind, using "
    "only these:\n"
    "MARKET - only the few stories (four or five a day) that move the WHOLE "
    "Indian stock market or a whole sector today: RBI or US Fed decisions, "
    "a sharp move in crude, the rupee or global markets, heavy FII selling "
    "or buying, war or a crisis, a big government decision, a sharp Sensex/"
    "Nifty move with its cause. Not single companies, not regional unrest, "
    "not opinion, not previews or 'trade setup' and support levels, not "
    "'stocks to buy' lists, not weekly market-value round-ups, not routine "
    "market-close reports.\n"
    "KOLKATA - MAJOR news about Kolkata or West Bengal only: big government "
    "decisions, large investments and jobs, major infrastructure, metro and "
    "transport disruptions, power or water crises, severe weather warnings, "
    "elections and important politics, serious law-and-order events. Not "
    "routine politics or speeches, not gossip, film or TV, not single "
    "crimes or accidents.\n"
    "SHARE - MAJOR news mainly about one of the listed companies below: "
    "quarterly results, large orders or contracts, mergers, acquisitions, "
    "stake sales, dividends, bonus or split, rating or target changes by "
    "known brokers, a change of chairman, CEO or MD, regulator or tax "
    "action, a sharp share-price move. Not routine filings, small "
    "appointments, AGM notices, generic articles, or lists where it is one "
    "of many; not another company or a person with a similar name.\n"
    "POLICY - MAJOR Indian economic or market policy decisions: RBI, SEBI, "
    "ministries, tax, GST, budget, trade and industry policy actually "
    "decided or announced - not speeches, opinions or consultations.\n"
    "GLOBAL - MAJOR world market or economy news only: Fed decisions, big "
    "moves in US or Chinese markets, oil shocks, trade wars, major "
    "geopolitical events.\n"
    "NONE - anything else: minor items, routine filings (AGM notices, "
    "postal ballots, board-meeting dates, junior appointments), and anything "
    "OLD - about an earlier quarter, year or month than the latest (results "
    "for a year or quarter already reported long ago, a meeting date that has "
    "passed).\n"
    "When several headlines tell the same story, mark only the best one and "
    "the rest NONE. Be strict: when in doubt, NONE. Answer one line per headline that is "
    "not NONE, exactly: number|KIND|SYMBOL|headline in English. SYMBOL only "
    "for SHARE, else -. Translate a Bengali or Hindi headline into plain "
    "English; keep an English one as it is. No other text. If all are NONE "
    "answer NONE.\n\nListed companies (SYMBOL = name):\n")


# share news found through Google News comes only from these (Google also
# carries machine-written pages that re-date old filings as new)
TRUSTED = re.compile(
    r"economic times|\bet\b|etmarkets|livemint|\bmint\b|moneycontrol|business "
    r"standard|businessline|hindu|financial express|ndtv|cnbc|business today|"
    r"zee business|reuters|bloomberg|times of india|hindustan times|indian "
    r"express|deccan herald|businessworld|fortune india|forbes india|et now|"
    r"outlook business|telegraph|the statesman|millennium post|news18|"
    r"economic times|financial times|press trust|\bpti\b|ians|india today|"
    r"the print|theprint|scroll|firstpost|wion|abp|anandabazar|ei samay|"
    r"bartaman|sangbad pratidin|aajkaal|investing\.com|marketwatch|"
    r"\bbs\b|upstox|groww|angel one|5paisa|goodreturns|trade brains", re.I)


def log(*a):
    # the log of a public repository is public: never the topic, never the
    # list of shares, never a headline - only counts
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
    t = re.sub(r"[^a-z0-9 ]", " ", (t or "").lower())
    return " ".join(w for w in t.split() if len(w) > 2)[:size]


def words(t):
    return set(key(t, 10 ** 6).split())


def similar(a, b):
    """The same story from two papers ("Sensex falls 800 pts as FIIs sell"
    / "FIIs sell, Sensex down 800 points")."""
    wa, wb = words(a), words(b)
    if len(wa) < 4 or len(wb) < 4:
        return False
    return len(wa & wb) / float(min(len(wa), len(wb))) >= 0.7


def is_indic(t):
    return any(0x0900 <= ord(c) <= 0x0DFF for c in (t or ""))


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
            out.append({"title": t, "link": link, "source": src, "age": age,
                        "from": source})
    return out


def google(q):
    url = ("https://news.google.com/rss/search?q=" +
           urllib.parse.quote(q + " when:1d") + "&hl=en-IN&gl=IN&ceid=IN:en")
    return rss(url, "Google News")


def matcher(groups):
    """[(SYMBOL, [compiled patterns])] for each share's names."""
    out = []
    for g in groups:
        if not g:
            continue
        sym, terms = str(g[0]).upper(), [str(x) for x in g[1:] if x] or [g[0]]
        pats = []
        for t in terms:
            body = re.escape(t).replace(r"\ ", r"[\s.]+")
            flags = 0 if (t.isupper() and len(t) <= 4) else re.I
            pats.append(re.compile(r"(?<![A-Za-z])" + body + r"(?![A-Za-z])",
                                   flags))
        out.append((sym, pats))
    return out


def which(title, groups, strict=True):
    """Shares a headline names - strictly, a name only counts in a headline
    about business (no "Trent" in a football story)."""
    if strict and not SHARE_CONTEXT.search(title):
        return []
    out = []
    for s, pats in groups:
        if any(p.search(title) for p in pats):
            out.append(s)
    return out


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


def ntfy_send(topic, title, body, click=None, tags="newspaper", prio="3"):
    # priority 3 = the phone's own settings decide sound and banner, like
    # any other app (silent switch, Focus, Notification settings)
    hdr = {"Title": title.encode("utf-8").decode("latin-1", "ignore")[:250],
           "Priority": prio, "Tags": tags}
    if click:
        hdr["Click"] = click
    req = urllib.request.Request("%s/%s" % (NTFY, topic),
                                 data=body.encode("utf-8"), headers=hdr)
    for _ in range(3):
        try:
            urllib.request.urlopen(req, timeout=15).read()
            return True
        except urllib.error.HTTPError as e:
            if e.code != 429:
                log("send failed:", e.code)
                return False
            time.sleep(10)
        except Exception as e:
            log("send failed:", type(e).__name__)
            time.sleep(3)
    return False


def ask_gemini(key_, prompt):
    # the lighter models first: they have the larger free daily allowance
    models = [m for m in (os.environ.get("GEMINI_MODELS") or "").split(",")
              if m.strip()] or ["gemini-flash-lite-latest",
                                "gemini-2.5-flash-lite", "gemini-flash-latest",
                                "gemini-2.5-flash"]
    for model in models:
        url = ("https://generativelanguage.googleapis.com/v1beta/models/%s:"
               "generateContent?key=%s" % (model, urllib.parse.quote(key_)))
        body = json.dumps({"contents": [{"parts": [{"text": prompt}]}],
                           "generationConfig": {"temperature": 0}})
        req = urllib.request.Request(url, data=body.encode(), headers={
            "Content-Type": "application/json"})
        try:
            d = json.loads(urllib.request.urlopen(req, timeout=60).read())
            return d["candidates"][0]["content"]["parts"][0]["text"]
        except Exception:
            continue
    return None


def yahoo_minutes(sym):
    """Today's one-minute bars of an NSE share: [(time, high, low, close)]."""
    url = ("https://query2.finance.yahoo.com/v8/finance/chart/%s.NS"
           "?range=1d&interval=1m" % urllib.parse.quote(sym))
    r = json.loads(fetch(url, 15))["chart"]["result"][0]
    q = (r.get("indicators") or {}).get("quote") or [{}]
    q = q[0]
    out = []
    for t, hi, lo, c in zip(r.get("timestamp") or [], q.get("high") or [],
                            q.get("low") or [], q.get("close") or []):
        if c is not None and hi is not None and lo is not None:
            out.append((int(t), float(hi), float(lo), float(c)))
    return out


def money(v):
    return "{:,.2f}".format(float(v))


def load_state():
    try:
        with open(STATE_FILE) as f:
            s = json.load(f)
        if isinstance(s, dict):
            return s
    except Exception:
        pass
    return {}


def save_state(s):
    try:
        cut = time.time() - 36 * 3600
        s["judged"] = {k: v for k, v in (s.get("judged") or {}).items()
                       if v.get("at", 0) > cut}
        s["sent"] = [x for x in (s.get("sent") or []) if x.get("at", 0) > cut]
        with open(STATE_FILE + ".tmp", "w") as f:
            json.dump(s, f)
        os.replace(STATE_FILE + ".tmp", STATE_FILE)
    except Exception as e:
        log("state not saved:", type(e).__name__)


def night_now(quiet):
    h = datetime.datetime.now(IST).hour
    return (h >= quiet[0] or h < quiet[1]) if quiet[0] > quiet[1] \
        else quiet[0] <= h < quiet[1]


class Bot:
    def __init__(self, topic, cfg, gemini):
        self.topic, self.cfg, self.gemini = topic, cfg, gemini
        self.held = matcher(cfg.get("held") or [])
        self.watch = matcher(cfg.get("watch") or [])
        self.names = dict(cfg.get("names") or {})
        for g in (cfg.get("held") or []) + (cfg.get("watch") or []):
            if g and g[0] not in self.names:
                self.names[g[0]] = next((x for x in g[1:] if " " in str(x)),
                                        g[1] if len(g) > 1 else g[0])
        self.quiet = cfg.get("quiet") or [22, 7]
        self.state = load_state()
        self.state.setdefault("judged", {})
        self.state.setdefault("sent", [])

    # ---- price alerts (20.12): while the Mac is off, the alerts he set in
    # the dashboard are checked here, from Yahoo's minute bars (a touch is
    # any minute whose high and low span the price). The Mac sends the same
    # alert first when it is on - the title is looked for in what ntfy
    # already carries, so it never comes twice.
    def check_alerts(self, msgs):
        alerts = self.cfg.get("alerts") or []
        if not alerts:
            return 0
        now = datetime.datetime.now(IST)
        mins = now.hour * 60 + now.minute
        if now.weekday() >= 5 or not (9 * 60 + 15 <= mins <= 15 * 60 + 35):
            return 0
        st = self.state.setdefault("alerts", {})
        today = now.date().isoformat()
        sent = 0
        for a in alerts:
            try:
                sym, price, until = str(a[0]), float(a[1]), str(a[2])
                note = str(a[4]) if len(a) > 4 and a[4] else ""
            except (IndexError, TypeError, ValueError):
                continue
            if until < today:
                continue
            k = "%s|%s" % (sym, price)
            one = st.setdefault(k, {"seen": 0, "hitAt": 0})
            try:
                bars = yahoo_minutes(sym)
            except Exception as e:
                log("alert price failed:", sym, type(e).__name__)
                continue
            since = max(one.get("seen") or 0, time.time() - 900)
            fresh = [b for b in bars if b[0] > since]
            if not fresh:
                continue
            one["seen"] = fresh[-1][0]
            hit = [b for b in fresh if b[2] <= price <= b[1]]
            if not hit or time.time() - (one.get("hitAt") or 0) < 1800:
                continue
            one["hitAt"] = time.time()
            if any("PRICE ALERT" in (m.get("title") or "") and sym in
                   (m.get("title") or "") and money(price) in
                   (m.get("title") or "") and
                   time.time() - (m.get("time") or 0) < 3600
                   for m in msgs or []):
                continue                 # the Mac has told him already
            last = fresh[-1][3]
            up = last >= price
            t = datetime.datetime.fromtimestamp(hit[0][0], IST)
            if ntfy_send(self.topic, "PRICE ALERT - %s Rs %s" % (
                    sym, money(price)),
                    "%s %s \u20b9%s at %s - now \u20b9%s.%s\n(checked from "
                    "GitHub while the Mac is off; until %s)" % (
                        sym, "rose to" if up else "fell to", money(price),
                        t.strftime("%H:%M"), money(last),
                        (" " + note) if note else "", until),
                    "https://www.nseindia.com/get-quotes/equity?symbol=" +
                    urllib.parse.quote(sym), tags="bell", prio="4"):
                sent += 1
        return sent

    # ---- what has gone already
    def remember_ntfy(self, msgs):
        have = {x.get("link") for x in self.state["sent"]}
        for m in msgs or []:
            link = m.get("click") or ""
            if link and link not in have:
                self.state["sent"].append({"link": link, "title": m.get(
                    "message", ""), "at": m.get("time") or time.time()})

    def already(self, it):
        if it["link"] in {x.get("link") for x in self.state["sent"]}:
            return True
        return any(similar(it["title"], x.get("title", "")) or (
            it.get("en") and similar(it["en"], x.get("title", "")))
            for x in self.state["sent"])

    # ---- reading
    def gather(self):
        items, seen = [], set()

        def add(got, kind):
            for it in got:
                k = it["link"]
                if k in seen:
                    continue
                seen.add(k)
                it["kind_hint"] = kind
                items.append(it)
        for kind, feeds in (("in", INDIAN_FEEDS), ("wo", GLOBAL_FEEDS),
                            ("local", LOCAL_FEEDS)):
            for src, url in feeds:
                try:
                    add(rss(url, src), kind)
                except Exception as e:
                    log("feed failed:", type(e).__name__)
        # the shares by name, a few names to a search - every 15 minutes
        # (Google turns away a computer that asks too often)
        if time.time() - self.state.get("googleAt", 0) < 14 * 60:
            return items
        self.state["googleAt"] = time.time()
        names = [self.names.get(g[0]) or g[0]
                 for g in (self.cfg.get("held") or []) if g]
        for i in range(0, len(names), 6):
            q = " OR ".join('"%s"' % n for n in names[i:i + 6])
            try:
                add(google(q), "share")
            except Exception:
                pass
            time.sleep(0.4)
        return items

    # ---- judging
    def candidates(self, items, max_age):
        out = []
        for it in items:
            if it["age"] is None or not 0 <= it["age"] <= max_age:
                continue
            if it["link"] in self.state["judged"] or self.already(it):
                continue
            if it["kind_hint"] == "share" and not TRUSTED.search(
                    it["source"] or ""):
                continue
            t = it["title"]
            # loosely here - Gemini decides; the word rules use strict
            hs = which(t, self.held, False) + which(t, self.watch, False)
            local = it["kind_hint"] == "local"
            if not (local or hs or MARKET_WORDS.search(t)):
                continue
            it["syms"] = hs
            out.append(it)
        return out

    def judge(self, cands):
        """[(item, KIND, SYMBOL, english title)] for the ones worth sending."""
        if not cands:
            return []
        if self.gemini:
            got = self.judge_gemini(cands)
            if got is not None:
                self.state.pop("gemFailSince", None)
                return got
            # Gemini busy for a moment: wait for it (the headlines are
            # looked at again in five minutes) rather than send what the
            # word rules let through; only after 45 minutes the rules step in
            since = self.state.setdefault("gemFailSince", time.time())
            if time.time() - since < 45 * 60:
                return []
        return self.judge_rules(cands)

    def judge_gemini(self, cands):
        syms = sorted({s for s in self.names})
        listing = "\n".join("%s = %s" % (s, self.names[s]) for s in syms)
        out = []
        for i in range(0, len(cands), 60):
            part = cands[i:i + 60]
            today = datetime.datetime.now(IST).strftime("%A %d %B %Y")
            ans = ask_gemini(self.gemini, (STYLE % today) + listing +
                             "\n\nHeadlines:\n" +
                             "\n".join("%d. %s" % (n + 1, it["title"])
                                       for n, it in enumerate(part)))
            if ans is None:
                log("gemini did not answer")
                return out or None
            now = time.time()
            for it in part:
                self.state["judged"][it["link"]] = {"at": now, "kind": "NONE"}
            for ln in ans.splitlines():
                m = re.match(r"\s*\**(\d+)[.)]?\s*\|\s*([A-Za-z]+)\s*\|(.*)", ln)
                if not m:
                    continue
                n, kind = int(m.group(1)), m.group(2).upper()
                rest = m.group(3).split("|")
                # "SYMBOL|headline", "-|headline", sometimes "-|-|headline"
                # - and a headline may itself hold a "|"
                sym = ""
                while len(rest) > 1 and (rest[0].strip() in ("", "-") or (
                        re.fullmatch(r"[A-Z0-9&-]{2,20}", rest[0].strip())
                        and not sym)):
                    tok = rest.pop(0).strip()
                    if tok not in ("", "-"):
                        sym = tok
                en = clean("|".join(rest)).strip(" -")
                if not 1 <= n <= len(part) or kind not in ORDER:
                    continue
                it = part[n - 1]
                if kind == "SHARE" and sym not in self.names:
                    continue
                if kind == "KOLKATA" and it["kind_hint"] != "local" and \
                        not re.search(r"kolkata|bengal|howrah|calcutta",
                                      it["title"] + " " + en, re.I):
                    continue
                self.state["judged"][it["link"]] = {"at": now, "kind": kind}
                out.append((it, kind, sym if kind == "SHARE" else "",
                            en or it["title"]))
        return out

    def judge_rules(self, cands):
        """Without Gemini: stricter word rules, and Bengali left out (it
        cannot be translated here)."""
        out, now = [], time.time()
        for it in cands:
            t = it["title"]
            kind, sym = None, ""
            if is_indic(t):
                kind = None
            elif it["kind_hint"] == "in" and BIG_WORDS.search(t):
                kind = "MARKET"
            elif it["kind_hint"] == "local" and it["from"] in (
                    "TOI Kolkata", "HT Kolkata", "Indian Express Kolkata",
                    "Millennium Post") and re.search(
                        r"kolkata|bengal|howrah|calcutta|mamata|suvendu", t,
                        re.I):
                kind = "KOLKATA"
            elif which(t, self.held) or which(t, self.watch):
                kind, sym = "SHARE", (which(t, self.held) or
                                      which(t, self.watch))[0]
            elif it["kind_hint"] == "in" and POLICY_WORDS.search(t):
                kind = "POLICY"
            elif it["kind_hint"] == "wo" and GLOBAL_WORDS.search(t):
                kind = "GLOBAL"
            self.state["judged"][it["link"]] = {"at": now,
                                                "kind": kind or "NONE"}
            if kind:
                out.append((it, kind, sym, t))
        return out

    # ---- sending
    def send(self, it, kind, sym, en):
        label = {"MARKET": "MARKET", "KOLKATA": "KOLKATA", "POLICY": "POLICY",
                 "GLOBAL": "GLOBAL"}.get(kind, sym or "SHARE")
        tags = {"MARKET": "chart_with_upwards_trend", "KOLKATA": "round_pushpin",
                "POLICY": "classical_building", "GLOBAL": "globe_with_meridians"
                }.get(kind, "newspaper")
        if ntfy_send(self.topic, "%s · %s" % (label, it["source"]), en,
                     click=it["link"], tags=tags):
            self.state["sent"].append({"link": it["link"], "title": en,
                                       "at": time.time()})
            return True
        return False

    def check(self, first):
        sent = ntfy_poll(self.topic)
        if sent is None:
            return 0                     # better nothing than repeats
        self.remember_ntfy(sent)
        now = datetime.datetime.now(IST)
        # the first look after the night gathers the night's news (each
        # still one notification of its own, spaced out)
        wake = now.replace(hour=self.quiet[1], minute=0, second=0,
                           microsecond=0)
        morning = first and not any(
            x.get("at", 0) >= wake.timestamp() for x in self.state["sent"])
        max_age = ((24 - self.quiet[0] + self.quiet[1]) % 24 + 1) \
            if morning else MAX_AGE_H
        items = self.gather()
        cands = self.candidates(items, max_age)
        picked = self.judge(cands)
        # chosen earlier but not sent before that run ended
        for p in self.state.get("pending") or []:
            try:
                it = p["item"]
                it["age"] = (time.time() - p["at"]) / 3600.0 + (p["age0"] or 0)
                if it["age"] <= max_age + 1:
                    picked.append((it, p["kind"], p["sym"], p["en"]))
            except (KeyError, TypeError):
                continue
        self.state["pending"] = []
        # one story once, even when three papers carry it
        out = []
        for p in sorted(picked, key=lambda x: (ORDER[x[1]], -(x[0]["age"] or 0))):
            if not any(similar(p[3], q[3]) for q in out) and \
                    not self.already(dict(p[0], en=p[3])):
                out.append(p)
        log("read %d, new %d, chosen %d" % (len(items), len(cands), len(out)))
        return out

    def run(self):
        start = time.time()
        deadline = start + RUN_MINUTES * 60
        first, total = True, 0
        while True:
            if night_now(self.quiet):
                log("quiet hours - nothing sent")
                break
            t0 = time.time()
            try:
                total += self.check_alerts(ntfy_poll(self.topic))
            except Exception as e:
                log("alerts failed:", type(e).__name__)
            queue = self.check(first) or []
            first = False
            save_state(self.state)
            if queue:
                # 20.11: sent as soon as found - together if several came
                # together, and the night's news all at once at 7 (each
                # still its own notification); a short pause only so that
                # ntfy keeps them in order
                gap = 2.0
                for n, (it, kind, sym, en) in enumerate(queue):
                    if time.time() > deadline + 120 or night_now(self.quiet):
                        # the rest wait for the next run
                        self.state["pending"] = [
                            {"item": x[0], "kind": x[1], "sym": x[2],
                             "en": x[3], "at": time.time(),
                             "age0": x[0].get("age") or 0}
                            for x in queue[n:]]
                        break
                    if self.send(it, kind, sym, en):
                        total += 1
                    save_state(self.state)
                    if n < len(queue) - 1:
                        time.sleep(gap)
            left = deadline - time.time()
            if left < 60:
                break
            time.sleep(max(5.0, min(left, CHECK_EVERY - (time.time() - t0))))
        save_state(self.state)
        log("sent: %d" % total)


def main():
    topic = os.environ.get("NTFY_TOPIC", "").strip()
    if not topic:
        log("NTFY_TOPIC is not set")
        return 1
    try:
        cfg = json.loads(os.environ.get("WATCH") or "{}")
    except ValueError:
        cfg = {}
    Bot(topic, cfg, os.environ.get("GEMINI_KEY", "").strip()).run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
