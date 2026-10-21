#!/usr/bin/env python3
"""Fetch intraday TAIEX + TX near-month 1-min series -> data/taiex.json, data/tx.json (stdlib only)."""
import json, urllib.request, datetime, os, sys
TZ = datetime.timezone(datetime.timedelta(hours=8))
UA = {"User-Agent": "Mozilla/5.0"}
# Default is repo-root data/ (this file lives in live/). OUT_DIR overrides, e.g. OUT_DIR=data.
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.environ.get("OUT_DIR") or os.path.join(_ROOT, "data")
TOPN = int(os.environ.get("STOCK_TOPN", "150"))  # intraday files for top-N stocks by prev-day turnover (+ watchlist)

def get(url, body=None):
    h = dict(UA)
    if body is not None:
        h["Content-Type"] = "application/json"; body = json.dumps(body).encode()
    with urllib.request.urlopen(urllib.request.Request(url, body, h), timeout=20) as r:
        return json.load(r)

def ts(day, hhmmss):  # day 'YYYYMMDD', hhmmss 'HH:MM:SS' or 'HHMMSS' -> epoch (Taipei)
    s = hhmmss.replace(":", "")
    return int(datetime.datetime.strptime(day + s, "%Y%m%d%H%M%S").replace(tzinfo=TZ).timestamp())

def taiex():
    try:  # TWSE official 5-sec index, downsampled to 1-min (last value per minute)
        d = get("https://www.twse.com.tw/rwd/zh/TAIEX/MI_5MINS_INDEX?response=json")
        if d.get("stat") != "OK": raise RuntimeError(d.get("stat"))
        day, m = d["date"], {}
        for row in d["data"]:
            t = ts(day, row[0]); m[t - t % 60] = float(row[1].replace(",", ""))
        prev = float(d["data"][0][1].replace(",", ""))  # 09:00:00 row = previous close
        idx5 = {r[0]: float(r[1].replace(",", "")) for r in d["data"]}
        vw = {}
        try:  # 均價線: weight each 5-sec index value by that interval's Δ成交金額 (MI_5MINS, 百萬元)
            a = get("https://www.twse.com.tw/rwd/zh/afterTrading/MI_5MINS?response=json")
            if a.get("stat") == "OK" and a["date"] == day:
                pa, spv, sa = 0.0, 0.0, 0.0
                for r in a["data"]:
                    amt = float(r[7].replace(",", "")); da = amt - pa; pa = amt
                    if r[0] in idx5 and da > 0:
                        spv += idx5[r[0]] * da; sa += da
                    if sa:
                        t = ts(day, r[0]); vw[t - t % 60] = round(spv / sa, 2)
        except Exception as e:
            print("MI_5MINS (vwap) failed:", e, file=sys.stderr)
        pts = [[t, v, None, vw.get(t)] for t, v in sorted(m.items())]
        src = "twse:MI_5MINS_INDEX+MI_5MINS"
    except Exception as e:
        print("TWSE failed, Yahoo fallback:", e, file=sys.stderr)
        r = get("https://query1.finance.yahoo.com/v8/finance/chart/%5ETWII?interval=1m&range=1d")["chart"]["result"][0]
        pts = [[t, round(c, 2), None, None] for t, c in zip(r["timestamp"], r["indicators"]["quote"][0]["close"]) if c]
        prev = r["meta"].get("chartPreviousClose"); src = "yahoo:^TWII"
    try:  # real-time last from MIS (5s delay) appended if newer
        q = get("https://mis.twse.com.tw/stock/api/getStockInfo.jsp?ex_ch=tse_t00.tw")["msgArray"][0]
        if q.get("z") not in (None, "-"):
            t = int(q["tlong"]) // 1000; v = float(q["z"]); prev = float(q["y"])
            if not pts or t > pts[-1][0]: pts.append([t, v, None, pts[-1][3] if pts else None])
    except Exception as e:
        print("MIS failed:", e, file=sys.stderr)
    return {"name": "加權指數", "src": src, "prev": prev, "points": pts}  # [epoch, index, null, vwap]

# ---------- individual stocks: intraday 1-min (Yahoo chart API, server-side) ----------
def universe():
    """Top-N by previous-day trading value (TWSE + TPEx openapi) + data/watchlist.txt codes."""
    rows = []
    try:
        for r in get("https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL"):
            if r["Code"].isdigit() and len(r["Code"]) == 4:
                rows.append((float(r["TradeValue"] or 0), r["Code"], "TW"))
    except Exception as e: print("STOCK_DAY_ALL failed:", e, file=sys.stderr)
    try:
        for r in get("https://www.tpex.org.tw/openapi/v1/tpex_mainboard_daily_close_quotes"):
            c = r["SecuritiesCompanyCode"]
            if c.isdigit() and len(c) == 4:
                rows.append((float(r["TransactionAmount"] or 0), c, "TWO"))
    except Exception as e: print("TPEx quotes failed:", e, file=sys.stderr)
    rows.sort(reverse=True)
    mk = {c: m for _, c, m in rows}
    sel = [(c, m) for _, c, m in rows[:TOPN]]
    wl = os.path.join(OUT, "watchlist.txt")
    if os.path.exists(wl):
        for c in open(wl).read().split():
            if c in mk and (c, mk[c]) not in sel: sel.append((c, mk[c]))
    return sel

def stock_intraday(code, mkt):
    r = get("https://query1.finance.yahoo.com/v8/finance/chart/%s.%s?interval=1m&range=1d" % (code, mkt))["chart"]["result"][0]
    q = r["indicators"]["quote"][0]; pts = []
    for t, c, v in zip(r.get("timestamp") or [], q["close"], q["volume"]):
        if c is not None: pts.append([t, round(c, 2), v or 0])
    return {"code": code, "mkt": mkt, "prev": r["meta"].get("chartPreviousClose"), "points": pts}  # [epoch, close, vol]

def stocks(now):
    from concurrent.futures import ThreadPoolExecutor
    d = os.path.join(OUT, "intraday"); os.makedirs(d, exist_ok=True)
    sel = universe(); ok = []
    def one(cm):
        try:
            x = stock_intraday(*cm)
            if not x["points"]: return None
            x["updated"] = now
            json.dump(x, open(os.path.join(d, cm[0] + ".json"), "w"), separators=(",", ":"))
            return cm[0]
        except Exception as e:
            print("stock", cm[0], "failed:", e, file=sys.stderr)
    with ThreadPoolExecutor(8) as ex:
        ok = [c for c in ex.map(one, sel) if c]
    json.dump({"updated": now, "codes": sorted(ok)}, open(os.path.join(d, "index.json"), "w"), separators=(",", ":"))
    print("intraday stocks", len(ok), "/", len(sel))

def tx():
    base = "https://mis.taifex.com.tw/futures/api/"
    sym = None
    for mt in ("1", "0"):  # 1 = night (after-hours), 0 = regular; pick whichever is live
        ql = get(base + "getQuoteList", {"MarketType": mt, "SymbolType": "F", "KindID": "1", "CID": "TXF",
               "ExpireMonth": "", "RowSize": "全部", "PageNo": "", "SortColumn": "", "AscDesc": "A"})["RtData"]["QuoteList"]
        c = [q for q in ql if q["SymbolID"] != "TXF-P" and "/" not in q["SymbolID"]]
        if c and c[0]["CLastPrice"]:
            sym = c[0]["SymbolID"]; break  # first contract = near month
    if not sym: sym = c[0]["SymbolID"]
    d = get(base + "getChartData1M", {"SymbolID": sym})["RtData"]
    q, day = d["Quote"], d["Quote"]["CDate"]
    pts, cum_pv, cum_v = [], 0.0, 0
    night = sym.endswith("-M")
    for T, O, H, L, C, V in d["Ticks"]:
        dd = day
        t = ts(dd, T)
        if night and int(T) < 150000:  # after midnight -> next calendar day
            t += 86400
        c, v = float(C), int(V)
        cum_pv += c * v; cum_v += v
        pts.append([t, c, v, round(cum_pv / cum_v, 2) if cum_v else c])
    return {"name": d["DispCName"], "symbol": sym, "session": "night" if night else "day",
            "src": "taifex:getChartData1M", "prev": float(q["CRefPrice"]), "last": float(q["CLastPrice"] or 0),
            "points": pts}  # [epoch, close, vol, vwap]

ETFS = ["0050", "0056", "00878", "00919", "00981A"]

def etf():
    """TWSE MIS quotes for ETF zone -> data/etf.json (Yahoo fallback per symbol)."""
    out = {}
    try:
        ch = "|".join("tse_%s.tw" % c for c in ETFS)
        d = get("https://mis.twse.com.tw/stock/api/getStockInfo.jsp?ex_ch=%s&json=1&delay=0" % ch)
        for q in d.get("msgArray", []):
            y = float(q["y"]); z = q.get("z", "-")
            if z in ("-", "", None):  # no trade yet: use best bid/ask midpoint-free fallback = first bid
                b = (q.get("b") or "-").split("_")[0]
                z = b if b not in ("-", "") else None
            last = float(z) if z else None
            out[q["c"]] = {"name": q["n"], "last": last, "prev": y,
                           "chg": round(last - y, 2) if last else None,
                           "pct": round((last - y) / y * 100, 2) if last else None,
                           "date": q.get("d"), "time": q.get("t"), "src": "twse-mis"}
    except Exception as e:
        print("etf mis FAILED", e, file=sys.stderr)
    for c in ETFS:
        if c in out and out[c]["last"]:
            continue
        try:
            r = get("https://query1.finance.yahoo.com/v8/finance/chart/%s.TW?range=1d&interval=1d" % c)["chart"]["result"][0]["meta"]
            last, y = r["regularMarketPrice"], r["chartPreviousClose"]
            out[c] = {"name": out.get(c, {}).get("name", c), "last": last, "prev": y,
                      "chg": round(last - y, 2), "pct": round((last - y) / y * 100, 2),
                      "date": datetime.datetime.fromtimestamp(r["regularMarketTime"], TZ).strftime("%Y%m%d"),
                      "time": datetime.datetime.fromtimestamp(r["regularMarketTime"], TZ).strftime("%H:%M:%S"), "src": "yahoo"}
        except Exception as e:
            print("etf yahoo", c, "FAILED", e, file=sys.stderr)
    if not out:
        raise RuntimeError("no etf quotes")
    return {"codes": ETFS, "quotes": out}

if __name__ == "__main__":
    os.makedirs(OUT, exist_ok=True)
    now = datetime.datetime.now(TZ).isoformat(timespec="seconds")
    for name, fn in (("taiex", taiex), ("tx", tx)):
        try:
            data = fn(); data["updated"] = now
            json.dump(data, open(os.path.join(OUT, name + ".json"), "w"), ensure_ascii=False, separators=(",", ":"))
            print(name, data.get("symbol", ""), len(data["points"]), data["points"][-1])
        except Exception as e:
            print(name, "FAILED", e, file=sys.stderr)  # keep previous file
    try:
        data = etf(); data["updated"] = now
        json.dump(data, open(os.path.join(OUT, "etf.json"), "w"), ensure_ascii=False, separators=(",", ":"))
        print("etf", len(data["quotes"]))
    except Exception as e:
        print("etf FAILED", e, file=sys.stderr)
    hm = datetime.datetime.now(TZ).strftime("%H%M")
    if os.environ.get("STOCKS") == "1" or ("0900" <= hm <= "1345" and os.environ.get("STOCKS") != "0"):
        stocks(now)
