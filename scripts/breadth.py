#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
breadth.py — A股市场宽度 & 反弹/反转 判定器

用法:
    python breadth.py                 # 完整报告 + 追加历史
    python breadth.py --no-holdings   # 不看持仓
    python breadth.py --save-only     # 只记录，不打印报告
    python breadth.py --json out.json # 同时导出 JSON

数据源:
    全A快照   push2.eastmoney.com/api/qt/clist/get
    报价      qt.gtimg.cn/q=
"""

import argparse
import json
import os
import sys
import tempfile
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _writable(d):
    try:
        os.makedirs(d, exist_ok=True)
        probe = os.path.join(d, ".w_probe")
        with open(probe, "w", encoding="utf-8") as f:
            f.write("1")
        os.remove(probe)
        return True
    except Exception:
        return False


def pick_data_dir():
    """技能目录通常不可写（沙箱），逐级回退到可写位置。"""
    cands = []
    env = os.environ.get("BREADTH_DATA_DIR")
    if env:
        cands.append(env)
    cands.append(os.path.join(BASE, "data"))
    cands.append(os.path.join(os.getcwd(), "breadth-data"))
    cands.append(os.path.join(os.path.expanduser("~"), "Documents",
                              "deepseek-harness", "default-workspace",
                              "breadth-data"))
    cands.append(os.path.join(tempfile.gettempdir(), "dsh-breadth"))
    for c in cands:
        if _writable(c):
            return c
    return tempfile.gettempdir()


DATA = pick_data_dir()
HIST_PATH = os.path.join(DATA, "history.json")
HOLD_PATH = os.path.join(DATA, "holdings.json")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")

EM_URL = ("https://push2.eastmoney.com/api/qt/clist/get?pn={pn}&pz=100&po=1&np=1"
          "&fltt=2&invt=2&fid=f3&fs=m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23"
          "&fields=f2,f3,f5,f6,f12,f14")

DEFAULT_HOLDINGS = [
    {"code": "sh515880", "name": "通信ETF",    "qty": 76300, "cost": 0.8370},
    {"code": "sz159516", "name": "半导体设备",  "qty": 7000,  "cost": 0.9887},
    {"code": "sh601328", "name": "交通银行",    "qty": 1000,  "cost": 6.8789},
    {"code": "sh512800", "name": "银行ETF",     "qty": 0,     "cost": 0.0},
]

INDEXES = [("sh000001", "上证"), ("sz399006", "创业板"), ("sh000688", "科创50")]


# ---------------------------------------------------------------- http

def http_get(url, timeout=15, referer="https://quote.eastmoney.com/"):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Referer": referer})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def fetch_page(pn):
    try:
        raw = http_get(EM_URL.format(pn=pn))
        js = json.loads(raw.decode("utf-8", "ignore"))
        d = js.get("data")
        if not d:
            return 0, []
        return d.get("total", 0) or 0, (d.get("diff") or [])
    except Exception:
        return 0, []


def fetch_all_a():
    total, first = fetch_page(1)
    if not first:
        raise RuntimeError("东财全A快照拉取失败（第1页为空）")
    pages = max(1, (total + 99) // 100)
    rows = list(first)
    if pages > 1:
        with ThreadPoolExecutor(max_workers=8) as ex:
            for _t, batch in ex.map(fetch_page, range(2, pages + 1)):
                rows.extend(batch)
    seen, uniq = set(), []
    for r in rows:
        c = r.get("f12")
        if c and c not in seen:
            seen.add(c)
            uniq.append(r)
    return total, uniq


def fetch_quotes(codes):
    """腾讯行情。返回 {code: dict}"""
    out = {}
    if not codes:
        return out
    try:
        raw = http_get("http://qt.gtimg.cn/q=" + ",".join(codes),
                       referer="http://gu.qq.com/")
        txt = raw.decode("gbk", "ignore")
    except Exception:
        return out
    for line in txt.split(";"):
        line = line.strip()
        if not line.startswith("v_"):
            continue
        try:
            key, payload = line.split("=", 1)
            code = key[2:].strip()
            f = payload.strip().strip('"').split("~")
            if len(f) < 40:
                continue
            out[code] = {
                "name": f[1],
                "price": float(f[3] or 0),
                "prev": float(f[4] or 0),
                "open": float(f[5] or 0),
                "pct": float(f[32] or 0),
                "high": float(f[33] or 0),
                "low": float(f[34] or 0),
                "amount": float(f[37] or 0) * 10000.0,   # 万元 -> 元
            }
        except Exception:
            continue
    return out


# ---------------------------------------------------------------- 计算

def limit_threshold(code, name):
    n = (name or "").upper()
    if "ST" in n:
        return 4.8
    pre = code[:2]
    if pre in ("30", "68"):
        return 19.5
    return 9.7


def market_progress(now):
    """A股交易时间进度 0..1（扣除午休）。"""
    t = now.hour * 60 + now.minute
    am_s, am_e = 9 * 60 + 30, 11 * 60 + 30
    pm_s, pm_e = 13 * 60, 15 * 60
    if t <= am_s:
        return 0.0
    if t <= am_e:
        return (t - am_s) / 240.0
    if t <= pm_s:
        return 0.5
    if t <= pm_e:
        return (120.0 + (t - pm_s)) / 240.0
    return 1.0


KLINE_URL = ("https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
             "?param={code},day,,,{n},qfq")


def fetch_daily(code, n=80):
    """返回 [(date_str, close), ...]，日期升序。失败返回 []。"""
    try:
        raw = http_get(KLINE_URL.format(code=code, n=n), timeout=20,
                       referer="https://gu.qq.com/")
        js = json.loads(raw.decode("utf-8", "ignore"))
        d = (js.get("data") or {}).get(code) or {}
        arr = d.get("qfqday") or d.get("day") or []
        out = []
        for x in arr:
            if len(x) >= 3:
                try:
                    out.append((str(x[0]), float(x[2])))
                except Exception:
                    continue
        return out
    except Exception:
        return []


def ma(seq, n):
    if seq is None or len(seq) < n:
        return None
    return sum(seq[-n:]) / n


def ema(seq, n):
    if seq is None or len(seq) < n:
        return None
    k = 2.0 / (n + 1)
    e = sum(seq[:n]) / float(n)
    for x in seq[n:]:
        e = x * k + e * (1 - k)
    return e


def technical_vote_from(code, live_price, today):
    """日线技术面票：多 / 空 / 中。

    规则（写死、可复现）：
      收盘 > MA20 且 MA5 > MA10  -> 多
      收盘 < MA20 且 MA5 < MA10  -> 空
      其他                        -> 中
    """
    rows = fetch_daily(code, 80)
    if not rows:
        return "中", {"ma5": None, "ma10": None, "ma20": None, "px": live_price,
                      "note": "K线拉取失败"}
    closes = [c for d, c in rows if d != today]
    if not closes:
        closes = [c for _d, c in rows]
    px = live_price if live_price else (closes[-1] if closes else None)
    if px is None:
        return "中", {"ma5": None, "ma10": None, "ma20": None, "px": None,
                      "note": "价格缺失"}
    s = list(closes) + [px]
    m5, m10, m20 = ma(s, 5), ma(s, 10), ma(s, 20)
    if None in (m5, m10, m20):
        return "中", {"ma5": m5, "ma10": m10, "ma20": m20, "px": px,
                      "note": "均线样本不足"}
    if px > m20 and m5 > m10:
        v = "多"
    elif px < m20 and m5 < m10:
        v = "空"
    else:
        v = "中"
    return v, {"ma5": m5, "ma10": m10, "ma20": m20, "px": px, "note": ""}


def fund_vote_from_score(total):
    """资金面票：0-4 空 / 5-8 中 / 9-12 多。"""
    if total <= 4:
        return "空"
    if total <= 8:
        return "中"
    return "多"


def combine_votes(fund, tech):
    """两票制：同向则执行；冲突则「准备X，等」。"""
    if fund == tech:
        if fund == "多":
            return "多", "同向多 → 可按计划加/持有"
        if fund == "空":
            return "空", "同向空 → 不加仓，占比高的减"
        return "中", "双中性 → 不动"
    if fund == "中":
        return tech, f"资金中性 → 跟随技术面（{tech}）"
    if tech == "中":
        return fund, f"技术中性 → 跟随资金面（{fund}）"
    # 冲突：一多一空
    wait_side = "空" if fund == "空" else "多"
    return "等", f"冲突（资金{fund} / 技术{tech}）→ 准备{wait_side}，等两边同向"


def compute_breadth(rows):
    up = down = flat = 0
    amt_total = amt_up = vol_total = vol_up = 0.0
    lim_up = lim_dn = 0
    for r in rows:
        try:
            pct = float(r.get("f3") or 0)
            amt = float(r.get("f6") or 0)
            vol = float(r.get("f5") or 0)
        except Exception:
            continue
        code = str(r.get("f12") or "")
        name = str(r.get("f14") or "")
        amt_total += amt
        vol_total += vol
        if pct > 0:
            up += 1
            amt_up += amt
            vol_up += vol
        elif pct < 0:
            down += 1
        else:
            flat += 1
        th = limit_threshold(code, name)
        if pct >= th:
            lim_up += 1
        elif pct <= -th:
            lim_dn += 1

    valid = up + down + flat
    up_ratio = up / valid if valid else 0.0
    amt_ratio = amt_up / amt_total if amt_total else 0.0
    vol_ratio = vol_up / vol_total if vol_total else 0.0

    amt_dn = amt_total - amt_up
    vol_dn = vol_total - vol_up
    trin_amt = (up / down) / (amt_up / amt_dn) if (down and amt_dn and amt_up) else None
    trin_vol = (up / down) / (vol_up / vol_dn) if (down and vol_dn and vol_up) else None

    return {
        "up": up, "down": down, "flat": flat, "valid": valid,
        "up_ratio": up_ratio,
        "amt_ratio": amt_ratio,
        "vol_ratio": vol_ratio,
        "gap_amt": (amt_ratio - up_ratio) * 100.0,
        "gap_vol": (vol_ratio - up_ratio) * 100.0,
        "trin_amt": trin_amt,
        "trin_vol": trin_vol,
        "limit_up": lim_up,
        "limit_down": lim_dn,
        "amount_yi": amt_total / 1e8,
    }


# ---------------------------------------------------------------- 历史

def load_history():
    if not os.path.exists(HIST_PATH):
        return []
    try:
        with open(HIST_PATH, "r", encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, list) else []
    except Exception:
        return []


def save_history(hist):
    os.makedirs(DATA, exist_ok=True)
    tmp = HIST_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(hist, f, ensure_ascii=False, indent=1)
    os.replace(tmp, HIST_PATH)


def pctile_rank(series, value):
    """value 在 series 中的百分位(0-100)。样本不足返回 None。"""
    s = [x for x in series if x is not None]
    if len(s) < 5:
        return None
    below = sum(1 for x in s if x < value)
    return 100.0 * below / len(s)


def win_stats(hist, key, n):
    s = [h.get(key) for h in hist[-n:]]
    s = [x for x in s if x is not None]
    if not s:
        return None
    return sum(s) / len(s)


def full_day_avg(hist, n=5):
    """只用「完整交易日」的记录算均值，盘中记录不计入基准。"""
    vals = [h.get("amount_yi") for h in hist
            if (h.get("progress") if h.get("progress") is not None else 1.0) >= 0.99
            and h.get("amount_yi")]
    if not vals:
        return None
    vals = vals[-n:]
    return sum(vals) / len(vals)


def compute_trend(hist):
    """10 日 TRIN 均线 + MCL（McClellan Oscillator = EMA19(AD) − EMA39(AD)）。"""
    trin = [h.get("trin_amt") for h in hist if h.get("trin_amt") is not None]
    trin_ma10 = (sum(trin[-10:]) / 10.0) if len(trin) >= 10 else None

    ad = []
    for h in hist:
        if h.get("up") is not None and h.get("down") is not None:
            ad.append(h["up"] - h["down"])
    mcl = None
    if len(ad) >= 40:
        e19, e39 = ema(ad, 19), ema(ad, 39)
        if e19 is not None and e39 is not None:
            mcl = e19 - e39
    return {"trin_ma10": trin_ma10, "mcl": mcl,
            "trin_n": len(trin), "ad_n": len(ad)}


# ---------------------------------------------------------------- 打分

def score_all(ind, hist, progress=1.0):
    items = []

    r = ind["up_ratio"] * 100
    if r >= 60:
        items.append(("涨家占比", f"{r:.1f}%", 2, "普涨"))
    elif r >= 40:
        items.append(("涨家占比", f"{r:.1f}%", 1, "中性"))
    else:
        items.append(("涨家占比", f"{r:.1f}%", 0, "普跌"))

    g = ind["gap_amt"]
    if g <= 4:
        items.append(("额价差", f"{g:+.1f}pp", 2, "普惠，非抱团"))
    elif g <= 10:
        items.append(("额价差", f"{g:+.1f}pp", 1, "偏抱团"))
    else:
        items.append(("额价差", f"{g:+.1f}pp", 0, "强抱团=权重拉指数"))

    t = ind["trin_amt"]
    ur = ind["up_ratio"]
    if t is None:
        items.append(("TRIN_额", "n/a", 1, "数据缺失，按中性"))
    elif ur >= 0.55:
        # 普涨：低 TRIN = 资金坚决做多
        if t <= 0.70:
            items.append(("TRIN_额", f"{t:.2f}", 2, "普涨+资金坚决"))
        elif t <= 1.00:
            items.append(("TRIN_额", f"{t:.2f}", 1, "普涨但资金一般"))
        else:
            items.append(("TRIN_额", f"{t:.2f}", 0, "普涨但资金在流出"))
    else:
        # 普跌：低 TRIN = 少数票吸走资金（极端抱团）
        if t <= 0.50:
            items.append(("TRIN_额", f"{t:.2f}", 1, "极端抱团：少数票吸走资金"))
        elif t <= 1.20:
            items.append(("TRIN_额", f"{t:.2f}", 1, "中性"))
        else:
            items.append(("TRIN_额", f"{t:.2f}", 0, "资金全面撤离"))

    lu, ld = ind["limit_up"], ind["limit_down"]
    ratio = (lu / ld) if ld else (99.0 if lu else 1.0)
    if ratio >= 2.0:
        items.append(("涨停/跌停", f"{lu}/{ld}", 2, "情绪偏强"))
    elif ratio >= 0.8:
        items.append(("涨停/跌停", f"{lu}/{ld}", 1, "分化"))
    else:
        items.append(("涨停/跌停", f"{lu}/{ld}", 0, "跌停多，情绪差"))

    avg5 = full_day_avg(hist, 5)
    if avg5 and progress > 0.10:
        equiv = ind["amount_yi"] / progress
        mult = equiv / avg5
        if mult >= 1.10:
            items.append(("成交额/5日均", f"×{mult:.2f}", 2, "放量"))
        elif mult >= 0.90:
            items.append(("成交额/5日均", f"×{mult:.2f}", 1, "平量"))
        else:
            items.append(("成交额/5日均", f"×{mult:.2f}", 0, "缩量=假反弹"))
        amount_mult = mult
    else:
        items.append(("成交额/5日均", "样本不足", 1, "首次运行，按中性"))
        amount_mult = None

    prev = hist[-1]["up_ratio"] if hist else None
    if prev is None:
        items.append(("连续性", "无从比较", 1, "首次运行"))
        streak = 0
    elif ind["up_ratio"] > prev:
        streak = 1
        if len(hist) >= 2 and hist[-1].get("up_ratio", 0) > hist[-2].get("up_ratio", 0):
            streak = 2
        items.append(("连续性", f"升 {streak} 天", 2 if streak >= 2 else 1,
                      "连续改善" if streak >= 2 else "首日改善"))
    else:
        streak = 0
        items.append(("连续性", "未改善", 0, "广度还在恶化"))

    total = sum(i[2] for i in items)
    if total >= 10:
        verdict, tag = "真反转", "GOOD"
    elif total >= 7:
        verdict, tag = "反弹进行中", "MAYBE"
    elif total >= 4:
        verdict, tag = "弱反弹 / 分化", "WEAK"
    else:
        verdict, tag = "假反弹 / 下跌中继", "BAD"
    return total, verdict, tag, items, amount_mult, streak


# ---------------------------------------------------------------- 报告

def build_report(ind, hist, total, verdict, tag, items, amount_mult, streak,
                 quotes, indexes, holdings, stamp, sample_n, total_stocks,
                 cash=0.0, progress=1.0, extras=None):
    L = []
    w = L.append
    w("═" * 52)
    w(f"  A股宽度日报  {stamp}")
    w("═" * 52)
    w("")
    w("【一、涨跌分布】")
    w(f"  上涨 {ind['up']:,} | 下跌 {ind['down']:,} | 平盘 {ind['flat']:,}"
      f"   (有效 {ind['valid']:,} / 全A {total_stocks:,})")
    w(f"  涨家占比 {ind['up_ratio']*100:5.1f}%   涨额占比 {ind['amt_ratio']*100:5.1f}%"
      f"   涨量占比 {ind['vol_ratio']*100:5.1f}%")
    w(f"  额价差 {ind['gap_amt']:+.1f}pp      量价差 {ind['gap_vol']:+.1f}pp")
    ta = f"{ind['trin_amt']:.2f}" if ind['trin_amt'] is not None else "n/a"
    tv = f"{ind['trin_vol']:.2f}" if ind['trin_vol'] is not None else "n/a"
    w(f"  TRIN_额 {ta}      TRIN_量 {tv}")
    w(f"  涨停 {ind['limit_up']} / 跌停 {ind['limit_down']}")
    pct_txt = f"交易进度 {progress*100:.0f}%"
    if amount_mult:
        equiv = ind["amount_yi"] / progress if progress > 0.10 else ind["amount_yi"]
        w(f"  全A成交额 {ind['amount_yi']:,.0f} 亿  ({pct_txt}，"
          f"全天等效 {equiv:,.0f} 亿，5日均 ×{amount_mult:.2f})")
    else:
        w(f"  全A成交额 {ind['amount_yi']:,.0f} 亿  ({pct_txt}，5日均样本不足)")
    if progress < 0.99:
        w("  ⚠ 盘中数据：量能按交易进度线性折算，误差约 ±10%；14:45 后最准。")
    w("")

    w(f"【二、历史分位】(样本 {sample_n} 天)")
    if sample_n >= 5:
        for k, label in (("up_ratio", "涨家占比"), ("trin_amt", "TRIN_额")):
            vals = [h.get(k) for h in hist if h.get(k) is not None]
            cur = ind.get(k)
            if cur is None or not vals:
                continue
            p20 = pctile_rank(vals[-20:], cur)
            p60 = pctile_rank(vals[-60:], cur)
            p250 = pctile_rank(vals[-250:], cur)
            fmt = lambda p: "n/a" if p is None else f"{p:.1f}"
            w(f"  {label}  P20 {fmt(p20)} / P60 {fmt(p60)} / P250 {fmt(p250)}")
    else:
        w("  ⚠ 历史样本不足（需 ≥5 天），本次只做阈值判断，不出分位。")
    w("")

    w(f"【三、六项打分】{total} / 12  →  {verdict}")
    for name, val, sc, why in items:
        mark = "✅" if sc == 2 else ("⚠️" if sc == 1 else "❌")
        w(f"  {mark} {name:<12} {val:<12} {sc}分  ({why})")
    w("")

    if indexes:
        w("【四、指数】")
        for code, label in INDEXES:
            q = indexes.get(code)
            if not q:
                continue
            w(f"  {label:<6} {q['price']:>10.2f}  {q['pct']:>+6.2f}%")
        w("")

    if holdings:
        w("【五、持仓】")
        total_val = 0.0
        cached = []
        for h in holdings:
            q = quotes.get(h["code"])
            if not q:
                continue
            qty = h.get("qty", 0) or 0
            cost = h.get("cost", 0) or 0
            val = qty * q["price"]
            total_val += val
            pnl = ((q["price"] / cost - 1) * 100) if cost else None
            cached.append((h.get("name") or q["name"], q, qty, cost, val, pnl))
        nav = total_val + cash
        for name, q, qty, cost, val, pnl in cached:
            share = (val / nav * 100) if nav else 0.0
            pnl_txt = f"{pnl:+.1f}%" if pnl is not None else "-"
            w(f"  {name}  {q['price']:.3f}  {q['pct']:+.2f}%  "
              f"{qty:,}股  {val:,.0f}元  成本{cost:.4f}  盈亏{pnl_txt}  "
              f"占比{share:.1f}%")
        w(f"  持仓 {total_val:,.0f} 元 | 现金 {cash:,.0f} 元 | 总资产 {nav:,.0f} 元")
        if nav:
            big = [n for n, q, qy, c, v, p in cached if v / nav > 0.5]
            top = max([v for _, _, _, _, v, _ in cached] or [0]) / nav
            if big:
                w(f"  ⚠ 单一持仓占比 >50%：{'、'.join(big)}"
                  f" —— 任何「假反弹」都会让你再亏一轮。")
            elif top < 0.30:
                w("  ✅ 单一持仓占比 <30%，可以等「真反转」再加仓。")
        w("")

    ex = extras or {}
    if ex.get("fund_vote"):
        fd = ex["tech_detail"] or {}
        w("【六、两票制】（资金面为大，技术面为小）")
        w(f"  资金面票  {ex['fund_vote']:<2}   （广度 {total}/12）")
        ma5, ma10, ma20 = fd.get("ma5"), fd.get("ma10"), fd.get("ma20")
        fmt = lambda x: "n/a" if x is None else f"{x:,.2f}"
        w(f"  技术面票  {ex['tech_vote']:<2}   （上证 {fmt(fd.get('px'))} | "
          f"MA5 {fmt(ma5)} / MA10 {fmt(ma10)} / MA20 {fmt(ma20)}）")
        fv = ex["final_vote"]
        mark = {"多": "🟢", "空": "🔴", "中": "⚪", "等": "⏸"}.get(fv, "⚪")
        w(f"  合  并    {mark} {fv}")
        w(f"  → {ex['final_reason']}")
        if fv == "等":
            w("  按「冲突则等」的纪律：今天不开新仓，等两边同向再动。")
        w("")

    tr = ex.get("trend") or {}
    if tr:
        w("【七、趋势指标】")
        t10 = tr.get("trin_ma10")
        if t10 is None:
            w(f"  10日 TRIN  样本不足（需 10 天，现有 {tr.get('trin_n', 0)} 天）")
        else:
            state = "超买" if t10 < 0.8 else ("超卖" if t10 > 1.2 else "中性")
            w(f"  10日 TRIN  {t10:.2f}  （{state}；<0.8 超买 / >1.2 超卖）")
        mcl = tr.get("mcl")
        if mcl is None:
            w(f"  MCL        样本不足（需 39 天，现有 {tr.get('ad_n', 0)} 天）")
        else:
            state = "广度强" if mcl > 0 else "广度弱"
            w(f"  MCL        {mcl:+.1f}  （{state}；= EMA19−EMA39 的涨跌差）")
        w("")

    w("【八、结论】")
    if ind["up_ratio"] < 0.25 and ind["gap_amt"] > 15:
        w("  ⚠ 极致分化：涨家占比 <25% 但涨额占比明显偏高，"
          "指数被少数权重/题材拉着，多数股票在跌。")
        w("     这种行情里「指数红」不等于「你的票红」。")
        w("")
    if tag == "GOOD":
        w("  ✅ 真反转：广度、量能、情绪三者共振。")
        w("     动作：持仓可继续持有；想加仓的，等回踩不破今日低点再加。")
    elif tag == "MAYBE":
        w("  🟡 反弹进行中：有改善，但还没确认。")
        w("     动作：先不动，等第二天广度继续改善、成交额继续放大。")
    elif tag == "WEAK":
        w("  🟠 弱反弹 / 分化：只有部分指标改善。")
        w("     动作：不加仓；持仓占比 >50% 的，反弹到今日高点附近减一部分。")
    else:
        w("  ❌ 假反弹 / 下跌中继：权重拉指数或情绪恐慌。")
        w("     动作：不抄底；持仓占比 >50% 的，必须减仓。")
    w("")
    w("  ⚠ 只判断「当前状态是真/假」，不预测点位与明天涨跌。")
    w("═" * 52)
    return "\n".join(L)


# ---------------------------------------------------------------- HTML

TAG_COLOR = {"GOOD": "#22c55e", "MAYBE": "#eab308",
             "WEAK": "#f97316", "BAD": "#ef4444"}


def _esc(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;"))


def _spark(hist, key, w=560, h=90, color="#60a5fa"):
    vals = [(h.get(key) or 0) for h in hist[-40:] if h.get(key) is not None]
    if len(vals) < 2:
        return ('<div style="color:#64748b;font-size:13px;padding:18px 0">'
                '历史样本不足（需 ≥2 天），暂不绘制曲线</div>')
    lo, hi = min(vals), max(vals)
    rng = (hi - lo) or 1.0
    n = len(vals)
    pts = []
    for i, v in enumerate(vals):
        x = 6 + i * (w - 12) / (n - 1)
        y = h - 10 - (v - lo) / rng * (h - 24)
        pts.append(f"{x:.1f},{y:.1f}")
    last_x, last_y = pts[-1].split(",")
    return (f'<svg viewBox="0 0 {w} {h}" width="100%" height="{h}" '
            f'preserveAspectRatio="none">'
            f'<polyline points="{" ".join(pts)}" fill="none" '
            f'stroke="{color}" stroke-width="2"/></svg>'
            f'<div style="font-size:12px;color:#64748b;margin-top:4px">'
            f'{n} 天 · 当前 {vals[-1]*100:.1f}% · 区间 '
            f'{lo*100:.1f}%–{hi*100:.1f}%</div>')


def render_html(ind, hist, total, verdict, tag, items, amount_mult, streak,
                quotes, indexes, holdings, stamp, sample_n, total_stocks,
                cash=0.0, progress=1.0, extras=None):
    color = TAG_COLOR.get(tag, "#94a3b8")
    P = []
    a = P.append

    a('<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">')
    a('<meta name="viewport" content="width=device-width,initial-scale=1">')
    a(f'<title>A股宽度 · {_esc(stamp)}</title>')
    a('<meta http-equiv="refresh" content="300">')
    a('<style>')
    a('*{box-sizing:border-box}body{margin:0;background:#0b0e14;'
      'color:#e2e8f0;font:15px/1.6 -apple-system,"Segoe UI",'
      '"Microsoft YaHei",sans-serif;padding:22px}')
    a('.wrap{max-width:1040px;margin:0 auto}')
    a('h1{font-size:19px;margin:0 0 4px;font-weight:600}')
    a('.sub{color:#64748b;font-size:13px;margin-bottom:20px}')
    a('.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));'
      'gap:12px;margin-bottom:18px}')
    a('.card{background:#141922;border:1px solid #1e2633;border-radius:10px;'
      'padding:14px 16px}')
    a('.k{color:#64748b;font-size:12px;letter-spacing:.02em}')
    a('.v{font-size:24px;font-weight:600;margin-top:4px;'
      'font-variant-numeric:tabular-nums}')
    a('.n{color:#94a3b8;font-size:12px;margin-top:2px}')
    a('.panel{background:#141922;border:1px solid #1e2633;border-radius:10px;'
      'padding:16px 18px;margin-bottom:16px}')
    a('.panel h2{font-size:14px;margin:0 0 12px;color:#94a3b8;font-weight:600;'
      'letter-spacing:.04em}')
    a('.bar{height:8px;background:#1e2633;border-radius:4px;overflow:hidden;'
      'margin-top:10px}')
    a('.bar>i{display:block;height:100%;border-radius:4px}')
    a('table{width:100%;border-collapse:collapse;font-size:14px}')
    a('td{padding:7px 0;border-bottom:1px solid #1a212c}')
    a('td:first-child{color:#94a3b8;white-space:nowrap}')
    a('td.num{text-align:right;font-variant-numeric:tabular-nums;'
      'white-space:nowrap}')
    a('.tag{display:inline-block;padding:1px 8px;border-radius:20px;'
      'font-size:12px;font-weight:600}')
    a('.verdict{border-left:3px solid ' + color + ';padding:12px 16px;'
      'background:#111823;border-radius:0 8px 8px 0}')
    a('.verdict b{color:' + color + ';font-size:17px}')
    a('.warn{color:#fbbf24;font-size:13px;margin-top:10px}')
    a('.foot{color:#475569;font-size:12px;margin-top:22px;text-align:center}')
    a('</style></head><body><div class="wrap">')

    a(f'<h1>A股宽度 · 反弹真假判定</h1>')
    a(f'<div class="sub">采样 {_esc(stamp)} · 有效 {total_stocks:,} 只 · '
      f'历史样本 {sample_n} 天</div>')

    # 大数字
    a('<div class="grid">')
    for k, v, n in [
        ("涨家占比", f"{ind['up_ratio']*100:.1f}%", f"{ind['up']:,} 涨 / "
         f"{ind['down']:,} 跌"),
        ("额价差", f"{ind['gap_amt']:+.1f}pp", "涨额占比−涨家占比"),
        ("TRIN_额", f"{ind['trin_amt']:.2f}" if ind['trin_amt'] is not None
         else "n/a", "<0.5 极端抱团"),
        ("涨停/跌停", f"{ind['limit_up']}/{ind['limit_down']}",
         "跌停多=情绪差"),
        ("全A成交额", f"{ind['amount_yi']:,.0f}亿",
         f"进度{progress*100:.0f}%" + (f" · ×{amount_mult:.2f}"
                                       if amount_mult else "")),
    ]:
        a(f'<div class="card"><div class="k">{k}</div>'
          f'<div class="v">{v}</div><div class="n">{n}</div></div>')
    a('</div>')

    # 结论
    a('<div class="panel"><h2>判定</h2>')
    a(f'<div class="verdict"><b>{_esc(verdict)}</b>'
      f'<span style="color:#64748b;font-size:13px"> · {total}/12 分</span>')
    a('<div class="bar">')
    a(f'<i style="width:{total/12*100:.0f}%;background:{color}"></i></div>')
    if ind["up_ratio"] < 0.25 and ind["gap_amt"] > 15:
        a('<div class="warn">⚠ 极致分化：涨家 &lt;25% 但涨额偏高，'
          '指数被少数权重/题材拉着。这种行情里「指数红」不等于「你的票红」。</div>')
    if progress < 0.99:
        a('<div class="warn">盘中数据：量能按交易进度线性折算，'
          '误差约 ±10%；14:45 后最准。</div>')
    a('</div></div>')

    # 两票制
    ex = extras or {}
    if ex.get("fund_vote"):
        fv = ex["final_vote"]
        vc = {"多": "#22c55e", "空": "#ef4444", "中": "#94a3b8",
              "等": "#eab308"}.get(fv, "#94a3b8")
        a('<div class="panel"><h2>两票制 · 资金面为大，技术面为小</h2>')
        a('<table>')
        a(f'<tr><td>资金面票</td><td class="num">'
          f'<b style="font-size:17px">{ex["fund_vote"]}</b></td>'
          f'<td class="num" style="color:#64748b">广度 {total}/12</td></tr>')
        fd = ex.get("tech_detail") or {}
        fmt = lambda x: "n/a" if x is None else f"{x:,.2f}"
        a(f'<tr><td>技术面票</td><td class="num">'
          f'<b style="font-size:17px">{ex["tech_vote"]}</b></td>'
          f'<td class="num" style="color:#64748b">上证 {fmt(fd.get("px"))} · '
          f'MA5 {fmt(fd.get("ma5"))} / MA20 {fmt(fd.get("ma20"))}</td></tr>')
        a(f'<tr><td><b>合并</b></td><td class="num">'
          f'<b style="font-size:20px;color:{vc}">{fv}</b></td>'
          f'<td class="num" style="color:#64748b">{_esc(ex["final_reason"])}'
          f'</td></tr>')
        a('</table>')
        if fv == "等":
            a('<div class="warn">⏸ 冲突 → 准备X，等。今天不开新仓，'
              '等资金面与技术面同向再动。</div>')
        a('</div>')

    # 趋势指标
    tr = ex.get("trend") or {}
    if tr:
        a('<div class="panel"><h2>趋势指标</h2><table>')
        t10 = tr.get("trin_ma10")
        if t10 is None:
            a(f'<tr><td>10日 TRIN</td><td class="num">样本不足</td>'
              f'<td class="num" style="color:#64748b">需 10 天，现有 '
              f'{tr.get("trin_n", 0)} 天</td></tr>')
        else:
            st = "超买" if t10 < 0.8 else ("超卖" if t10 > 1.2 else "中性")
            a(f'<tr><td>10日 TRIN</td><td class="num">{t10:.2f}</td>'
              f'<td class="num" style="color:#64748b">{st}'
              f'（&lt;0.8 超买 / &gt;1.2 超卖）</td></tr>')
        mcl = tr.get("mcl")
        if mcl is None:
            a(f'<tr><td>MCL</td><td class="num">样本不足</td>'
              f'<td class="num" style="color:#64748b">需 39 天，现有 '
              f'{tr.get("ad_n", 0)} 天</td></tr>')
        else:
            st = "广度强" if mcl > 0 else "广度弱"
            a(f'<tr><td>MCL</td><td class="num">{mcl:+.1f}</td>'
              f'<td class="num" style="color:#64748b">{st}'
              f'（EMA19−EMA39）</td></tr>')
        a('</table></div>')

    # 六项打分
    a('<div class="panel"><h2>六项打分</h2><table>')
    for name, val, sc, why in items:
        c = "#22c55e" if sc == 2 else ("#eab308" if sc == 1 else "#ef4444")
        a(f'<tr><td>{_esc(name)}</td>'
          f'<td class="num">{_esc(val)}</td>'
          f'<td class="num"><span class="tag" style="background:{c}1f;'
          f'color:{c}">{sc}分</span></td>'
          f'<td class="num" style="color:#64748b">{_esc(why)}</td></tr>')
    a('</table></div>')

    # 指数
    if indexes:
        a('<div class="panel"><h2>指数</h2><table>')
        for code, label in INDEXES:
            q = indexes.get(code)
            if not q:
                continue
            c = "#ef4444" if q["pct"] < 0 else "#22c55e"
            a(f'<tr><td>{label}</td><td class="num">{q["price"]:,.2f}</td>'
              f'<td class="num" style="color:{c}">{q["pct"]:+.2f}%</td>'
              f'<td></td></tr>')
        a('</table></div>')

    # 持仓
    if holdings:
        rows, tot = [], 0.0
        for h in holdings:
            q = quotes.get(h["code"])
            if not q:
                continue
            qty = h.get("qty", 0) or 0
            cost = h.get("cost", 0) or 0
            val = qty * q["price"]
            tot += val
            rows.append((h.get("name") or q["name"], q, qty, cost, val,
                         ((q["price"] / cost - 1) * 100) if cost else None))
        nav = tot + cash
        a('<div class="panel"><h2>持仓 · 占比是风控的第一指标</h2><table>')
        for name, q, qty, cost, val, pnl in rows:
            share = (val / nav * 100) if nav else 0
            c = "#ef4444" if q["pct"] < 0 else "#22c55e"
            warn = "color:#ef4444;font-weight:600" if share > 50 else \
                "color:#94a3b8"
            pnl_txt = f"{pnl:+.1f}%" if pnl is not None else "-"
            a(f'<tr><td>{_esc(name)}</td>'
              f'<td class="num">{q["price"]:.3f}</td>'
              f'<td class="num" style="color:{c}">{q["pct"]:+.2f}%</td>'
              f'<td class="num">{qty:,}股</td>'
              f'<td class="num">{val:,.0f}元</td>'
              f'<td class="num">{pnl_txt}</td>'
              f'<td class="num" style="{warn}">{share:.1f}%</td></tr>')
        a(f'<tr><td>现金</td><td></td><td></td><td></td>'
          f'<td class="num">{cash:,.0f}元</td><td></td>'
          f'<td class="num">{(cash/nav*100 if nav else 0):.1f}%</td></tr>')
        a(f'<tr><td><b>总资产</b></td><td></td><td></td><td></td>'
          f'<td class="num"><b>{nav:,.0f}元</b></td><td></td><td></td></tr>')
        a('</table>')
        if nav:
            top = max([v for _, _, _, _, v, _ in rows] or [0]) / nav
            if top > 0.5:
                a('<div class="warn">⚠ 单一持仓占比 &gt;50%：'
                  '任何「假反弹」都会让你再亏一轮。</div>')
            elif top < 0.30:
                a('<div style="color:#22c55e;font-size:13px;margin-top:10px">'
                  '✅ 单一持仓占比 &lt;30%，可以等「真反转」再加仓。</div>')
        a('</div>')

    # 历史
    a('<div class="panel"><h2>涨家占比历史（近 40 日）</h2>')
    a(_spark(hist, "up_ratio"))
    a('</div>')

    a('<div class="foot">只判断「当前状态是真/假」，不预测点位与明日涨跌。'
      '数字以券商 App 为准。</div>')
    a('</div></body></html>')
    return "".join(P)


# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-holdings", action="store_true")
    ap.add_argument("--save-only", action="store_true")
    ap.add_argument("--json", dest="json_path", default=None)
    ap.add_argument("--html", dest="html_path", nargs="?", const="",
                    default=None,
                    help="同时生成网页版（默认为 data/breadth.html）")
    args = ap.parse_args()

    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    now = datetime.now()
    stamp = now.strftime("%Y-%m-%d %H:%M")
    today = now.strftime("%Y-%m-%d")
    progress = market_progress(now)

    total_stocks, rows = fetch_all_a()
    ind = compute_breadth(rows)

    hist = load_history()
    sample_n = len(hist)
    total, verdict, tag, items, amount_mult, streak = score_all(ind, hist, progress)

    # 报价
    indexes = fetch_quotes([c for c, _ in INDEXES])
    quotes, holdings, cash = {}, [], 0.0
    if not args.no_holdings:
        holdings = list(DEFAULT_HOLDINGS)
        if os.path.exists(HOLD_PATH):
            try:
                with open(HOLD_PATH, "r", encoding="utf-8") as f:
                    hh = json.load(f)
                if isinstance(hh, dict):
                    if isinstance(hh.get("list"), list) and hh["list"]:
                        holdings = hh["list"]
                    cash = float(hh.get("cash") or 0)
                elif isinstance(hh, list) and hh:
                    holdings = hh
            except Exception:
                pass
        quotes = fetch_quotes([h["code"] for h in holdings])

    rec = {
        "date": today,
        "stamp": stamp,
        "up": ind["up"], "down": ind["down"], "flat": ind["flat"],
        "up_ratio": round(ind["up_ratio"], 6),
        "amt_ratio": round(ind["amt_ratio"], 6),
        "vol_ratio": round(ind["vol_ratio"], 6),
        "trin_amt": None if ind["trin_amt"] is None else round(ind["trin_amt"], 4),
        "trin_vol": None if ind["trin_vol"] is None else round(ind["trin_vol"], 4),
        "limit_up": ind["limit_up"], "limit_down": ind["limit_down"],
        "amount_yi": round(ind["amount_yi"], 1),
        "progress": round(progress, 4),
        "score": total, "verdict": verdict,
    }

    # 同一天重跑则覆盖
    hist = [h for h in hist if h.get("date") != today]
    hist.append(rec)
    hist.sort(key=lambda h: h.get("date", ""))
    save_history(hist)

    # ---- 两票制（资金面 / 技术面）+ 趋势指标 ----
    trend = compute_trend(hist)
    idx_q = indexes.get("sh000001") or {}
    tech_vote, tech_detail = technical_vote_from("sh000001",
                                                 idx_q.get("price"), today)
    fund_vote = fund_vote_from_score(total)
    final_vote, final_reason = combine_votes(fund_vote, tech_vote)

    hold_tech = {}
    for h in holdings:
        if not h.get("qty"):
            continue
        q = quotes.get(h["code"]) or {}
        v, d = technical_vote_from(h["code"], q.get("price"), today)
        hold_tech[h["code"]] = (v, d)

    extras = {
        "fund_vote": fund_vote, "tech_vote": tech_vote,
        "final_vote": final_vote, "final_reason": final_reason,
        "tech_detail": tech_detail, "trend": trend, "hold_tech": hold_tech,
    }

    if args.json_path:
        with open(args.json_path, "w", encoding="utf-8") as f:
            json.dump({"record": rec, "history": hist}, f, ensure_ascii=False, indent=1)

    if args.html_path is not None:
        hp = args.html_path or os.path.join(DATA, "breadth.html")
        try:
            with open(hp, "w", encoding="utf-8") as f:
                f.write(render_html(ind, hist[:-1], total, verdict, tag, items,
                                    amount_mult, streak, quotes, indexes,
                                    holdings, stamp, sample_n, total_stocks,
                                    cash, progress, extras))
            print(f"[html] {hp}")
        except Exception as e:
            print(f"[html] 生成失败: {e}")

    if args.save_only:
        print(f"[saved] {today} up_ratio={ind['up_ratio']*100:.1f}% "
              f"score={total} {verdict}")
        return

    report = build_report(ind, hist[:-1], total, verdict, tag, items, amount_mult,
                          streak, quotes, indexes, holdings, stamp, sample_n,
                          total_stocks, cash, progress, extras)
    print(report)


if __name__ == "__main__":
    main()
