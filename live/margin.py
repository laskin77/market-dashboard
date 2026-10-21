#!/usr/bin/env python3
"""大盤融資維持率 → data/margin.json
Usage: python3 live/margin.py [YYYYMMDD]   (run after ~21:30 Asia/Taipei)
Primary: 玩股網 WantGoo (/stock/0000A|twoA/margin-trading/historical-lending-balance-long-term).
Fallback / cross-check: own estimate from TWSE MI_MARGN × MI_INDEX closes (上市 only).
"""
import sys, json, time, datetime as dt, urllib.request, http.cookiejar, os
TZ = dt.timezone(dt.timedelta(hours=8))
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/130 Safari/537.36"
cj = http.cookiejar.CookieJar()
op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))

def get(url, xhr=False, ref=None):
    h = {"User-Agent": UA}
    if xhr: h["X-Requested-With"] = "XMLHttpRequest"
    if ref: h["Referer"] = ref
    return op.open(urllib.request.Request(url, headers=h), timeout=30).read().decode("utf-8")

def wantgoo(mkt):  # mkt: 'taiex' -> 0000A, 'otc' -> twoA
    ref = f"https://www.wantgoo.com/stock/margin-trading/market-price/{mkt}"
    get(ref)  # obtain cookies
    rows = json.loads(get(f"https://www.wantgoo.com/stock/{'0000A' if mkt=='taiex' else 'twoA'}/margin-trading/historical-lending-balance-long-term", True, ref))
    out = {}
    for r in rows:
        d = dt.datetime.fromtimestamp(r["date"]/1000, TZ).strftime("%Y-%m-%d")
        out[d] = dict(ratio=round(r["marginRatio"]*100, 2), bal=r["lendingBalance"], last=r["lastLendingBalance"])
    return out

num = lambda s: float(str(s).replace(",", "") or 0)

def estimate_tse(ymd):
    m = json.loads(get(f"https://www.twse.com.tw/rwd/zh/marginTrading/MI_MARGN?date={ymd}&selectType=ALL&response=json"))
    if m.get("stat") != "OK": return None
    tabs = m["tables"]
    amt = next(r for r in tabs[0]["data"] if r[0].startswith("融資金額"))
    bal_k, last_k = num(amt[5]), num(amt[4])
    lots = {r[0].strip(): num(r[6]) for r in tabs[1]["data"]}
    time.sleep(3)
    q = json.loads(get(f"https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX?date={ymd}&type=ALLBUT0999&response=json"))
    close = {}
    for t in q.get("tables", []):
        f = t.get("fields", [])
        if "證券代號" in f and "收盤價" in f:
            i, j = f.index("證券代號"), f.index("收盤價")
            for r in t["data"]:
                try: close[r[i].strip()] = num(r[j])
                except ValueError: pass
    mv = sum(lots[c]*1000*close[c] for c in lots if close.get(c))
    miss = [c for c in lots if lots[c] and not close.get(c)]
    return dict(ratio=round(mv/(bal_k*1000)*100, 2), bal=bal_k, last=last_k, missing=len(miss))

def main():
    target = sys.argv[1] if len(sys.argv) > 1 else dt.datetime.now(TZ).strftime("%Y%m%d")
    tday = f"{target[:4]}-{target[4:6]}-{target[6:]}"
    res = {"target_date": tday}
    try:
        tse, otc = wantgoo("taiex"), wantgoo("otc")
    except Exception as e:
        tse, otc = {}, {}; res["wantgoo_error"] = str(e)
    est = None
    try: est = estimate_tse(target)
    except Exception as e: res["estimate_error"] = str(e)
    if tday in tse:
        d, w = tday, tse[tday]
        res.update(date=d, ratio_tse=w["ratio"], source="玩股網 WantGoo（published）", method="玩股網公布之大盤融資維持率（上市；上櫃另列）")
        bal, last = w["bal"], w["last"]
        if est: res["estimate_crosscheck"] = {"ratio_tse": est["ratio"], "diff_pt": round(est["ratio"]-w["ratio"], 2)}
    elif est:
        d = tday
        res.update(date=d, ratio_tse=est["ratio"], source="自行估算（TWSE MI_MARGN × MI_INDEX 收盤價）— ESTIMATE",
                   method="Σ(各股融資餘額張×1000×收盤價)/融資金額(仟元×1000)；上市含ETF；無收盤價者略去(%d檔)" % est["missing"])
        bal, last = est["bal"], est["last"]
    elif tse:
        d = max(tse); w = tse[d]
        res.update(date=d, ratio_tse=w["ratio"], source="玩股網 WantGoo（published；目標日尚未公布，顯示最新）", method="玩股網公布值")
        bal, last = w["bal"], w["last"]
    else:
        sys.exit("no data: " + json.dumps(res, ensure_ascii=False))
    if d in otc:
        res["ratio_otc"] = otc[d]["ratio"]
        tb = bal + otc[d]["bal"]
        if d in tse:
            res["ratio_all"] = round((tse[d]["ratio"]*tse[d]["bal"] + otc[d]["ratio"]*otc[d]["bal"]) / tb, 2)
        res["margin_balance_all_yi"] = round(tb/1e5, 2)
    res["margin_balance_yi"] = round(bal/1e5, 2)       # 仟元 → 億元 (上市)
    res["margin_change_yi"] = round((bal-last)/1e5, 2)
    hist = sorted((k, v["ratio"]) for k, v in tse.items() if k <= d)[-20:]
    if d not in dict(hist): hist = (hist + [(d, res["ratio_tse"])])[-20:]
    res["history"] = [list(h) for h in hist]
    if otc: res["history_otc"] = [[k, otc[k]["ratio"]] for k, _ in hist if k in otc]
    blend_history(res)
    res["generated_at"] = dt.datetime.now(TZ).isoformat(timespec="seconds")
    os.makedirs(os.path.join(ROOT, "data"), exist_ok=True)
    with open(os.path.join(ROOT, "data", "margin.json"), "w", encoding="utf-8") as f:
        json.dump(res, f, ensure_ascii=False, indent=1)
        f.write("\n")
    print(json.dumps({k: res[k] for k in ("date", "ratio_tse", "ratio_otc", "margin_balance_yi", "margin_change_yi", "source") if k in res}, ensure_ascii=False))

def blend_history(res):
    """WantGoo is often blocked here. Keep the sparkline from the previous file."""
    path = os.path.join(ROOT, "data", "margin.json")
    if len(res.get("history") or []) >= 5 or not os.path.exists(path):
        return
    try:
        prev = json.load(open(path, encoding="utf-8"))
    except Exception:
        return
    older = [h for h in prev.get("history") or [] if h[0] != res.get("date")]
    if not older:
        return
    res["history"] = sorted(older + [[res["date"], res["ratio_tse"]]])[-20:]
    if "history_otc" not in res and prev.get("history_otc"):
        res["history_otc"] = [h for h in prev["history_otc"] if h[0] <= res["date"]][-20:]

def snippet(r):
    ys = [h[1] for h in r["history"]]; lo, hi = min(ys+[130]) if False else min(ys), max(ys)
    W, H = 160, 36; n = len(ys); rng = (hi-lo) or 1
    pts = " ".join(f"{i*W/(n-1 if n>1 else 1):.1f},{H-2-(y-lo)/rng*(H-4):.1f}" for i, y in enumerate(ys))
    ch = r["margin_change_yi"]; cls = "up" if ch > 0 else "dn" if ch < 0 else ""
    trend = "up" if len(ys) > 1 and ys[-1] >= ys[-2] else "dn"
    otc = f'　上櫃 {r["ratio_otc"]:.2f}%' if "ratio_otc" in r else ""
    est = "估算" if "ESTIMATE" in r["source"] else "玩股網"
    return f'''<style>.mg-spark{{display:block;margin:4px 0}}.mg-spark polyline{{fill:none;stroke:#4286f5;stroke-width:1.5}}</style>
<div class="card"><div class="cn">融資維持率（上市）</div>
<div class="cv num {trend}">{r["ratio_tse"]:.2f}%</div>
<div class="cc num {cls}">融資餘額 {r["margin_balance_yi"]:,.2f} 億　{"+" if ch>0 else "−" if ch<0 else ""}{abs(ch):,.2f} 億</div>
<svg class="mg-spark" width="100%" height="{H}" viewBox="0 0 {W} {H}" preserveAspectRatio="none"><polyline points="{pts}"/></svg>
<div class="note">{r["date"][5:].replace("-","/")}{otc}｜近{n}日 {lo:.1f}–{hi:.1f}%｜130% 為追繳線｜來源：{est}</div></div>
'''

if __name__ == "__main__": main()
