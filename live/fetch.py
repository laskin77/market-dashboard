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

def fnum(x):
    if x in (None, "-", ""):
        return None
    try:
        return float(str(x).replace(",", ""))
    except (TypeError, ValueError):
        return None

def session_day(now=None):
    """Trading day shown on the 加權 chart. Before 09:00 Taipei, that is the previous weekday."""
    now = now or datetime.datetime.now(TZ)
    if now.hour < 9:
        prev = now - datetime.timedelta(days=1)
        while prev.weekday() >= 5:
            prev -= datetime.timedelta(days=1)
        return prev.strftime("%Y%m%d")
    return now.strftime("%Y%m%d")

def epoch_day(t):
    return datetime.datetime.fromtimestamp(int(t), TZ).strftime("%Y%m%d")

def in_day_session(t, day):
    return ts(day, "090000") <= int(t) <= ts(day, "133059")

# MIS index field m is cumulative turnover. One unit lines up with about 1e5 TWD
# (cnyes per-minute volume is in TWD). The scale cancels inside a pure-MIS average;
# it only matters when a backfill bar's TWD volume shares the same 均價.
MIS_M_TO_TWD = 100000

def mis_index():
    q = get("https://mis.twse.com.tw/stock/api/getStockInfo.jsp?ex_ch=tse_t00.tw&json=1&delay=0")["msgArray"][0]
    t = int(q["tlong"]) // 1000 if q.get("tlong") else None
    if t:
        t -= t % 60
    return {"day": str(q.get("d") or ""), "z": fnum(q.get("z")), "t": t,
            "y": fnum(q.get("y")), "o": fnum(q.get("o")), "m": fnum(q.get("m"))}

def fetch_official_index(day):
    """MI_5MINS_INDEX with today's date. The undated endpoint keeps serving yesterday until it rolls."""
    stamp = int(datetime.datetime.now(TZ).timestamp())
    urls = [
        "https://www.twse.com.tw/rwd/zh/TAIEX/MI_5MINS_INDEX?response=json&date=%s&_=%d" % (day, stamp),
        "https://www.twse.com.tw/exchangeReport/MI_5MINS_INDEX?response=json&date=%s&_=%d" % (day, stamp),
        "https://www.twse.com.tw/rwd/zh/TAIEX/MI_5MINS_INDEX?response=json&_=%d" % stamp,
    ]
    for url in urls:
        try:
            d = get(url)
        except Exception as e:
            print("MI_5MINS_INDEX failed:", e, file=sys.stderr)
            continue
        if d.get("stat") != "OK" or str(d.get("date") or "") != str(day) or not d.get("data"):
            print("MI_5MINS_INDEX skip", d.get("date"), d.get("stat"), file=sys.stderr)
            continue
        return d
    return None

def official_points(d, day):
    prev = fnum(d["data"][0][1]) if d.get("data") else None  # 09:00:00 row is the previous close
    m = {}
    for row in d["data"]:
        try:
            t = ts(day, row[0])
        except ValueError:
            continue
        px = fnum(row[1])
        if px is None or not in_day_session(t, day) or epoch_day(t) != str(day):
            continue
        m[t - t % 60] = px  # last print in the minute
    pts = [[t, round(v, 2), None, None] for t, v in sorted(m.items())]
    return pts, prev

def fetch_official_vwap(day, index_rows):
    """均價: weight each 5-sec print by Δ成交金額. Refuse any file that is not this trading day."""
    idx5 = {}
    for r in index_rows:
        px = fnum(r[1])
        if px is not None:
            idx5[r[0]] = px
    stamp = int(datetime.datetime.now(TZ).timestamp())
    urls = [
        "https://www.twse.com.tw/rwd/zh/afterTrading/MI_5MINS?response=json&date=%s&_=%d" % (day, stamp),
        "https://www.twse.com.tw/rwd/zh/afterTrading/MI_5MINS?response=json&_=%d" % stamp,
    ]
    a = None
    for url in urls:
        try:
            got = get(url)
        except Exception as e:
            print("MI_5MINS failed:", e, file=sys.stderr)
            continue
        if got.get("stat") == "OK" and str(got.get("date") or "") == str(day) and got.get("data"):
            a = got
            break
        print("MI_5MINS skip", got.get("date"), got.get("stat"), file=sys.stderr)
    if not a:
        return {}
    pa, spv, sa, vw = 0.0, 0.0, 0.0, {}
    for r in a["data"]:
        amt = fnum(r[7])
        if amt is None:
            continue
        da, pa = amt - pa, amt
        if r[0] in idx5 and da > 0:
            spv += idx5[r[0]] * da
            sa += da
        if not sa:
            continue
        try:
            t = ts(day, r[0])
        except ValueError:
            continue
        if in_day_session(t, day):
            vw[t - t % 60] = round(spv / sa, 2)
    return vw

def fetch_backfill(day):
    """Minute bars from 09:00 for the gap before this loop has sampled MIS. Prices stay today-only."""
    bars = {}
    try:
        start, end = ts(day, "085500"), int(datetime.datetime.now(TZ).timestamp()) + 180
        url = ("https://ws.api.cnyes.com/ws/api/v1/charting/history?resolution=1"
               "&symbol=TWS:TSE01:INDEX&from=%d&to=%d" % (start, end))
        d = get(url).get("data") or {}
        if d.get("s") == "ok":
            vols = d.get("v") or []
            for i, (t, c) in enumerate(zip(d.get("t") or [], d.get("c") or [])):
                if c is None:
                    continue
                t = int(t) - int(t) % 60
                if epoch_day(t) != str(day) or not in_day_session(t, day):
                    continue
                v = vols[i] if i < len(vols) and vols[i] else 0
                bars[t] = (float(c), float(v or 0))
    except Exception as e:
        print("cnyes backfill failed:", e, file=sys.stderr)
    if bars:
        return bars, "cnyes"
    try:
        r = get("https://query1.finance.yahoo.com/v8/finance/chart/%5ETWII?interval=1m&range=1d")["chart"]["result"][0]
        q = r["indicators"]["quote"][0]
        vols = q.get("volume") or []
        for i, (t, c) in enumerate(zip(r.get("timestamp") or [], q.get("close") or [])):
            if c is None:
                continue
            t = int(t) - int(t) % 60
            if epoch_day(t) != str(day) or not in_day_session(t, day):
                continue
            v = vols[i] if i < len(vols) and vols[i] else 0
            bars[t] = (float(c), float(v or 0))
    except Exception as e:
        print("yahoo backfill failed:", e, file=sys.stderr)
    return bars, ("yahoo" if bars else "")

def load_taiex_state(day):
    path = os.path.join(OUT, "taiex.json")
    if not os.path.exists(path):
        return None
    try:
        old = json.load(open(path, encoding="utf-8"))
    except Exception:
        return None
    if str(old.get("date") or "") != str(day):
        return None
    return old

def push_mis(hist, quote, day):
    hist = [h for h in (hist or []) if epoch_day(h[0]) == str(day) and in_day_session(h[0], day)]
    if not quote or quote.get("day") != str(day) or quote.get("z") is None or not quote.get("t"):
        return hist
    if not in_day_session(quote["t"], day):
        return hist
    t = int(quote["t"])
    hist = [h for h in hist if int(h[0]) != t]
    hist.append([t, quote["z"], quote.get("m")])
    hist.sort(key=lambda h: h[0])
    return hist

def fold_mis(bars, hist):
    """MIS last wins for any minute this loop has seen. Backfill volume is kept when we have it."""
    prev_m = None
    for h in hist:
        t, price = int(h[0]), float(h[1])
        m = float(h[2]) if isinstance(h[2], (int, float)) else fnum(h[2])
        vol = None
        if m is not None and prev_m is not None and m > prev_m:
            vol = (m - prev_m) * MIS_M_TO_TWD
        if m is not None:
            prev_m = m
        if t in bars and bars[t][1]:
            bars[t] = (price, bars[t][1])
        else:
            bars[t] = (price, vol or 0.0)
    return bars

def bars_to_points(bars):
    spv = sa = 0.0
    pts = []
    for t in sorted(bars):
        price, vol = bars[t]
        if vol and vol > 0:
            spv += price * vol
            sa += vol
        pts.append([int(t), round(price, 2), None, round(spv / sa, 2) if sa else None])
    return pts

def apply_vwap_map(pts, vw):
    last = None
    for p in pts:
        if p[0] in vw:
            last = vw[p[0]]
        if last is not None:
            p[3] = last
    return pts

def drop_stale_taiex(day):
    path = os.path.join(OUT, "taiex.json")
    if not os.path.exists(path):
        return
    try:
        old = json.load(open(path, encoding="utf-8"))
    except Exception:
        return
    if str(old.get("date") or "") != str(day):
        os.remove(path)

def taiex():
    day = session_day()
    try:
        data = build_taiex(day)
    except Exception:
        if datetime.datetime.now(TZ).hour >= 9:
            drop_stale_taiex(day)
        raise
    if not data["points"] or any(epoch_day(p[0]) != str(day) for p in data["points"]):
        raise RuntimeError("taiex points are not %s only" % day)
    return data

def build_taiex(day):
    mis = None
    try:
        mis = mis_index()
    except Exception as e:
        print("MIS t00 failed:", e, file=sys.stderr)
    prev = mis["y"] if mis and mis.get("day") == str(day) and mis.get("y") is not None else None
    official = fetch_official_index(day)
    if official:
        pts, prev_off = official_points(official, day)
        if prev is None:
            prev = prev_off
        vw = {}
        try:
            vw = fetch_official_vwap(day, official["data"])
        except Exception as e:
            print("MI_5MINS (vwap) failed:", e, file=sys.stderr)
        hist = push_mis((load_taiex_state(day) or {}).get("mis"), mis, day)
        if mis and mis.get("day") == str(day) and mis.get("z") is not None and mis.get("t") and in_day_session(mis["t"], day):
            t = int(mis["t"])
            pts = [p for p in pts if p[0] != t]
            pts.append([t, round(mis["z"], 2), None, None])
            pts.sort(key=lambda p: p[0])
        if vw:
            apply_vwap_map(pts, vw)
        elif pts:
            bars, _how = fetch_backfill(day)
            vol = {t: bars[t][1] for t in bars}
            spv = sa = 0.0
            for p in pts:
                v = vol.get(p[0]) or 0
                if v > 0:
                    spv += p[1] * v
                    sa += v
                p[3] = round(spv / sa, 2) if sa else None
        src = "twse:MI_5MINS_INDEX" + ("+MI_5MINS" if vw else "") + ("+mis" if hist else "")
        return {"name": "加權指數", "src": src, "prev": prev, "date": day, "points": pts, "mis": hist}

    state = load_taiex_state(day) or {}
    bars, how = fetch_backfill(day)
    hist = push_mis(state.get("mis"), mis, day)
    fold_mis(bars, hist)
    if hist:
        last_mis = max(int(h[0]) for h in hist)
        bars = {t: v for t, v in bars.items() if t <= last_mis}
    if mis and mis.get("day") == str(day) and mis.get("o") and ts(day, "090000") not in bars:
        bars[ts(day, "090000")] = (mis["o"], 0.0)
    pts = [p for p in bars_to_points(bars) if epoch_day(p[0]) == str(day) and in_day_session(p[0], day)]
    if not pts:
        raise RuntimeError("no today TAIEX series")
    if prev is None:
        prev = state.get("prev")
    src = "mis:tse_t00.tw" + (("+" + how) if how else "")
    print("taiex", day, src, "n", len(pts), "mis", len(hist), file=sys.stderr)
    return {"name": "加權指數", "src": src, "prev": prev, "date": day, "points": pts, "mis": hist}  # [epoch, index, null, vwap]

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

def on_day(epoch, day):
    try:
        return epoch_day(epoch) == str(day)
    except (TypeError, ValueError, OSError):
        return False

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
    day = session_day()
    def one(cm):
        code, mkt = cm
        path = os.path.join(d, code + ".json")
        x = None
        try:
            y = stock_intraday(code, mkt)
            y["points"] = [p for p in y["points"] if on_day(p[0], day)]
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
            pts = [p for p in ((base or {}).get("points") or []) if on_day(p[0], day)]
            if pts and q["t"] > pts[-1][0] + 6 * 3600:
                pts = []
            if q.get("t") and on_day(q["t"], day) and (not pts or q["t"] > pts[-1][0] + 20):
                pts.append([q["t"], q["last"], 0])
            prev = q["prev"] if q["prev"] is not None else (base or {}).get("prev")
            x = {"code": code, "mkt": mkt, "prev": prev, "points": pts}
        # MIS y is the exchange reference (開盤競價基準), including ex-dividend names.
        # Yahoo chartPreviousClose is the pre-dividend close and must not win.
        if q and x and q.get("prev") is not None:
            x["prev"] = q["prev"]
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
            y = float(q["y"])  # 開盤競價基準 / 參考價, not a guessed ex-div close
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
