"""Capital Compass - what the phone reads while the laptop is off (20.15).

Runs on GitHub's free computers after each look for news (every 15 minutes
in the day, every half hour at night). It uses the dashboard's own program
(cas_server.py, copied here beside it) to read, from public sources only:
the IPOs open and coming (with the grey-market premium and subscription), the
corporate actions of the shares he follows, the F&O ban list, and the F&O
figures (futures, option chain round the money, India VIX) of NIFTY, BANK
NIFTY and the names in his F&O list. They are written to snapshot.json in
this (public) repository, which the phone reads when the laptop does not
answer.

Nothing about him is in it: no name, e-mail, phone number, address, account
or broker number, holding size, price paid or profit - only market figures
and the names of the shares he follows (which he agreed may be public).
"""
import datetime
import json
import os
import sys
import time
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import cas_server as C  # noqa: E402  (the dashboard's own fetchers)

IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))


def watch():
    try:
        w = json.loads(os.environ.get("WATCH") or "{}")
    except ValueError:
        w = {}
    syms = [g[0] for g in (w.get("held") or []) + (w.get("watch") or [])
            if g]
    fno = [s for s in (w.get("fno") or []) if isinstance(s, str)]
    # 20.17: every share of his table and Out of list, for their prices
    every = list(dict.fromkeys(syms + [s for s in (w.get("all") or [])
                                       if isinstance(s, str)]))
    return syms, fno, every


UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/605.1.15"
YAHOO_IDX = {"^NSEI": "NIFTY 50", "^BSESN": "SENSEX", "^INDIAVIX": "INDIA VIX",
             "^NSEBANK": "BANK NIFTY"}


def yahoo_spark(symbols):
    """20.17: last price and today's % for up to 20 Yahoo symbols a request
    (no sign-in needed; GitHub's computers already read Yahoo for news)."""
    out = {}
    for i in range(0, len(symbols), 20):
        part = symbols[i:i + 20]
        url = ("https://query1.finance.yahoo.com/v7/finance/spark?symbols="
               + ",".join(urllib.parse.quote(s, safe="^") for s in part)
               + "&range=1d&interval=1d")
        try:
            raw = urllib.request.urlopen(urllib.request.Request(
                url, headers={"User-Agent": UA}), timeout=20).read()
            res = (json.loads(raw.decode("utf-8", "replace")).get("spark")
                   or {}).get("result") or []
        except Exception as e:
            print("yahoo:", type(e).__name__)
            res = []
        for r in res:
            m = ((r.get("response") or [{}])[0] or {}).get("meta") or {}
            p, c = m.get("regularMarketPrice"), m.get(
                "regularMarketChangePercent")
            if p:
                out[str(r.get("symbol") or "")] = {
                    "ltp": round(float(p), 2),
                    "pchange": None if c is None else round(float(c), 2)}
        time.sleep(0.6)
    return out


def news_sent():
    """20.17: the news the phone was sent (the news program's own record of
    the last 36 hours), for the phone's news tab while the laptop is off."""
    try:
        s = json.load(open("state.json"))
    except Exception:
        return []
    out = []
    for x in sorted(s.get("sent") or [], key=lambda x: -x.get("at", 0))[:80]:
        if x.get("title") and x.get("link"):
            out.append({"title": x["title"][:300], "link": x["link"],
                        "label": x.get("sym") or "", "at": int(x.get("at", 0))})
    return out


def main():
    now = datetime.datetime.now(IST)
    syms, fno, every = watch()
    out = {"at": now.strftime("%d %b %H:%M"), "ts": int(time.time()),
           "src": "GitHub", "ipo": [], "corp": [], "ban": [], "fno": {},
           "idx": [], "px": {}, "news": news_sent()}
    # 20.17: the market strip and the prices of his shares
    try:
        y = yahoo_spark(list(YAHOO_IDX) + [s + ".NS" for s in every[:300]])
        for ys, name in YAHOO_IDX.items():
            if ys in y:
                out["idx"].append({"name": name, "last": y[ys]["ltp"],
                                   "pct": y[ys]["pchange"]})
        for s in every:
            if s + ".NS" in y:
                out["px"][s] = y[s + ".NS"]
    except Exception as e:
        out["pxError"] = str(e)[:100]
    try:
        have = {r["name"] for r in out["idx"]}
        world = C.cnbc_batch([c[0] for c in C.INDEX_CATALOG
                              if c[0] not in have])
        for c in C.INDEX_CATALOG:
            r = world.get(c[0])
            if r and r.get("last") is not None:
                out["idx"].append({"name": c[0], "pct": r.get("pct"),
                                   "last": round(r["last"] * C.INDEX_SCALE.get(
                                       c[0], 1), 4)})
    except Exception as e:
        out["idxError"] = str(e)[:100]
    try:
        for r in (C.fetch_ipos().get("rows") or [])[:30]:
            out["ipo"].append({k: r.get(k, "") for k in (
                "name", "status", "dates", "band", "gmpText", "gainPct",
                "board", "subscription", "listDate", "minAmount")})
    except Exception as e:
        out["ipoError"] = str(e)[:100]
    try:
        mine = set(syms)
        for r in C.fetch_corp_actions():
            if r.get("symbol") in mine:
                out["corp"].append({
                    "symbol": r["symbol"], "kind": r.get("kind"),
                    "purpose": r.get("purpose", "")[:160],
                    "value": r.get("value") or "", "date": r.get("exDate") or "",
                    "daysTo": r.get("daysTo"), "expected": ""})
    except Exception as e:
        out["corpError"] = str(e)[:100]
    try:
        b = C.fno_ban()
        out["ban"] = b.get("names") or []
        out["banDay"] = b.get("day") or ""
    except Exception:
        pass
    # F&O: in the day only (the figures do not move at night)
    if now.weekday() < 5 and 9 <= now.hour < 16:
        for s in (["NIFTY", "BANKNIFTY"] + fno)[:10]:
            try:
                out["fno"][s] = C.phone_fno(s)
            except Exception as e:
                out["fno"][s] = {"ok": False, "error": str(e)[:100],
                                 "symbol": s}
            time.sleep(1)
    else:
        try:
            old = json.load(open("snapshot.json")).get("fno") or {}
            out["fno"] = old
        except Exception:
            pass
    with open("snapshot.json", "w") as f:
        json.dump(out, f, separators=(",", ":"))
    print("snapshot: %d IPOs, %d corporate actions, %d F&O names, "
          "%d indices, %d share prices, %d news" % (
              len(out["ipo"]), len(out["corp"]), len(out["fno"]),
              len(out["idx"]), len(out["px"]), len(out["news"])))


if __name__ == "__main__":
    main()
