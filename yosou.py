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
    ("全国勝率", ["n_win", "n_win_dev", "n_win_rk", "n_2ren", "n_win_in"],
     "n_win", "{:.2f}"),
    ("当地勝率", ["l_win", "l_win_dev", "l_win_rk", "l_2ren"], "l_win", "{:.2f}"),
    ("モーター2連率", ["m_2ren", "m_2ren_dev", "m_2ren_rk", "b_2ren"],
     "m_2ren", "{:.0%}"),
    ("内側とのモーター差", ["m_2ren_in"], "m_2ren_in", "{:+.0%}"),
    ("今節得点率", ["tok", "tok_dev", "tok_rk", "srank", "tok_in"], "tok", "{:.2f}"),
    ("今節ST", ["st_setsu", "st_setsu_dev", "st_setsu_rk", "st_setsu_in"],
     "st_setsu", "{:.2f}"),
    ("コース別1着率", ["c_win", "c_win_dev", "c_win_rk"], "c_win", "{:.0f}%"),
    ("コース別3連率", ["c_ren3", "c_st", "avg_st", "avg_st_dev", "avg_st_rk", "avg_st_in"],
     "c_ren3", "{:.0f}%"),
    ("級別", ["cls_val", "cls_max", "cls_gap"], "cls_val", "cls"),
    ("フライング", ["f_count", "is_f2", "has_f"], "f_count", "F{:.0f}"),
]
# 根拠に出さないもの(数値を示せないので読めない)
HIDDEN = ["age", "weight", "f_count", "jcd", "rno", "day_no", "n_days",
          "is_final", "nruns", "genten"]
SMALL_IS_GOOD = {"今節ST"}

# 1着艇・2着艇との差を取る指標(yosou_train2.py と同じ順)
REL = ["n_win", "tok", "m_2ren", "avg_st", "st_setsu", "c_win"]

# 勝負レースの基準(34,018レースの検証で決めた絶対値)
#   上位8組の確率合計 >= 0.553  … 全体の約10%
#   実測: 上位8組を買って 的中率 67.6% / 回収率 81.9%
#         上位6組なら     的中率 約58% / 回収率 約82%
#   参考の閾値: 0.518=上位20% / 0.582=上位5% / 0.634=上位1%
SHOBU_TH = 0.553
SHOBU_PTS = 6          # 勝負レースで買う点数
NOTIFY_BEFORE = 5             # ネット締切の何分前に通知するか(ntfyの予約送信)

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


def fetch_all_results(sess, date):
    """払戻金一覧(1ページ)から全場・全レースの3連単を取る。
    各行に raceresult?rno=&jcd= のリンクと ¥金額 が入っているので、
    そこから場・レース番号・払戻を拾い、直前のセルの3桁を組番とする。"""
    try:
        r = sess.get(f"{BASE}/pay", params={"hd": date}, timeout=25)
        r.raise_for_status()
        r.encoding = "utf-8"
    except requests.RequestException as e:
        print(f"  払戻一覧の取得に失敗 {type(e).__name__}")
        return {}
    soup = BeautifulSoup(r.text, "html.parser")
    out = {}
    for a in soup.find_all("a", href=re.compile(r"raceresult\?rno=")):
        t = a.get_text(strip=True)
        if not t.startswith("¥"):
            continue
        m = re.search(r"rno=(\d+)&jcd=(\d+)", a["href"])
        if not m:
            continue
        rno, jcd = int(m.group(1)), int(m.group(2))
        td = a.find_parent("td")
        combo = None
        if td:
            prev = td.find_previous_sibling("td")
            if prev:
                z = re.fullmatch(r"([1-6])([1-6])([1-6])", prev.get_text(strip=True))
                if z:
                    combo = f"{z.group(1)}-{z.group(2)}-{z.group(3)}"
        if not combo:
            continue
        row = a.find_parent("tr")
        henkan = bool(row and "返" in row.get_text())
        try:
            pay = int(t.lstrip("¥").replace(",", ""))
        except ValueError:
            continue
        out[(jcd, rno)] = {"hit": combo, "pay": pay, "henkan": henkan}
    return out


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
    f = np.nan_to_num(d["f_count"], nan=0.0)
    d["is_f2"] = (f >= 2).astype(float)
    d["has_f"] = (f >= 1).astype(float)
    for c, big in (("n_win", True), ("tok", True), ("m_2ren", True),
                   ("avg_st", False), ("st_setsu", False)):
        v = d[c]
        z = np.full(6, np.nan)
        z[1:] = (v[1:] - v[:-1]) if big else (v[:-1] - v[1:])
        d[f"{c}_in"] = z
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
            if fmt == "cls":
                shown = L[i].get("cls")
                cs = [x.get("cls") for x in L if x.get("cls")]
                if shown and cs:
                    if len(set(cs)) == 1:
                        continue                    # 全員同じ級別なら出さない
                    rankv = CLS.get(shown, 0)
                    top = max(CLS.get(z, 0) for z in cs)
                    same = cs.count(shown)
                    if rankv == top:
                        note = "6艇で最上位" if same == 1 else f"最上位{same}人"
                    else:
                        above = sum(1 for z in cs if CLS.get(z, 0) > rankv)
                        note = f"上に{above}人"
                scored.append({"k": name, "c": float(c), "v": shown, "note": note})
                continue
            if key and vals.get(key):
                v = L[i].get(key)
                arr = [z for z in vals[key] if z is not None]
                if v is not None and arr and len(set(arr)) > 1:
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
                        note = f"{len(arr)}艇で最{'速' if small else '高'}(平均{mn})"
                    elif rk >= len(arr):
                        note = f"{len(arr)}艇で最{'遅' if small else '低'}(平均{mn})"
                    else:
                        note = f"{rk}番目(平均{mn})"
                    # 数値の順位と評価の向きが食い違う場合は理由を添える
                    lo_rank = rk > len(arr) / 2
                    if c > 0 and lo_rank:
                        note += f"　{L[i]['lane']}コースなら十分"
                    elif c < 0 and not lo_rank:
                        note += f"　{L[i]['lane']}コースには物足りない"
            if key and shown is None:
                continue                      # 値を出せない項目は載せない
            scored.append({"k": name, "c": float(c), "v": shown, "note": note})
        scored.sort(key=lambda z: -abs(z["c"]))
        out.append([{"k": z["k"], "v": z["v"], "note": z["note"],
                     "d": 1 if z["c"] > 0 else -1} for z in scored[:3]])
    return out


def notify(topic, r, app_url, at=None):
    """at を渡すと ntfy の予約送信になる。朝のうちに全部登録できる。"""
    if not topic:
        return False
    buys = "  ".join(z["c"] for z in r["combos"][:SHOBU_PTS])
    body = (f"{buys}\n"
            f"本命 {r['top_lane']}号艇 {r['top_p']*100:.0f}%\n"
            f"実測 的中率55% 回収率80%  {SHOBU_PTS*100}円")
    payload = {"topic": topic,
               "title": f"勝負 {r['venue']} {r['rno']}R  "
                        f"ネット{r.get('net') or r['close']}締切",
               "message": body, "priority": 4, "tags": ["fire"]}
    if app_url:
        payload["click"] = app_url
    if at is not None:
        payload["delay"] = str(int(at))      # UNIX秒。この時刻に配信される
    try:
        rr = requests.post("https://ntfy.sh", json=payload, timeout=15)
        if rr.status_code >= 300:
            print(f"    ntfy応答 {rr.status_code}: {rr.text[:120]}")
        return rr.status_code < 300
    except requests.RequestException as e:
        print(f"    ntfy失敗 {type(e).__name__}")
        return False


def rel_row(base, p1, j, fixed, names):
    """1艇ぶんの特徴量を names の順に並べる。
    fixed = [1着のindex] または [1着, 2着]"""
    v = {c: base[c][j] for c in base}
    for k, f in enumerate(fixed):
        tag = ["w1", "w2"][k]
        v[f"{tag}_lanediff"] = float(j - f)
        v[f"{tag}_inside"] = 1.0 if j < f else 0.0
        v[f"{tag}_lane"] = float(f + 1)
        for c in REL:
            if c in base:
                a, b = base[c][j], base[c][f]
                v[f"{tag}_d_{c}"] = (a - b) if (a == a and b == b) else np.nan
        v[f"{tag}_p1"] = float(p1[f])
    return [v.get(nm, np.nan) for nm in names]


def cascade(base, p1, m2, m3, n2, n3):
    """1着確率から、2着・3着を条件付きで積み上げて120通りを作る"""
    pairs = [(a, b) for a in range(6) for b in range(6) if b != a]
    X2 = np.array([rel_row(base, p1, b, [a], n2) for a, b in pairs],
                  dtype=np.float64)
    r2 = m2.predict(X2)
    P2 = np.zeros((6, 6))
    for (a, b), v in zip(pairs, r2):
        P2[a, b] = max(v, 1e-9)
    P2 /= P2.sum(1, keepdims=True)

    tri = [(a, b, c) for a, b in pairs for c in range(6) if c not in (a, b)]
    X3 = np.array([rel_row(base, p1, c, [a, b], n3) for a, b, c in tri],
                  dtype=np.float64)
    r3 = m3.predict(X3)
    P3 = np.zeros((6, 6, 6))
    for (a, b, c), v in zip(tri, r3):
        P3[a, b, c] = max(v, 1e-9)
    for a, b in pairs:
        P3[a, b] /= P3[a, b].sum()

    p2m = np.zeros(6)                     # 2着になる確率
    p3m = np.zeros(6)                     # 3着になる確率
    combos = []
    for a, b, c in tri:
        pr = p1[a] * P2[a, b] * P3[a, b, c]
        combos.append((f"{a+1}-{b+1}-{c+1}", pr))
        p3m[c] += pr
    for a, b in pairs:
        p2m[b] += p1[a] * P2[a, b]
    combos.sort(key=lambda z: -z[1])
    return p2m, p3m, combos


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="yosou/data.json")
    ap.add_argument("--model-dir", dest="model_dir", default="yosou_model")
    ap.add_argument("--date", default="", help="YYYYMMDD(空で本日)")
    ap.add_argument("--workers", type=int, default=3)
    ap.add_argument("--app-url", dest="app_url",
                    default="https://honda1986.github.io/boatrace-appV23/yosou/",
                    help="通知をタップしたときに開くURL")
    args = ap.parse_args()

    import lightgbm as lgb
    now = datetime.now(JST)
    date = args.date or now.strftime("%Y%m%d")
    F3 = json.load(open(f"{args.model_dir}/features3.json", encoding="utf-8"))
    feats, n2, n3 = F3["p1"], F3["p2"], F3["p3"]
    model = lgb.Booster(model_file=f"{args.model_dir}/lgb_p1.txt")
    m2 = lgb.Booster(model_file=f"{args.model_dir}/lgb_p2.txt")
    m3 = lgb.Booster(model_file=f"{args.model_dir}/lgb_p3.txt")
    print(f"{date} の予想を作ります "
          f"(1着{len(feats)} / 2着{len(n2)} / 3着{len(n3)}特徴量)")

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

    # 2) 公式から締切予定時刻。前回の結果があれば使い回す(1場9秒かかるため)
    old_sched = {}
    if os.path.exists(args.out):
        try:
            o = json.load(open(args.out, encoding="utf-8"))
            if o.get("date") == date:
                old_sched = {int(k): v for k, v in (o.get("sched") or {}).items()}
        except Exception:
            pass
    need_s = [jp for jp in open_v if jp[0] not in old_sched]
    scheds = dict(old_sched)
    if need_s:
        print(f"締切時刻を取ります {len(need_s)}場")
        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            scheds.update(dict(ex.map(
                lambda jp: (jp[0], fetch_official(sess, jp[0], date)[0]), need_s)))
    else:
        print("締切時刻は前回の値を使います")
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

    # 既に出した予想は上書きしない(今節成績が日中に更新されるため)
    prev = {}
    if os.path.exists(args.out):
        try:
            old = json.load(open(args.out, encoding="utf-8"))
            if old.get("date") == date:
                for v in old.get("venues", []):
                    for r in v.get("races", []):
                        prev[(v["jcd"], r["rno"])] = r
        except Exception:
            pass
    if prev:
        print(f"前回の予想を引き継ぎます {len(prev)}レース")

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
            base = {c: X[:, fi[c]] for c in feats}
            p2m, p3m, combos = cascade(base, p, m2, m3, n2, n3)
            keep = prev.get((jcd, race["rno"]))
            if keep and keep.get("boats"):
                # 古い形式なら、あとから足した項目を補完する
                if "shobu" not in keep:
                    if keep.get("combos"):
                        s8 = sum(z["p"] for z in keep["combos"][:8])
                        keep["sum8"] = round(float(s8), 4)
                        keep["shobu"] = bool(s8 >= SHOBU_TH)
                        keep.setdefault("notified", False)
                    else:
                        keep = None          # combos が無い版は作り直す
                if keep:
                    races.append(keep)
                    continue
            close = sc[race["rno"] - 1] if sc and len(sc) >= race["rno"] else None
            boats = []
            for i, x in enumerate(race["lanes"]):
                boats.append({"lane": x["lane"], "name": x.get("name"),
                              "cls": x.get("cls"), "p": round(float(p[i]), 4),
                              "p2": round(float(p2m[i]), 4),
                              "p3": round(float(p3m[i]), 4),
                              "t2": round(float(p[i] + p2m[i]), 4),
                              "t3": round(float(p[i] + p2m[i] + p3m[i]), 4),
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
                "combos": [{"c": c, "p": round(float(v), 4)}
                           for c, v in combos[:8]],
                "sum8": round(float(sum(v for _, v in combos[:8])), 4),
                "shobu": bool(sum(v for _, v in combos[:8]) >= SHOBU_TH),
                "notified": False,
            })
        races.sort(key=lambda r: r["rno"])
        venues.append({"jcd": jcd, "venue": VENUE[jcd],
                       "day_no": page.get("day_no"), "n_days": page.get("n_days"),
                       "first": races[0]["net"] if races and races[0]["net"] else "99:99",
                       "races": races})
        print(f"  {VENUE[jcd]} {len(races)}R  "
              f"1号艇平均{np.mean([r['p1'] for r in races])*100:.0f}%", flush=True)

    # 4) 結果を入れる(払戻金一覧の1ページで全場ぶん)
    #    rank が無い古い記録は、保存済みの結果から補完する
    fixed = 0
    for v in venues:
        for r in v["races"]:
            res = r.get("result")
            if res and "rank" not in res and r.get("combos"):
                cl = [z["c"] for z in r["combos"]]
                res["rank"] = cl.index(res["hit"]) + 1 if res["hit"] in cl else None
                res["n_combos"] = len(cl)
                fixed += 1
    if fixed:
        print(f"古い結果 {fixed}件に順位を補完しました")

    if any(not r.get("result") for v in venues for r in v["races"]):
        print("\n払戻金一覧から結果を取ります (1リクエスト)")
        rr = fetch_all_results(sess, date)
        print(f"  {len(rr)}レース分の払戻を取得")
        n_res = 0
        for v in venues:
            for r in v["races"]:
                if r.get("result") or (v["jcd"], r["rno"]) not in rr:
                    continue
                x = rr[(v["jcd"], r["rno"])]
                picks = sorted(r["boats"], key=lambda b: -b["p"])
                cl = [z["c"] for z in r.get("combos", [])]
                hr = cl.index(x["hit"]) + 1 if x["hit"] in cl else None
                r["result"] = {
                    "hit": x["hit"], "pay": x["pay"], "henkan": x["henkan"],
                    "won": int(x["hit"].split("-")[0]) == r["top_lane"],
                    "rank": hr, "n_combos": len(cl),
                    "order": [int(z) for z in x["hit"].split("-")],
                    "ranks": [next((i + 1 for i, b in enumerate(picks)
                                    if b["lane"] == int(z)), None)
                              for z in x["hit"].split("-")],
                }
                n_res += 1
        print(f"  {n_res}レースに結果を記録しました")

    # 5) 勝負レースを ntfy に予約登録する(締切の5分前に届くよう指定)
    topic = os.environ.get("NTFY_TOPIC", "").strip()
    sent = skip = 0
    for v in venues:
        for r in v["races"]:
            if not r.get("shobu") or r.get("notified"):
                continue
            t = r.get("net") or r.get("close")
            if not t:
                continue
            hh, mm = (int(z) for z in t.split(":"))
            when = now.replace(hour=hh, minute=mm, second=0,
                               microsecond=0) - timedelta(minutes=NOTIFY_BEFORE)
            if (when - now).total_seconds() < 60:
                r["notified"] = True      # もう間に合わない
                skip += 1
                continue
            if notify(topic, r, args.app_url, at=when.timestamp()):
                r["notified"] = True
                sent += 1
                print(f"  予約 {v['venue']}{r['rno']}R  "
                      f"{when.strftime('%H:%M')} に配信 ({t}締切)")
    ns = sum(1 for v in venues for r in v["races"] if r.get("shobu"))
    print(f"\n勝負レース {ns}件 / 新たに予約 {sent}件"
          + (f" / 締切済み {skip}件" if skip else ""))

    venues.sort(key=lambda v: v["first"])
    data = {"date": date, "updated": now.strftime("%Y-%m-%d %H:%M"),
            "sched": {str(k): v for k, v in scheds.items() if v},
            "shobu_th": SHOBU_TH, "shobu_pts": SHOBU_PTS,
            "venues": venues,
            "note": "オッズ・展示タイム・進入コースは使っていません"}
    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
    n = sum(len(v["races"]) for v in venues)
    print(f"\n{args.out} を更新 ({len(venues)}場 {n}レース)")


if __name__ == "__main__":
    main()
