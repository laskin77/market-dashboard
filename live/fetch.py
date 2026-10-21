#!/usr/bin/env python3
"""Fetch intraday TAIEX + TX near-month 1-min series -> data/taiex.json, data/tx.json (stdlib only)."""
import json, urllib.request, datetime, os, sys
TZ = datetime.timezone(datetime.timedelta(hours=8))
UA = {"User-Agent": "Mozilla/5.0"}
# Script lives in live/; JSON is served from the repo root data/ directory.
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "data")

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
        pts = [[t, v] for t, v in sorted(m.items())]
        prev = pts[0][1]  # 09:00:00 row = previous close
        src = "twse:MI_5MINS_INDEX"
    except Exception as e:
        print("TWSE failed, Yahoo fallback:", e, file=sys.stderr)
        r = get("https://query1.finance.yahoo.com/v8/finance/chart/%5ETWII?interval=1m&range=1d")["chart"]["result"][0]
        pts = [[t, round(c, 2)] for t, c in zip(r["timestamp"], r["indicators"]["quote"][0]["close"]) if c]
        prev = r["meta"].get("chartPreviousClose"); src = "yahoo:^TWII"
    try:  # real-time last from MIS (5s delay) appended if newer
        q = get("https://mis.twse.com.tw/stock/api/getStockInfo.jsp?ex_ch=tse_t00.tw")["msgArray"][0]
        if q.get("z") not in (None, "-"):
            t = int(q["tlong"]) // 1000; v = float(q["z"]); prev = float(q["y"])
            if not pts or t > pts[-1][0]: pts.append([t, v])
    except Exception as e:
        print("MIS failed:", e, file=sys.stderr)
    return {"name": "加權指數", "src": src, "prev": prev, "points": pts}

def tx():
    base = "https://mis.taifex.com.tw/futures/api/"
    sym = None
    c = []
    for mt in ("1", "0"):  # 1 = night (after-hours), 0 = regular; pick whichever is live
        ql = get(base + "getQuoteList", {"MarketType": mt, "SymbolType": "F", "KindID": "1", "CID": "TXF",
               "ExpireMonth": "", "RowSize": "全部", "PageNo": "", "SortColumn": "", "AscDesc": "A"})["RtData"]["QuoteList"]
        c = [q for q in ql if q["SymbolID"] != "TXF-P" and "/" not in q["SymbolID"]]
        if c and c[0]["CLastPrice"]:
            sym = c[0]["SymbolID"]; break  # first contract = near month
    if not sym:
        if not c: raise RuntimeError("no TX quote")
        sym = c[0]["SymbolID"]
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

if __name__ == "__main__":
    os.makedirs(OUT, exist_ok=True)
    now = datetime.datetime.now(TZ).isoformat(timespec="seconds")
    for name, fn in (("taiex", taiex), ("tx", tx)):
        try:
            data = fn(); data["updated"] = now
            path = os.path.join(OUT, name + ".json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
            print(name, data.get("symbol", ""), len(data["points"]), data["points"][-1], "->", path)
        except Exception as e:
            print(name, "FAILED", e, file=sys.stderr)  # keep previous file
