"""SMART - what the phone reads while the laptop is off (20.15).

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
    return syms, fno


def main():
    now = datetime.datetime.now(IST)
    syms, fno = watch()
    out = {"at": now.strftime("%d %b %H:%M"), "ts": int(time.time()),
           "src": "GitHub", "ipo": [], "corp": [], "ban": [], "fno": {}}
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
    print("snapshot: %d IPOs, %d corporate actions, %d F&O names" % (
        len(out["ipo"]), len(out["corp"]), len(out["fno"])))


if __name__ == "__main__":
    main()
