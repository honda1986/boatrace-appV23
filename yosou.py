#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
yosou.py -- 当日の全レースについて、各艇の1着確率を出す

■ 使う情報(レース当日の朝に確定しているもの)
  出走表: 枠番・級別・年齢・体重・F数・平均ST・全国勝率/2連率・当地勝率/2連率
  今節:   得点率・節内順位・節平均ST・走数
  コース別(直近6ヶ月): 1着率・3連率・ST
■ 使わない情報
  オッズ / 展示タイム / 進入コース

■ 正直な前提
  検証(34,018レース)での実力:
    このモデル 対数損失 1.2041 / 枠番だけ 1.4092 / 市場(オッズ) 約1.146
  オッズには勝てない。オッズが出る前に各艇の実力を眺めるための道具。
  較正は良好(予想2.5%→実測2.3%, 24.4%→23.8%, 45.1%→45.8%)。
  高確率帯だけ控えめに出るので、そこだけ補正する。

■ 出力
  yosou/data.json
"""

import argparse
import json
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import numpy as np
import requests
from bs4 import BeautifulSoup

import tokuten as TK          # ページ取得とパースを再利用

VENUE = {1: "桐生", 2: "戸田", 3: "江戸川", 4: "平和島", 5: "多摩川", 6: "浜名湖",
         7: "蒲郡", 8: "常滑", 9: "津", 10: "三国", 11: "びわこ", 12: "住之江",
         13: "尼崎", 14: "鳴門", 15: "丸亀", 16: "児島", 17: "宮島", 18: "徳山",
         19: "下関", 20: "若松", 21: "芦屋", 22: "福岡", 23: "唐津", 24: "大村"}

JST = timezone(timedelta(hours=9))
BASE = "https://www.boatrace.jp/owpc/pc/race"
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
      "Accept-Language": "ja"}
NET_LEAD = 3                  # ネット投票は本場締切より3分早い

CARD = ["lane", "cls_val", "age", "weight", "f_count", "avg_st",
        "n_win", "n_2ren", "l_win", "l_2ren", "m_2ren", "b_2ren"]
SETSU = ["tok", "srank", "genten", "nruns", "st_setsu",
         "c_win", "c_ren3", "c_st"]
CLS = {"A1": 4, "A2": 3, "B1": 2, "B2": 1}

# 特徴量をまとめて「何が効いたか」を人に読める形にする
GROUP = [
    ("枠番", ["lane"], None, None),
    ("全国勝率", ["n_win", "n_win_dev", "n_win_rk", "n_2ren"], "n_win", "{:.2f}"),
    ("当地勝率", ["l_win", "l_win_dev", "l_win_rk", "l_2ren"], "l_win", "{:.2f}"),
    ("モーター2連率", ["m_2ren", "m_2ren_dev", "m_2ren_rk", "b_2ren"], "m_2ren", "{:.0%}"),
    ("今節得点率", ["tok", "tok_dev", "tok_rk", "srank"], "tok", "{:.2f}"),
    ("今節ST", ["st_setsu", "st_setsu_dev", "st_setsu_rk"], "st_setsu", "{:.2f}"),
    ("コース別1着率", ["c_win", "c_win_dev", "c_win_rk"], "c_win", "{:.0f}%"),
    ("コース別成績", ["c_ren3", "c_st", "avg_st", "avg_st_dev", "avg_st_rk"],
     "c_ren3", "{:.0f}%"),
    ("級別", ["cls_val", "cls_max", "cls_gap"], None, None),
    ("選手", ["age", "weight", "f_count"], None, None),
    ("開催条件", ["jcd", "rno", "day_no", "n_days", "is_final", "nruns", "genten"],
     None, None),
]
SMALL_IS_GOOD = {"今節ST"}

# 較正の補正 (予想 → 実測)。学習時の検証結果から。
CAL = [(0.00, 0.05, 2.5, 2.3), (0.05, 0.10, 7.3, 6.6), (0.10, 0.15, 12.2, 11.7),
       (0.15, 0.20, 17.3, 16.9), (0.20, 0.30, 24.4, 23.8), (0.30, 0.40, 34.7, 33.8),
       (0.40, 0.50, 45.1, 45.8), (0.50, 0.70, 60.5, 63.8), (0.70, 1.01, 73.2, 78.6)]


def calibrate(p):
    for lo, hi, pred, act in CAL:
        if lo <= p < hi:
            return float(np.clip(p * (act / pred), 0.001, 0.995))
    return p


# ---------------------------------------------------------------- 出走表
def fetch_card(sess, jcd, date):
    """uchisankaku から1場12レース分をまとめて取る。
    開催していない場でも直近の節のページが返ることがあるので、
    日程タブに指定日が含まれているか(day_no が付くか)で必ず確かめる。"""
    html = TK.fetch(sess, jcd, date)
    if not html:
        return None
    page = TK.parse_page(html, date)
    if not page or not page.get("races"):
        return None
    if page.get("day_no") is None:
        return None                      # 別の日のページ = この日は開催なし
    return page


def parse_schedule(html):
    soup = BeautifulSoup(html, "html.parser")
    for tr in soup.find_all("tr"):
        if "締切予定時刻" not in tr.get_text():
            continue
        t = re.findall(r"\b(\d{1,2}:\d{2})\b", tr.get_text(" "))
        if len(t) >= 12:
            return t[:12]
    return None


def fetch_official(sess, jcd, date):
    """公式から締切予定時刻と、モーター/ボート2連率など出走表の数値を取る"""
    try:
        r = sess.get(f"{BASE}/racelist", params={"rno": 1, "jcd": f"{jcd:02d}",
                                                 "hd": date}, timeout=25)
        r.raise_for_status()
        r.encoding = "utf-8"
    except requests.RequestException:
        return None, {}
    sched = parse_schedule(r.text)
    return sched, {}


def net_time(hhmm):
    hh, mm = (int(x) for x in hhmm.split(":"))
    t = datetime(2000, 1, 1, hh, mm) - timedelta(minutes=NET_LEAD)
    return t.strftime("%H:%M")


# ---------------------------------------------------------------- 特徴量
def build_rows(page, jcd, raw_entries):
    """1場ぶんの (レース, 艇) 行を作る。raw_entries は公式出走表からの補完用"""
    out = []
    for r in page["races"]:
        lanes = []
        for x in r["lanes"]:
            pct = lambda v: (v / 100.0) if v is not None else None
            lanes.append({
                "lane": x["lane"],
                "cls_val": CLS.get(x.get("cls")),
                "age": x.get("age"), "weight": x.get("weight"),
                "f_count": x.get("f_count"),
                "avg_st": x.get("c_st"),          # コース別STを平均STの代わりに
                "n_win": x.get("n_win"), "n_2ren": pct(x.get("n_2ren")),
                "l_win": x.get("l_win"), "l_2ren": pct(x.get("l_2ren")),
                "m_2ren": pct(x.get("m_2ren")), "b_2ren": None,
                "tok": x.get("tokuten"), "srank": x.get("rank"),
                "genten": x.get("genten"), "nruns": x.get("n_runs"),
                "st_setsu": x.get("st_setsu"), "c_win": x.get("c_win"),
                "c_ren3": x.get("c_ren3"), "c_st": x.get("c_st"),
                "name": x.get("name"), "cls": x.get("cls"),
                "toban": x.get("toban"),
            })
        out.append({"rno": r["rno"], "name": r.get("name", ""),
                    "day_no": page.get("day_no"), "n_days": page.get("n_days"),
                    "jcd": jcd, "lanes": lanes})
    return out


def make_matrix(race, feats):
    """1レース6艇ぶんの特徴量行列"""
    L = race["lanes"]
    d = {}
    for c in CARD + SETSU:
        d[c] = np.array([x.get(c) if x.get(c) is not None else np.nan
                         for x in L], dtype=float)
    d["jcd"] = np.full(6, race["jcd"], dtype=float)
    d["rno"] = np.full(6, race["rno"], dtype=float)
    d["day_no"] = np.full(6, race.get("day_no") or np.nan, dtype=float)
    d["n_days"] = np.full(6, race.get("n_days") or np.nan, dtype=float)
    d["is_final"] = np.full(6, 1.0 if any(w in (race["name"] or "")
                                          for w in ("準優", "優勝", "選抜"))
                            else 0.0)
    d["cls_max"] = np.full(6, np.nanmax(d["cls_val"]) if not np.all(np.isnan(d["cls_val"]))
                           else np.nan)
    d["cls_gap"] = d["cls_val"] - d["cls_max"]
    for c in ("n_win", "l_win", "m_2ren", "c_win", "avg_st", "tok", "st_setsu"):
        v = d[c]
        d[f"{c}_dev"] = v - np.nanmean(v) if not np.all(np.isnan(v)) else np.full(6, np.nan)
        asc = c in ("avg_st", "st_setsu")
        order = np.argsort(v if asc else -v, kind="stable")
        rk = np.empty(6)
        rk[order] = np.arange(1, 7)
        rk[np.isnan(v)] = np.nan
        d[f"{c}_rk"] = rk
    return np.column_stack([d.get(f, np.full(6, np.nan)) for f in feats])


def explain(race, contrib, fi):
    """各艇について、確率を押し上げた/押し下げた要因の上位3つを作る。
    枠番は毎回いちばん大きくなるので除く(表を見れば分かる)。"""
    L = race["lanes"]
    vals = {}
    for _, cols, key, _fmt in GROUP:
        if key:
            vals[key] = [x.get(key) for x in L]

    out = []
    for i in range(len(L)):
        scored = []
        for name, cols, key, fmt in GROUP:
            if name == "枠番":
                continue
            c = sum(contrib[i][fi[f]] for f in cols if f in fi)
            if abs(c) < 1e-9:
                continue
            note, shown = "", None
            if key and vals.get(key):
                v = L[i].get(key)
                arr = [z for z in vals[key] if z is not None]
                if v is not None and arr:
                    small = name in SMALL_IS_GOOD
                    better = (lambda z: z < v) if small else (lambda z: z > v)
                    rk = 1 + sum(1 for z in arr if better(z))
                    mean = sum(arr) / len(arr)
                    if fmt == "{:.0%}":
                        shown = f"{v*100:.0f}%"
                        mn = f"{mean*100:.0f}%"
                    else:
                        shown = fmt.format(v)
                        mn = fmt.format(mean)
                    if rk == 1:
                        note = f"6艇で最{'速' if small else '高'}(平均{mn})"
                    elif rk >= len(arr):
                        note = f"6艇で最{'遅' if small else '低'}(平均{mn})"
                    else:
                        note = f"{rk}番目(平均{mn})"
            scored.append({"k": name, "c": float(c), "v": shown, "note": note})
        scored.sort(key=lambda z: -abs(z["c"]))
        out.append([{"k": z["k"], "v": z["v"], "note": z["note"],
                     "d": 1 if z["c"] > 0 else -1} for z in scored[:3]])
    return out


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="yosou/data.json")
    ap.add_argument("--model", default="yosou_model/lgb_yosou.txt")
    ap.add_argument("--features", default="yosou_model/features.json")
    ap.add_argument("--date", default="", help="YYYYMMDD(空で本日)")
    ap.add_argument("--workers", type=int, default=3)
    args = ap.parse_args()

    import lightgbm as lgb
    now = datetime.now(JST)
    date = args.date or now.strftime("%Y%m%d")
    feats = json.load(open(args.features, encoding="utf-8"))
    model = lgb.Booster(model_file=args.model)
    print(f"{date} の予想を作ります (特徴量{len(feats)}個)")

    sess = requests.Session()
    sess.headers.update(UA)
    sess.mount("https://", requests.adapters.HTTPAdapter(
        pool_connections=8, pool_maxsize=8))

    # 1) 出走表(uchisankaku)を全場ぶん取る
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        pages = list(ex.map(lambda j: (j, fetch_card(sess, j, date)),
                            range(1, 25)))
    open_v = [(j, p) for j, p in pages if p]
    print(f"日付が一致した場 {len(open_v)}  {time.time()-t0:.0f}秒")
    if not open_v:
        print("本日の開催はありません")
        json.dump({"date": date, "updated": now.strftime("%Y-%m-%d %H:%M"),
                   "venues": []}, open(args.out, "w"), ensure_ascii=False)
        return

    # 2) 公式から締切予定時刻(1場1リクエスト)。ここに無い場は開催していない
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        scheds = dict(ex.map(lambda jp: (jp[0], fetch_official(sess, jp[0], date)[0]),
                             open_v))
    dropped = [VENUE[j] for j, _ in open_v if not scheds.get(j)]
    open_v = [(j, p) for j, p in open_v if scheds.get(j)]
    if dropped:
        print(f"公式に締切時刻が無いので除外: {' '.join(dropped)}")
    print(f"開催確定 {len(open_v)}場")
    if not open_v:
        print("本日の開催はありません")
        json.dump({"date": date, "updated": now.strftime("%Y-%m-%d %H:%M"),
                   "venues": []}, open(args.out, "w"), ensure_ascii=False)
        return

    # 3) 予想
    venues = []
    for jcd, page in open_v:
        sc = scheds.get(jcd)
        races = []
        for race in build_rows(page, jcd, {}):
            X = make_matrix(race, feats)
            raw = model.predict(X)
            p = raw / max(raw.sum(), 1e-9)
            p = np.array([calibrate(v) for v in p])
            p = p / p.sum()
            contrib = model.predict(X, pred_contrib=True)   # (6, 特徴量+1)
            fi = {f: i for i, f in enumerate(feats)}
            why = explain(race, contrib, fi)
            close = sc[race["rno"] - 1] if sc and len(sc) >= race["rno"] else None
            boats = []
            for i, x in enumerate(race["lanes"]):
                boats.append({"lane": x["lane"], "name": x.get("name"),
                              "cls": x.get("cls"), "p": round(float(p[i]), 4),
                              "tok": x.get("tok"), "srank": x.get("srank"),
                              "st": x.get("st_setsu"), "cwin": x.get("c_win"),
                              "nwin": x.get("n_win"), "lwin": x.get("l_win"),
                              "m2": x.get("m_2ren"), "why": why[i]})
            top = int(np.argmax(p))
            races.append({
                "rno": race["rno"], "name": race["name"],
                "close": close, "net": net_time(close) if close else None,
                "boats": boats,
                "p1": round(float(p[0]), 4),
                "top_lane": boats[top]["lane"], "top_p": round(float(p[top]), 4),
                "spread": round(float(p.max() - np.sort(p)[-2]), 4),
            })
        races.sort(key=lambda r: r["rno"])
        venues.append({"jcd": jcd, "venue": VENUE[jcd],
                       "day_no": page.get("day_no"), "n_days": page.get("n_days"),
                       "first": races[0]["net"] if races and races[0]["net"] else "99:99",
                       "races": races})
        print(f"  {VENUE[jcd]} {len(races)}R  "
              f"1号艇平均{np.mean([r['p1'] for r in races])*100:.0f}%", flush=True)

    venues.sort(key=lambda v: v["first"])
    data = {"date": date, "updated": now.strftime("%Y-%m-%d %H:%M"),
            "venues": venues,
            "note": "オッズ・展示タイム・進入コースは使っていません"}
    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
    n = sum(len(v["races"]) for v in venues)
    print(f"\n{args.out} を更新 ({len(venues)}場 {n}レース)")


if __name__ == "__main__":
    main()
