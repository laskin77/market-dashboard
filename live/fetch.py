#!/usr/bin/env python3
"""Fetch intraday TAIEX + TX near-month 1-min series -> data/taiex.json, data/tx.json (stdlib only)."""
import json, urllib.request, urllib.parse, datetime, os, sys
try:
    from zoneinfo import ZoneInfo
    NY = ZoneInfo("America/New_York")
except Exception:
    NY = datetime.timezone(datetime.timedelta(hours=-4))
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
    s = str(hhmmss).replace(":", "")
    # TAIFEX labels the bar at the top of the hour as HH6000 (20:60:00).
    if len(s) >= 6 and s[2:4] == "60":
        hh = int(s[:2]) + 1
        s = "%02d00%s" % (hh % 24, s[4:6])
        if hh >= 24:
            day = (datetime.datetime.strptime(day, "%Y%m%d") + datetime.timedelta(days=1)).strftime("%Y%m%d")
    return int(datetime.datetime.strptime(day + s[:6], "%Y%m%d%H%M%S").replace(tzinfo=TZ).timestamp())

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
    for t, c, v in zip(r.get("timestamp") or [], q.get("close") or [], q.get("volume") or []):
        if c is not None: pts.append([t, round(float(c), 2), v or 0])
    return {"code": code, "mkt": mkt, "prev": r["meta"].get("chartPreviousClose"), "points": pts}  # [epoch, close, vol]

def mis_price(q):
    """Last trade. MIS leaves z as '-' between prints and keeps the trade in trade.z."""
    z = q.get("z")
    if z not in (None, "-", ""):
        return float(z)
    tr = q.get("trade") or {}
    if isinstance(tr, str):
        try:
            tr = json.loads(tr)
        except ValueError:
            tr = {}
    tz = tr.get("z") if isinstance(tr, dict) else None
    if tz not in (None, "-", ""):
        return float(tz)
    return None

def mis_last(sel):
    """Live last/prev from TWSE MIS. Yahoo's 1-minute bars can be empty during the session."""
    out = {}
    for i in range(0, len(sel), 40):
        chunk = sel[i:i + 40]
        ch = "|".join(("tse_" if m == "TW" else "otc_") + c + ".tw" for c, m in chunk)
        try:
            d = get("https://mis.twse.com.tw/stock/api/getStockInfo.jsp?ex_ch=%s&json=1&delay=0" % ch)
        except Exception as e:
            print("mis batch failed:", e, file=sys.stderr)
            continue
        for q in d.get("msgArray") or []:
            try:
                last = mis_price(q)
                if last is None:
                    continue
                prev = float(q["y"]) if q.get("y") not in (None, "-", "") else None
                t = int(q["tlong"]) // 1000
            except (TypeError, ValueError):
                continue
            out[q["c"]] = {"last": round(last, 2), "prev": prev, "t": t}
    return out

def stocks(now):
    from concurrent.futures import ThreadPoolExecutor
    d = os.path.join(OUT, "intraday"); os.makedirs(d, exist_ok=True)
    sel = universe()
    quotes = mis_last(sel)
    def one(cm):
        code, mkt = cm
        path = os.path.join(d, code + ".json")
        x = None
        try:
            y = stock_intraday(code, mkt)
            if y["points"]:
                x = y
        except Exception as e:
            print("stock", code, "yahoo:", e, file=sys.stderr)
        q = quotes.get(code)
        if q and (x is None or not x["points"] or q["t"] > x["points"][-1][0] + 30):
            # Yahoo has no bar for this print. Keep appending MIS lasts on this runner.
            base = x
            if base is None and os.path.exists(path):
                try:
                    base = json.load(open(path, encoding="utf-8"))
                except Exception:
                    base = None
            pts = list((base or {}).get("points") or [])
            if pts and q["t"] > pts[-1][0] + 6 * 3600:
                pts = []
            if not pts or q["t"] > pts[-1][0] + 20:
                pts.append([q["t"], q["last"], 0])
            prev = q["prev"] if q["prev"] is not None else (base or {}).get("prev")
            x = {"code": code, "mkt": mkt, "prev": prev, "points": pts}
        if not x or not x["points"]:
            return None
        x["updated"] = now
        json.dump(x, open(path, "w"), separators=(",", ":"))
        return code
    with ThreadPoolExecutor(8) as ex:
        ok = [c for c in ex.map(one, sel) if c]
    json.dump({"updated": now, "codes": sorted(ok)}, open(os.path.join(d, "index.json"), "w"), separators=(",", ":"))
    print("intraday stocks", len(ok), "/", len(sel), "mis", len(quotes))

def tx():
    base = "https://mis.taifex.com.tw/futures/api/"
    tnow = datetime.datetime.now(TZ)
    hm = tnow.hour * 60 + tnow.minute
    day_sess = tnow.weekday() <= 4 and (8 * 60 + 45) <= hm <= (13 * 60 + 45)
    # Night quotes keep yesterday's last price after 05:00, so the clock chooses the
    # session. Day contracts end in -F (TXFK6-F); night contracts end in -M.
    # TXF-S is the spot index and is not the futures chart.
    order = (("0", "-F"), ("1", "-M")) if day_sess else (("1", "-M"), ("0", "-F"))
    sym = None
    for mt, suffix in order:
        ql = get(base + "getQuoteList", {"MarketType": mt, "SymbolType": "F", "KindID": "1", "CID": "TXF",
               "ExpireMonth": "", "RowSize": "全部", "PageNo": "", "SortColumn": "", "AscDesc": "A"})["RtData"]["QuoteList"]
        c = [q for q in ql if "/" not in q.get("SymbolID", "") and q["SymbolID"].endswith(suffix) and q.get("CLastPrice")]
        if c:
            sym = c[0]["SymbolID"]
            break
    if not sym:
        raise RuntimeError("no TX contract")
    d = get(base + "getChartData1M", {"SymbolID": sym})["RtData"]
    q, day = d["Quote"], d["Quote"]["CDate"]
    pts, cum_pv, cum_v = [], 0.0, 0
    night = sym.endswith("-M")
    for T, O, H, L, C, V in d["Ticks"]:
        raw = str(T).replace(":", "")
        try:
            t = ts(day, raw)
        except ValueError:
            print("skip tick", T, file=sys.stderr)
            continue
        if night and raw.isdigit() and int(raw[:6]) < 150000:  # after midnight -> next calendar day
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
            y = float(q["y"])
            last = mis_price(q)
            if last is None:  # no trade yet: first bid
                b = (q.get("b") or "-").split("_")[0]
                last = float(b) if b not in ("-", "") else None
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

US_SYMS = (("DJI", "^DJI", "道瓊"), ("GSPC", "^GSPC", "標普"), ("IXIC", "^IXIC", "那指"), ("SOX", "^SOX", "費半"), ("VIX", "^VIX", "VIX"))

def ny_now():
    return datetime.datetime.now(NY)

def us_should_fetch(path):
    t = ny_now()
    hm = t.hour * 60 + t.minute
    if t.weekday() <= 4 and (9 * 60 + 25) <= hm <= (16 * 60 + 10):
        return True  # regular session, plus a few minutes around the bell
    return not os.path.exists(path)

def fetch_us(stamp):
    """1-minute regular-session bars for the four indices, and VIX last. Yahoo chart."""
    path = os.path.join(OUT, "us.json")
    if os.environ.get("US") == "0" or not us_should_fetch(path):
        print("us skip")
        return
    quotes, regular = {}, {}
    for key, sym, name in US_SYMS:
        try:
            url = "https://query1.finance.yahoo.com/v8/finance/chart/%s?interval=1m&range=1d&includePrePost=false" % urllib.parse.quote(sym)
            r = get(url)["chart"]["result"][0]
            meta, q = r["meta"], r["indicators"]["quote"][0]
            pts = []
            if key != "VIX":
                for t, c in zip(r.get("timestamp") or [], q.get("close") or []):
                    if c is not None:
                        pts.append([int(t), round(float(c), 2)])
            prev = meta.get("chartPreviousClose") or meta.get("previousClose")
            last = pts[-1][1] if pts else meta.get("regularMarketPrice")
            if last is not None:
                last = round(float(last), 2)
            chg = round(last - prev, 2) if last is not None and prev else None
            pct = round(chg / prev * 100, 2) if chg is not None and prev else None
            item = {"name": name, "symbol": sym, "prev": None if prev is None else round(float(prev), 2),
                    "last": last, "chg": chg, "pct": pct, "updated": stamp}
            if key != "VIX":
                item["points"] = pts
            quotes[key] = item
            reg = (meta.get("currentTradingPeriod") or {}).get("regular") or {}
            if reg.get("start") and not regular:
                regular = reg
            print("us", sym, last, "pts", len(pts))
        except Exception as e:
            print("us", sym, "FAILED", e, file=sys.stderr)
    if not quotes:
        print("us FAILED no quotes", file=sys.stderr)
        return
    ny = ny_now()
    data = {"updated": stamp, "tz_offset": int(ny.utcoffset().total_seconds()),
            "regular_start": regular.get("start"), "regular_end": regular.get("end"), "quotes": quotes}
    json.dump(data, open(path, "w"), ensure_ascii=False, separators=(",", ":"))
    print("us", len(quotes), "open_window", data["regular_start"], data["regular_end"])

def write_live():
    """One bundle for the page: data/live.json = {updated, taiex, tx, etf, us}."""
    stamp = datetime.datetime.now(TZ).isoformat(timespec="seconds")
    bundle = {"updated": stamp}
    for name in ("taiex", "tx", "etf", "us"):
        path = os.path.join(OUT, name + ".json")
        if not os.path.exists(path):
            continue
        with open(path, encoding="utf-8") as f:
            obj = json.load(f)
        obj["updated"] = stamp
        bundle[name] = obj
    if len(bundle) == 1:
        print("live.json skipped, no series", file=sys.stderr)
        return
    out = os.path.join(OUT, "live.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(bundle, f, ensure_ascii=False, separators=(",", ":"))
    tx = bundle.get("tx") or {}
    print("live.json", stamp, tx.get("symbol", ""), (tx.get("points") or [[None]])[-1])

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
    try:
        fetch_us(now)
    except Exception as e:
        print("us FAILED", e, file=sys.stderr)
    write_live()
