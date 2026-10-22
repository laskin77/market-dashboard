#!/usr/bin/env python3
"""Rebuild the after-close report from TWSE / TPEx official files.

Writes data/close.json, data/etf.json and data/etf_div.json.
Market, sectors and ETFs are expected after ~14:30 Taipei.
機構買賣超 (T86 / BFI82U) often appears later; a run before that
still publishes the rest and leaves inst.ready false.

CLOSE_PUSH=1 commits those JSON files to main (used by the live loop).
The close workflow commits them itself.
"""
import datetime, json, os, subprocess, sys, time, urllib.request

TZ = datetime.timezone(datetime.timedelta(hours=8))
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UA = {"User-Agent": "Mozilla/5.0"}
ETFS = ["0050", "0056", "00878", "00919", "00981A"]
# TWSE 產業別代號 (t187ap03).
IND_NAME = {
    "01": "水泥", "02": "食品", "03": "塑膠", "04": "紡織", "05": "電機機械", "06": "電器電纜",
    "08": "玻璃陶瓷", "09": "造紙", "10": "鋼鐵", "11": "橡膠", "12": "汽車", "14": "建材營造",
    "15": "航運", "16": "觀光餐旅", "17": "金融保險", "18": "貿易百貨", "19": "綜合", "20": "其他",
    "21": "化學", "22": "生技醫療", "23": "油電燃氣", "24": "半導體", "25": "電腦及周邊", "26": "光電",
    "27": "通信網路", "28": "電子零組件", "29": "電子通路", "30": "資訊服務", "31": "其他電子",
    "32": "文化創意", "33": "農業科技", "34": "電子商務", "35": "綠能環保", "36": "數位雲端",
    "37": "運動休閒", "38": "居家生活", "91": "存託憑證",
}
# Combined indexes that double-count the narrower 類指數.
SKIP_SECTOR = {"化學生技醫療類指數", "電子工業類指數", "水泥窯製類指數", "塑膠化工類指數"}
MIN_TURNOVER = 3e8  # 3 億元


def fnum(x):
    if x in (None, "", "--", "---", "X", "null"):
        return None
    s = str(x).replace(",", "").replace("+", "").strip()
    if s in ("", "--", "---"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def sign_of(cell):
    return -1 if "-" in str(cell) else 1


def signed_mag(sign_cell, num_cell):
    """漲跌點數 is often an unsigned magnitude plus an HTML sign cell.
    漲跌百分比 is already signed ('-1.81', '2.83'). Use the sign that is
    actually printed on the number, and the sign cell only when it is not.
    """
    n = fnum(num_cell)
    if n is None:
        return None
    raw = str(num_cell)
    if "-" in raw or "+" in raw:
        return n
    return sign_of(sign_cell) * abs(n)


def get(url):
    last = None
    for i in range(3):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=40) as r:
                return json.loads(r.read().decode("utf-8", "replace"))
        except Exception as e:
            last = e
            time.sleep(2 + i * 2)
    raise last


def taipei_today():
    return datetime.datetime.now(TZ)


def ymd(d=None):
    d = d or taipei_today()
    return d.strftime("%Y%m%d")


def label(day):
    return "%s/%s" % (day[4:6].lstrip("0") or day[4:6], day[6:8].lstrip("0") or day[6:8])


def prev_weekday(day):
    d = datetime.datetime.strptime(day, "%Y%m%d")
    d -= datetime.timedelta(days=1)
    while d.weekday() >= 5:
        d -= datetime.timedelta(days=1)
    return d.strftime("%Y%m%d")


def load_json(path):
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def save_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, separators=(",", ":"))
        f.write("\n")


def mi_index(day):
    url = ("https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX?response=json&date=%s&type=ALLBUT0999" % day)
    d = get(url)
    if d.get("stat") != "OK" or str(d.get("date") or "") != day:
        raise RuntimeError("MI_INDEX %s %s" % (d.get("date"), d.get("stat")))
    return d


def table(d, pred):
    for t in d.get("tables") or []:
        if pred(t.get("title") or "", t.get("fields") or []):
            return t
    return None


def parse_sectors(d):
    t = table(d, lambda title, fields: fields[:1] == ["指數"] and "收盤指數" in fields)
    out = []
    if not t:
        return out
    for row in t["data"]:
        name = row[0]
        if not name.endswith("類指數") or name in SKIP_SECTOR:
            continue
        if name.startswith("未含") or "槓桿" in name or "反向" in name:
            continue
        last = fnum(row[1])
        chg = signed_mag(row[2], row[3])
        pct = signed_mag(row[2], row[4])
        if last is None or chg is None or pct is None:
            continue
        out.append({"name": name.replace("類指數", ""), "index": name, "last": last,
                    "chg": round(chg, 2), "pct": round(pct, 2)})
    return out


def parse_taiex(d):
    t = table(d, lambda title, fields: fields[:1] == ["指數"])
    for row in (t or {}).get("data") or []:
        if row[0] == "發行量加權股價指數":
            last = fnum(row[1])
            chg = signed_mag(row[2], row[3])
            pct = signed_mag(row[2], row[4])
            if last is None or chg is None or pct is None:
                break
            if (chg < 0 < pct) or (chg > 0 > pct):
                raise RuntimeError("TAIEX sign mismatch %s %s" % (chg, pct))
            return {"last": last, "chg": round(chg, 2), "pct": round(pct, 2)}
    raise RuntimeError("no TAIEX row")


def parse_value_yi(d):
    """成交金額 from 大盤統計資訊, in 億元."""
    t = table(d, lambda title, fields: "成交金額" in "".join(fields))
    if not t:
        return None
    for row in t["data"]:
        if "成交金額" in str(row[0]) and "總" in str(row[0]) or str(row[0]).strip() in ("總計", "合計", "成交金額"):
            v = fnum(row[1])
            if v:
                return round(v / 1e8, 2)
    # FMTQIK is the reliable turnover; caller may override.
    return None


def fmtqik_turnover(day):
    d = get("https://www.twse.com.tw/rwd/zh/afterTrading/FMTQIK?response=json")
    if d.get("stat") != "OK":
        return None
    want = "%s/%s/%s" % (str(int(day[:4]) - 1911), day[4:6], day[6:8])
    for row in d.get("data") or []:
        if row[0] == want:
            return round(fnum(row[2]) / 1e8, 2)
    return None


def parse_quotes(d):
    t = table(d, lambda title, fields: "證券代號" in fields and "收盤價" in fields)
    rows = []
    if not t:
        return rows
    fields = t.get("fields") or []
    if len(fields) < 11 or "收盤" not in str(fields[8]) or "漲跌價差" not in str(fields[10]):
        raise RuntimeError("quote fields %s" % fields)
    for row in t["data"]:
        code = str(row[0]).strip()
        if not (code.isdigit() and len(code) == 4) or code.startswith("00"):
            continue
        last = fnum(row[8])
        diff = fnum(row[10])
        val = fnum(row[4])
        pe = fnum(row[15]) if len(row) > 15 else None
        if last is None or diff is None or not val:
            continue
        sgn = sign_of(row[9])
        chg = round(sgn * diff, 2)
        prev = last - chg
        if prev <= 0:
            continue
        rows.append({
            "code": code, "name": str(row[1]).strip(), "mkt": "上市",
            "last": last, "chg": chg, "pct": round(chg / prev * 100, 2),
            "prev": round(prev, 2), "value": val, "pe": pe if pe and pe > 0 else None,
        })
    return rows


def tpex_quotes():
    rows = []
    try:
        data = get("https://www.tpex.org.tw/openapi/v1/tpex_mainboard_daily_close_quotes")
    except Exception as e:
        print("tpex quotes failed", e, file=sys.stderr)
        return rows
    for row in data:
        code = str(row.get("SecuritiesCompanyCode") or "")
        if not (code.isdigit() and len(code) == 4) or code.startswith("00"):
            continue
        last = fnum(row.get("Close"))
        chg = fnum(row.get("Change"))
        val = fnum(row.get("TransactionAmount"))
        if last is None or chg is None or not val:
            continue
        prev = last - chg
        if prev <= 0:
            continue
        rows.append({
            "code": code, "name": str(row.get("CompanyName") or code).strip(), "mkt": "上櫃",
            "last": last, "chg": round(chg, 2), "pct": round(chg / prev * 100, 2),
            "prev": round(prev, 2), "value": val, "pe": None,
        })
    return rows


def tpex_index():
    data = get("https://www.tpex.org.tw/openapi/v1/tpex_index")
    last = data[-1]
    close, chg = fnum(last["Close"]), fnum(last["Change"])
    prev = close - chg
    return {"date": str(last["Date"]), "last": close, "chg": round(chg, 2),
            "pct": round(chg / prev * 100, 2) if prev else None}


def industries():
    m = {}
    try:
        for row in get("https://openapi.twse.com.tw/v1/opendata/t187ap03_L"):
            m[str(row.get("公司代號"))] = IND_NAME.get(str(row.get("產業別") or "").zfill(2), "")
    except Exception as e:
        print("industry failed", e, file=sys.stderr)
    return m


def hist_closes(code, mkt):
    if mkt == "上市":
        d = get("https://www.twse.com.tw/exchangeReport/STOCK_DAY?response=json&date=%s&stockNo=%s" % (ymd(), code))
        if d.get("stat") != "OK":
            return []
        return [fnum(r[6]) for r in d.get("data") or [] if fnum(r[6])]
    sym = "%s.TWO" % code
    url = "https://query1.finance.yahoo.com/v8/finance/chart/%s?range=1mo&interval=1d" % sym
    r = get(url)["chart"]["result"][0]
    return [float(c) for c in (r["indicators"]["quote"][0].get("close") or []) if c]


def five_day(code, mkt):
    try:
        closes = hist_closes(code, mkt)
    except Exception as e:
        print("5d", code, e, file=sys.stderr)
        return None
    if len(closes) < 6 or not closes[-6]:
        return None
    return round((closes[-1] / closes[-6] - 1) * 100, 2)


def sector_levels(day):
    d = get("https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX?response=json&date=%s&type=IND" % day)
    if d.get("stat") != "OK" or str(d.get("date") or "") != day:
        return {}
    return {s["index"]: s["last"] for s in parse_sectors(d)}


def bfi(day):
    d = get("https://www.twse.com.tw/rwd/zh/fund/BFI82U?response=json&dayDate=%s" % day)
    if d.get("stat") != "OK" or str(d.get("date") or "") != day:
        raise RuntimeError("BFI82U %s" % d.get("stat"))
    fields = d.get("fields") or []
    net_i = 3
    for i, name in enumerate(fields):
        if "差額" in str(name):
            net_i = i
            break
    out = {}
    for row in d["data"]:
        out[row[0]] = fnum(row[net_i])
    return {
        "foreign": out.get("外資及陸資(不含外資自營商)"),
        "trust": out.get("投信"),
        "total": out.get("合計"),
    }


def t86_top(day, quotes):
    d = get("https://www.twse.com.tw/rwd/zh/fund/T86?response=json&date=%s&selectType=ALL" % day)
    if d.get("stat") != "OK" or str(d.get("date") or "") != day or not d.get("data"):
        raise RuntimeError("T86 %s %s" % (d.get("date"), d.get("stat")))
    fields = [str(f) for f in (d.get("fields") or [])]

    def find_col(pred, default):
        for i, name in enumerate(fields):
            if pred(name):
                return i
        return default

    fi = find_col(lambda n: "外陸資買賣超" in n and "不含外資自營商" in n, 4)
    ti = find_col(lambda n: "投信買賣超" in n and "買進" not in n and "賣出" not in n, 10)
    by = {q["code"]: q for q in quotes if q["mkt"] == "上市"}
    foreign, trust = [], []
    for row in d["data"]:
        code = str(row[0]).strip()
        q = by.get(code)
        if not q:
            continue
        name = str(row[1]).strip() or q["name"]
        for bucket, idx in ((foreign, fi), (trust, ti)):
            shares = fnum(row[idx])
            if not shares:
                continue
            amt = shares * q["last"] / 1e8
            if amt <= 0:
                continue
            bucket.append({"code": code, "name": name, "amt_yi": round(amt, 2),
                           "close": q["last"], "pct": q["pct"]})
    foreign.sort(key=lambda r: -r["amt_yi"])
    trust.sort(key=lambda r: -r["amt_yi"])
    return foreign[:5], trust[:5]


def yi(v):
    return None if v is None else round(v / 1e8, 2)


def roll_div(div, today):
    """After an announced ex-date, that dividend becomes 上次 and the next slot is an estimate."""
    items = div.get("items") or {}
    changed = False
    for code, item in items.items():
        nxt = item.get("next") or {}
        ex = nxt.get("ex") or ""
        if nxt.get("status") == "ann" and ex and ex.replace("-", "") <= today:
            item["last"] = {"ex": nxt.get("ex"), "pay": nxt.get("pay"), "amt": nxt.get("amt")}
            item["next"] = estimate_next(item.get("freq") or "", ex)
            changed = True
            print("rolled dividend", code, "last", item["last"]["ex"], "next", item["next"].get("label"))
    div["updated"] = "%s-%s-%s" % (today[:4], today[4:6], today[6:8])
    return div, changed


def estimate_next(freq, ex):
    """Next typical month after an ex-date, from the cadence written in freq. No new announcement."""
    year, month, _day = (int(x) for x in ex.split("-"))
    months = []
    if "1/4/7/10" in freq:
        months = [1, 4, 7, 10]
    elif "2/5/8/11" in freq:
        months = [2, 5, 8, 11]
    elif "3/6/9/12" in freq:
        months = [3, 6, 9, 12]
    elif "1月、7月" in freq or "1月,7月" in freq:
        months = [1, 7]
    if not months:
        month += 3
        if month > 12:
            year, month = year + 1, month - 12
    else:
        later = [m for m in months if m > month]
        if later:
            month = later[0]
        else:
            year, month = year + 1, months[0]
    label_s = "未公告，預估 %d年%d月" % (year, month)
    return {"status": "est", "month": "%04d-%02d" % (year, month), "label": label_s}


def build(day):
    d = mi_index(day)
    taiex = parse_taiex(d)
    value = fmtqik_turnover(day)
    sectors = parse_sectors(d)
    quotes = parse_quotes(d)
    quotes.extend(tpex_quotes())
    otc = tpex_index()
    if otc["date"] != day:
        raise RuntimeError("TPEx index date %s" % otc["date"])
    ind = industries()
    ranked = [q for q in quotes if q["value"] >= MIN_TURNOVER]
    ranked.sort(key=lambda q: (-q["pct"], -q["value"]))
    strong = []
    for q in ranked[:5]:
        time.sleep(0.3)
        d5 = five_day(q["code"], q["mkt"])
        strong.append({
            "code": q["code"], "name": q["name"], "mkt": q["mkt"],
            "last": q["last"], "chg": q["chg"], "pct": q["pct"],
            "limit_up": q["pct"] >= 9.5,
            "d5": d5, "industry": ind.get(q["code"]) or q["mkt"],
            "pe": q["pe"], "value_yi": round(q["value"] / 1e8, 2),
        })
    # Month-to-date and five-session sector moves from index levels, not from a story.
    base_day = prev_month_end(day)
    week_day = sessions_ago(day, 5)
    base_lv, week_lv = {}, {}
    try:
        time.sleep(0.4)
        base_lv = sector_levels(base_day)
    except Exception as e:
        print("sector base failed", e, file=sys.stderr)
    try:
        time.sleep(0.4)
        if week_day:
            week_lv = sector_levels(week_day)
    except Exception as e:
        print("sector week failed", e, file=sys.stderr)
    sector_rows = []
    for s in sectors:
        row = dict(s)
        old = base_lv.get(s["index"])
        wk = week_lv.get(s["index"])
        row["mtd"] = round((s["last"] / old - 1) * 100, 2) if old else None
        row["week"] = round((s["last"] / wk - 1) * 100, 2) if wk else None
        leaders = [q for q in quotes if ind.get(q["code"]) and ind[q["code"]] in s["name"] and q["value"] >= 1e8]
        leaders.sort(key=lambda q: -q["pct"])
        row["leaders"] = [{"name": q["name"], "code": q["code"], "pct": q["pct"]} for q in leaders[:3]]
        sector_rows.append(row)
    today_up = sorted(sector_rows, key=lambda r: -r["pct"])[:5]
    mtd_rows = [r for r in sector_rows if r["mtd"] is not None]
    mtd_up = sorted(mtd_rows, key=lambda r: -r["mtd"])[:3]
    mtd_dn = sorted(mtd_rows, key=lambda r: r["mtd"])[:4]
    inst = {"ready": False, "date": None}
    try:
        time.sleep(0.4)
        totals = bfi(day)
        foreign, trust = t86_top(day, quotes)
        inst = {
            "ready": True, "date": day,
            "foreign_yi": yi(totals["foreign"]), "trust_yi": yi(totals["trust"]),
            "total_yi": yi(totals["total"]),
            "foreign": foreign, "trust": trust,
        }
    except Exception as e:
        print("inst not ready:", e, file=sys.stderr)
    prev = load_json(os.path.join(ROOT, "data", "close.json")) or {}
    if not inst.get("ready") and prev.get("date") == day and (prev.get("inst") or {}).get("ready"):
        inst = prev["inst"]
    etf = {}
    by = {q["code"]: q for q in quotes}
    for code in ETFS:
        q = by.get(code)
        if not q:
            # ETFs were excluded from the gainer universe (00*). Read them from the raw table via a second pass.
            continue
        etf[code] = q
    etf = etf_from_table(d, etf)
    worst = sorted(sector_rows, key=lambda r: r["pct"])[0]
    best = today_up[0]
    best_word = "漲幅最大" if best["pct"] > 0 else "跌幅最小"
    worst_word = "跌幅最大" if worst["pct"] < 0 else "漲幅最小"
    summary = "加權收 %s，%s %s 點（%s%%），成交值 %s 億元。櫃買收 %s，%s %s 點（%s%%）。%s類股為%s %s%%，%s為%s %s%%。" % (
        fmt_num(taiex["last"], 2), "漲" if taiex["chg"] >= 0 else "跌", fmt_num(abs(taiex["chg"]), 2), usigned(taiex["pct"]),
        fmt_num(value or 0, 2),
        fmt_num(otc["last"], 2), "漲" if otc["chg"] >= 0 else "跌", fmt_num(abs(otc["chg"]), 2), usigned(otc["pct"]),
        best_word, best["name"], usigned(best["pct"]), worst_word, worst["name"], usigned(worst["pct"]),
    )
    report = {
        "updated": taipei_today().isoformat(timespec="seconds"),
        "date": day,
        "label": label(day),
        "us_label": label(prev_weekday(day)),
        "us_date": prev_weekday(day),
        "summary": summary,
        "market": {
            "taiex": {"name": "加權指數", "date": day, "last": taiex["last"], "chg": taiex["chg"], "pct": taiex["pct"], "value_yi": value},
            "otc": {"name": "櫃買指數", "date": otc["date"], "last": otc["last"], "chg": otc["chg"], "pct": otc["pct"]},
        },
        "strong": {
            "date": day,
            "method": "上市與上櫃普通股（4 碼，不含 ETF），成交金額 3 億元以上，依漲跌幅由高到低取前五。漲幅 9.5% 以上標示漲停。",
            "items": strong,
        },
        "sectors": {
            "date": day,
            "base": base_day,
            "week": week_day,
            "today": [{"name": r["name"], "pct": r["pct"], "last": r["last"], "leaders": r["leaders"]} for r in today_up],
            "mtd": [{"name": r["name"], "pct": r["mtd"], "week": r["week"], "last": r["last"]} for r in mtd_up],
            "weak": [{"name": r["name"], "pct": r["mtd"], "week": r["week"], "last": r["last"]} for r in mtd_dn],
        },
        "inst": inst,
        "etf": {c: {"name": etf[c]["name"], "last": etf[c]["last"], "prev": etf[c]["prev"],
                    "chg": etf[c]["chg"], "pct": etf[c]["pct"]} for c in ETFS if c in etf},
    }
    return report


def prev_month_end(day):
    d = datetime.datetime.strptime(day, "%Y%m%d").replace(day=1) - datetime.timedelta(days=1)
    while d.weekday() >= 5:
        d -= datetime.timedelta(days=1)
    return d.strftime("%Y%m%d")


def sessions_ago(day, n):
    """Nth prior weekday. Holidays can make this a non-session; the fetch then fails soft."""
    d = datetime.datetime.strptime(day, "%Y%m%d")
    left = n
    while left:
        d -= datetime.timedelta(days=1)
        if d.weekday() < 5:
            left -= 1
    return d.strftime("%Y%m%d")


def etf_from_table(d, found):
    t = table(d, lambda title, fields: "證券代號" in fields and "收盤價" in fields)
    if not t:
        return found
    want = set(ETFS) - set(found)
    for row in t["data"]:
        code = str(row[0]).strip()
        if code not in want:
            continue
        last, diff = fnum(row[8]), fnum(row[10])
        if last is None or diff is None:
            continue
        chg = round(sign_of(row[9]) * diff, 2)
        found[code] = {"code": code, "name": str(row[1]).strip(), "last": last, "chg": chg,
                       "prev": round(last - chg, 4), "pct": round(chg / (last - chg) * 100, 2) if last != chg else None}
    return found


def fmt_num(n, d):
    return format(abs(float(n)), ",.%df" % d)


def usigned(n, d=2):
    n = float(n)
    sign = "+" if n > 0 else ("−" if n < 0 else "")
    return sign + format(abs(n), ".%df" % d)


def write_outputs(report, day):
    data = os.path.join(ROOT, "data")
    save_json(os.path.join(data, "close.json"), report)
    out = os.environ.get("OUT_DIR")
    if out:
        save_json(os.path.join(out, "close.json"), report)
    quotes = {}
    for code, q in report["etf"].items():
        quotes[code] = {"name": q["name"], "last": q["last"], "prev": q["prev"], "chg": q["chg"], "pct": q["pct"],
                        "date": day, "time": "13:30:00", "src": "twse-close"}
    etf_doc = {"updated": report["updated"], "codes": ETFS, "quotes": quotes}
    save_json(os.path.join(data, "etf.json"), etf_doc)
    div_path = os.path.join(data, "etf_div.json")
    div = load_json(div_path) or {"items": {}}
    div, _ = roll_div(div, day)
    save_json(div_path, div)
    report["div"] = div.get("items") or {}
    save_json(os.path.join(data, "close.json"), report)
    if out:
        save_json(os.path.join(out, "close.json"), report)
    print("close", day, "taiex", report["market"]["taiex"]["last"], "inst", report["inst"].get("ready"),
          "strong", [s["code"] for s in report["strong"]["items"]])


def push_main():
    files = ["data/close.json", "data/etf.json", "data/etf_div.json"]
    subprocess.call(["git", "config", "user.email", "41898208+github-actions[bot]@users.noreply.github.com"], cwd=ROOT)
    subprocess.call(["git", "config", "user.name", "github-actions[bot]"], cwd=ROOT)
    subprocess.check_call(["git", "add", *files], cwd=ROOT)
    if subprocess.call(["git", "diff", "--cached", "--quiet"], cwd=ROOT) == 0:
        print("close unchanged")
        return
    day = ymd()
    subprocess.check_call(["git", "commit", "-q", "-m", "data: close %s" % day], cwd=ROOT)
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    repo = os.environ.get("GITHUB_REPOSITORY", "laskin77/market-dashboard")
    if not token:
        print("no token, skip close push", file=sys.stderr)
        return
    url = "https://x-access-token:%s@github.com/%s.git" % (token, repo)
    try:
        subprocess.check_call(["git", "pull", "--rebase", "--autostash", url, "main"], cwd=ROOT)
    except subprocess.CalledProcessError as e:
        print("close rebase failed", e, file=sys.stderr)
    subprocess.check_call(["git", "push", url, "HEAD:main"], cwd=ROOT)
    print("close pushed", day)


def already_done(day):
    cur = load_json(os.path.join(ROOT, "data", "close.json"))
    if not cur or cur.get("date") != day:
        return False
    inst = cur.get("inst") or {}
    # After 17:00 keep retrying until institutional numbers are in.
    hm = taipei_today().hour * 60 + taipei_today().minute
    if hm >= 16 * 60 + 30 and not inst.get("ready"):
        return False
    return bool(cur.get("market"))


def main():
    day = os.environ.get("CLOSE_DATE") or ymd()
    if already_done(day) and os.environ.get("CLOSE_FORCE") != "1":
        print("close already complete", day)
        cur = load_json(os.path.join(ROOT, "data", "close.json"))
        out = os.environ.get("OUT_DIR")
        if out and cur:
            save_json(os.path.join(out, "close.json"), cur)
        return 0
    try:
        report = build(day)
    except RuntimeError as e:
        print("close not ready:", e, file=sys.stderr)
        return 0
    except Exception as e:
        print("close build failed:", e, file=sys.stderr)
        return 2
    write_outputs(report, day)
    if os.environ.get("CLOSE_PUSH") == "1":
        try:
            push_main()
        except Exception as e:
            print("close push failed:", e, file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
