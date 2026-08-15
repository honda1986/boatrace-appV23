#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
tokuten.py -- uchisankaku.sakura.ne.jp から今節成績(得点率・順位)を集める

■ 仮説(先に凍結する)
  予選終盤、準優ボーダー(18位)ギリギリの選手は市場の想定より走り、
  当確圏で得点差に余裕がある選手は市場の想定より走らない。
  検証は「艇ごとの市場1着確率(3連単オッズから) vs 実測1着率」を
  動機グループ別に比べる。分析は別スクリプト。ここでは集めて検査するだけ。

■ このサイトを選ぶ理由
  1場1日1ページに12レース分の 順位・得点率・減点・過去走(コース/着) が
  全部入っている。日程タブから「何日目/全何日」も確定する。
  計算も節の復元も要らない。

■ 取り方の設計
  ・raw/ にある開催(date,jcd)だけ取る → 約半分に減る(1日24→12前後)
  ・個人サイトなので並列は3固定。上げない。
  ・1日1ファイル tokuten/YYYYMMDD.json.gz。再実行で続きから。
  ・登録番号は本文テキストに無い可能性が高い(ボタン内?)ので、
    input/リンク/画像まで見る。取れなくても氏名で選手を追える。

■ カンニング検査(--check) ※収集の前に必ずやる
  D日ページの得点率が「D日の結果を含んでいたら」全部無効(前科2件と同型)。
  判定: 同じ選手のD日ページとD+1日ページの走数を比べる。
    差 = D日のその選手の出走数 → 白(D日開始前の状態)
    差 = 0                    → 黒(D日の結果込み)
  さらに新しく増えた走の(コース/着)を raw/ の本番進入・着順と突き合わせ、
  「5(6)／3」の括弧がどちらの意味かも同時に判定する。

■ 使い方
  python tokuten.py --probe                 # 過去何日分あるか(約10リクエスト)
  python tokuten.py --check 20260804 12     # カンニング検査(2ページ+raw)
  python tokuten.py --days 90               # 収集(直近90日)
  python tokuten.py --start 20230501 --end 20250430 --minutes 300
"""

import argparse
import glob
import gzip
import json
import os
import re
import sys
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta

import requests
from bs4 import BeautifulSoup, NavigableString

URL = "https://uchisankaku.sakura.ne.jp/racelist.php"
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
WORKERS = 3            # 個人サイト。これ以上は上げない

DAY_PAT = re.compile(r"(\d{1,2})月(\d{1,2})日\s*(初日|(\d{1,2})日目|最終日)")
RUN_PAT = re.compile(r"^([1-6])(?:\(([1-6])\))?\s*[／/]\s*(\S{1,3})$")
RACE_PAT = re.compile(r"^(\d{1,2})\s*R\b\s*(.*)$")


def norm(s):
    """全角→半角、空白圧縮"""
    s = unicodedata.normalize("NFKC", s or "")
    return re.sub(r"\s+", " ", s).strip()


# ---------------------------------------------------------------- 取得
def make_session():
    s = requests.Session()
    s.headers.update(UA)
    ad = requests.adapters.HTTPAdapter(pool_connections=WORKERS + 1,
                                       pool_maxsize=WORKERS + 1)
    s.mount("https://", ad)
    return s


def fetch(sess, jcd, d):
    for attempt in (1, 2):
        try:
            r = sess.get(URL, params={"jcode": jcd, "date": d}, timeout=30)
            if r.status_code == 200:
                if not r.encoding or r.encoding.lower() == "iso-8859-1":
                    r.encoding = "utf-8"
                return r.text
            if r.status_code == 404:
                return None
        except requests.RequestException:
            pass
        if attempt == 1:
            time.sleep(2)
    return None


# ---------------------------------------------------------------- パース
def cell_toban(td):
    """登録番号は本文に無いことがある。input/リンク/画像まで見る。"""
    m = re.search(r"\b(\d{4})\b", td.get_text())
    if m:
        return int(m.group(1))
    for t in td.find_all(["input", "button"]):
        m = re.search(r"(\d{4})", (t.get("value") or "") + (t.get("name") or ""))
        if m:
            return int(m.group(1))
    for a in td.find_all("a", href=True):
        m = re.search(r"(\d{4})", a["href"])
        if m:
            return int(m.group(1))
    for img in td.find_all("img"):
        m = re.search(r"(\d{4})", (img.get("alt") or "") + (img.get("src") or ""))
        if m:
            return int(m.group(1))
    return None


def parse_tabs(soup, want):
    """日程タブ → 節の全日程と、この日が何日目か"""
    seen, order = set(), []
    for el in soup.find_all(["a", "li", "td", "div", "span"]):
        t = norm(el.get_text(" "))
        if len(t) > 30:
            continue
        m = DAY_PAT.search(t)
        if not m:
            continue
        key = (int(m.group(1)), int(m.group(2)))
        if key in seen:
            continue
        seen.add(key)
        order.append(key)
    if not order:
        return None, None, None
    y = int(want[:4])
    sched = []
    for mo, dd in order:
        best = None
        for yy in (y - 1, y, y + 1):
            try:
                c = date(yy, mo, dd)
            except ValueError:
                continue
            ref = date(int(want[:4]), int(want[4:6]), int(want[6:8]))
            if best is None or abs((c - ref).days) < abs((best - ref).days):
                best = c
        sched.append(best.strftime("%Y%m%d"))
    day_no = sched.index(want) + 1 if want in sched else None
    return sched, day_no, len(sched)


def parse_run(txt):
    t = norm(txt)
    if not t:
        return None
    m = RUN_PAT.match(t)
    if not m:
        return None
    fin = m.group(3)
    return {"a": int(m.group(1)),
            "b": int(m.group(2)) if m.group(2) else None,
            "fin": int(fin) if fin.isdigit() else fin,
            "raw": t}


def parse_page(html, want_date):
    soup = BeautifulSoup(html, "html.parser")
    sched, day_no, n_days = parse_tabs(soup, want_date)

    races, cur = {}, None
    blk = [""]
    sub = [""]
    for node in soup.descendants:
        if isinstance(node, NavigableString):
            t = norm(str(node))
            m = RACE_PAT.match(t)
            if m and 1 <= int(m.group(1)) <= 12:
                rno = int(m.group(1))
                name = m.group(2)
                if not name:                       # "1R" と名前が別要素の場合
                    p = node.parent
                    for _ in range(3):
                        if p is None:
                            break
                        pt = norm(p.get_text(" "))
                        mm = re.search(rf"\b{rno}\s*R\b\s*(\S.*)$", pt)
                        if mm and len(pt) < 40:
                            name = mm.group(1)
                            break
                        p = p.parent
                cur = rno
                blk[0] = ""
                races.setdefault(rno, {
                    "rno": rno, "name": name[:20],
                    "lanes": [{"lane": i + 1} for i in range(6)]})
            continue
        if getattr(node, "name", None) != "tr" or cur is None:
            continue
        # 決り手の <tr> が閉じられていないページがある。再帰で拾うと
        # 内側に入れ子になった以降の行のセルまで混ざるので直下だけ見る。
        cells = node.find_all(["td", "th"], recursive=False)
        if len(cells) < 7:
            continue
        label = norm("".join(c.get_text() for c in cells[:-6]))
        vals = cells[-6:]
        L = races[cur]["lanes"]
        # 語中の空白を落としてから判定する(「選 手 情 報」「モ ー タ ー」対策)
        lab = re.sub(r"\s+", "", label)
        # どのブロックの行かを覚える(見出しは先頭行にしか現れない)
        if "今節" in lab:
            blk[0] = "setsu"
        elif "コース別" in lab or "直近" in lab:
            blk[0] = "course"
        elif "モータ" in lab:
            blk[0] = "motor"
        elif "成績" in lab and "今節" not in lab:
            blk[0] = "grade"
            sub[0] = ""
        elif "選手" in lab:
            blk[0] = "info"
        if "登録番号" in label:
            for i, td in enumerate(vals):
                L[i]["toban"] = cell_toban(td)
        elif "氏名" in label:
            for i, td in enumerate(vals):
                L[i]["name"] = norm(td.get_text(" "))
        elif "級別" in label and "cls" not in L[0]:
            for i, td in enumerate(vals):
                L[i]["cls"] = norm(td.get_text())[:2]
        elif blk[0] == "info":
            key = None
            if "年齢" in lab:
                key = "age"
            elif "体重" in lab:
                key = "weight"
            elif lab.endswith("F数"):
                key = "f_count"
            elif lab.endswith("L数"):
                key = "l_count"
            if key:
                for i, td in enumerate(vals):
                    m2 = re.search(r"(\d+(?:\.\d+)?)", norm(td.get_text()))
                    L[i][key] = float(m2.group(1)) if m2 else None
        elif blk[0] == "grade":
            if "全国" in lab:
                sub[0] = "n"
            elif "当地" in lab:
                sub[0] = "l"
            key = None
            if sub[0] and "勝率" in lab:
                key = f"{sub[0]}_win"
            elif sub[0] and "2連率" in lab:
                key = f"{sub[0]}_2ren"
            elif sub[0] and "3連率" in lab:
                key = f"{sub[0]}_3ren"
            if key:
                for i, td in enumerate(vals):
                    try:
                        L[i][key] = float(norm(td.get_text()))
                    except ValueError:
                        L[i][key] = None
        elif blk[0] == "motor":
            key = ("m_2ren" if "2連率" in lab else
                   "m_3ren" if "3連率" in lab else None)
            if key:
                for i, td in enumerate(vals):
                    try:
                        L[i][key] = float(norm(td.get_text()))
                    except ValueError:
                        L[i][key] = None
        elif blk[0] == "course":
            kim = lab.replace("決り手", "")
            if kim in ("差され", "捲られ", "差し", "捲り差し", "捲り", "タイプ"):
                if kim == "タイプ":
                    for i, td in enumerate(vals):
                        t = norm(td.get_text())
                        L[i]["c_type"] = t if t and t != "-" else None
                elif kim in ("差され", "捲られ"):
                    # 1号艇の列だけが合計。2〜6号艇の括弧は
                    # 「1号艇がそのコースにやられた率」の内訳。
                    k = "c_sasare" if kim == "差され" else "c_makurare"
                    for i, td in enumerate(vals):
                        t = norm(td.get_text()).strip("()")
                        try:
                            v = float(t)
                        except ValueError:
                            v = None
                        L[i][k if i == 0 else k + "_from"] = v
                else:
                    k = {"差し": "c_sashi", "捲り差し": "c_makurizashi",
                         "捲り": "c_makuri"}[kim]
                    # 1号艇は "-"(そのコースからは差しに行かない)
                    for i, td in enumerate(vals):
                        try:
                            L[i][k] = float(norm(td.get_text()))
                        except ValueError:
                            L[i][k] = None
            else:
                key = None
                if label.endswith("ST"):
                    key = "c_st"
                elif "追い風" in label:
                    key = "c_st_oi"
                elif "向い風" in label:
                    key = "c_st_muk"
                elif "1着率" in label:
                    key = "c_win"
                elif "2着率" in label:
                    key = "c_2nd"
                elif "3着率" in label:
                    key = "c_3rd"
                elif "3連率" in label:
                    key = "c_ren3"
                if key:
                    for i, td in enumerate(vals):
                        t = norm(td.get_text()).replace("(", "").replace(")", "")
                        try:
                            L[i][key] = float(t)
                        except ValueError:
                            L[i][key] = None
        elif blk[0] == "setsu" and label.endswith("ST"):
            for i, td in enumerate(vals):
                t = norm(td.get_text())
                try:
                    L[i]["st_setsu"] = float(t)
                except ValueError:
                    L[i]["st_setsu"] = None
        elif blk[0] == "setsu" and ("1着率" in label or "2連率" in label
                                    or "3連率" in label):
            key = ("win" if "1着率" in label
                   else "ren2" if "2連率" in label else "ren3")
            for i, td in enumerate(vals):
                t = norm(td.get_text())
                try:
                    L[i]["s_" + key] = float(t)
                except ValueError:
                    L[i]["s_" + key] = None
        elif label.endswith("順位") or label == "順位":
            for i, td in enumerate(vals):
                t = norm(td.get_text())
                L[i]["rank"] = int(t) if t.isdigit() else None
        elif "得点率" in label:
            for i, td in enumerate(vals):
                t = norm(td.get_text())
                try:
                    L[i]["tokuten"] = float(t)
                except ValueError:
                    L[i]["tokuten"] = None
        elif "減点" in label:
            for i, td in enumerate(vals):
                t = norm(td.get_text())
                try:
                    L[i]["genten"] = float(t)
                except ValueError:
                    L[i]["genten"] = 0.0
        elif re.fullmatch(r".*[1-6]走", label):
            k = int(label[-2])
            for i, td in enumerate(vals):
                r = parse_run(td.get_text())
                if r:
                    L[i].setdefault("runs", {})[k] = r

    out = []
    for rno in sorted(races):
        r = races[rno]
        if not any("name" in x for x in r["lanes"]):
            continue
        for x in r["lanes"]:
            runs = x.pop("runs", {})
            x["runs"] = [runs[k] for k in sorted(runs)]
            x["n_runs"] = len(x["runs"])
        out.append(r)
    if not out:
        return None
    return {"date": want_date, "sched": sched, "day_no": day_no,
            "n_days": n_days, "races": out}


# ---------------------------------------------------------------- raw索引
def raw_index(raw_dir):
    """date -> set(jcd)。開催していた場だけ取るために使う。"""
    idx = {}
    for p in sorted(glob.glob(os.path.join(raw_dir, "*.json.gz"))):
        d = os.path.basename(p)[:8]
        try:
            with gzip.open(p, "rt", encoding="utf-8") as f:
                data = json.load(f)
            js = {r["jcd"] for r in data.get("races", []) if "error" not in r}
            if js:
                idx[d] = sorted(js)
        except Exception:
            continue
    return idx


def raw_results(raw_dir, d):
    """(jcd,rno,lane) -> (course_in, rank)"""
    p = os.path.join(raw_dir, f"{d}.json.gz")
    if not os.path.exists(p):
        return {}
    with gzip.open(p, "rt", encoding="utf-8") as f:
        data = json.load(f)
    out = {}
    for r in data.get("races", []):
        if "error" in r:
            continue
        for e in r["entries"]:
            out[(r["jcd"], r["rno"], e["lane"])] = (e.get("course_in"),
                                                    e.get("rank"))
    return out


# ---------------------------------------------------------------- probe
def do_probe(sess, raw_dir):
    idx = raw_index(raw_dir)
    if not idx:
        sys.exit("raw/ がありません")
    dates = sorted(idx)
    today = date.today()
    print("過去にさかのぼって、ページがあるか調べます")
    ok_oldest = None
    for back in (7, 30, 90, 180, 365, 550, 730, 900, 1100):
        target = (today - timedelta(days=back)).strftime("%Y%m%d")
        cand = [d for d in dates if d <= target]
        if not cand:
            break
        d = cand[-1]
        jcd = idx[d][0]
        html = fetch(sess, jcd, d)
        page = parse_page(html, d) if html else None
        has_tok = page and any(x.get("tokuten") is not None
                               for r in page["races"] for x in r["lanes"])
        mark = "○" if page else "×"
        extra = f" レース{len(page['races'])} 得点率{'あり' if has_tok else 'なし'}" \
            if page else ""
        print(f"  {back:>4}日前 ({d} 場{jcd:02d}): {mark}{extra}", flush=True)
        if page:
            ok_oldest = d
        else:
            break
    print()
    if ok_oldest:
        print(f"少なくとも {ok_oldest} まではあります。"
              f"--start {ok_oldest} で収集できます。")
    else:
        print("過去ページが見つかりません。前向きに貯める運用になります。")


# ---------------------------------------------------------------- check
def key_of(x):
    return x.get("toban") or x.get("name")


def do_check(sess, d, jcd, raw_dir):
    jcd = int(jcd)
    d2 = (datetime.strptime(d, "%Y%m%d") + timedelta(days=1)).strftime("%Y%m%d")
    print(f"=== カンニング検査 {d} → {d2}  場{jcd:02d} ===\n")
    h1 = fetch(sess, jcd, d)
    h2 = fetch(sess, jcd, d2)
    p1 = parse_page(h1, d) if h1 else None
    p2 = parse_page(h2, d2) if h2 else None
    if not p1 or not p2:
        sys.exit("ページが取れません。開催中の連続2日を指定してください。")
    if p2["sched"] and d not in p2["sched"]:
        sys.exit("2日が同じ節ではありません。節の途中の日を指定してください。")

    print(f"{d}: {p1['day_no']}日目/{p1['n_days']}日  レース{len(p1['races'])}")
    print(f"{d2}: {p2['day_no']}日目/{p2['n_days']}日  レース{len(p2['races'])}")
    n_tob = sum(1 for r in p1["races"] for x in r["lanes"] if x.get("toban"))
    n_all = sum(len(r["lanes"]) for r in p1["races"])
    print(f"登録番号の取得率 {n_tob}/{n_all}"
          f"  ({'氏名で追跡します' if n_tob < n_all//2 else 'OK'})\n")

    sched_d = {}                       # 選手 -> [(rno,lane), ...]
    for r in p1["races"]:
        for x in r["lanes"]:
            k = key_of(x)
            if k:
                sched_d.setdefault(k, []).append((r["rno"], x["lane"]))
    runs1 = {key_of(x): x for r in p1["races"] for x in r["lanes"] if key_of(x)}
    runs2 = {key_of(x): x for r in p2["races"] for x in r["lanes"] if key_of(x)}

    res = raw_results(raw_dir, d)
    clean = leaky = odd = 0
    m_outer = m_paren = m_fin = n_new = 0
    for k, x2 in runs2.items():
        x1 = runs1.get(k)
        if not x1:
            continue
        expect = len(sched_d.get(k, []))
        diff = x2["n_runs"] - x1["n_runs"]
        if diff == expect and expect > 0:
            clean += 1
        elif diff == 0 and expect > 0:
            leaky += 1
        else:
            odd += 1
        # 新しく増えた走を raw の本番進入・着順と突き合わせる
        new = x2["runs"][x1["n_runs"]:]
        for run, (rno, lane) in zip(new, sorted(sched_d.get(k, []))):
            got = res.get((jcd, rno, lane))
            if not got:
                continue
            ci, rk = got
            n_new += 1
            if run["a"] == ci:
                m_outer += 1
            if (run["b"] or run["a"]) == ci:
                m_paren += 1
            if run["fin"] == rk:
                m_fin += 1

    tot = clean + leaky + odd
    print(f"両日に出走した選手 {tot}人")
    print(f"  白(走数の差 = {d}の出走数): {clean}")
    print(f"  黒(走数の差 = 0 = 結果込み): {leaky}")
    print(f"  その他(欠場等): {odd}\n")
    if n_new:
        print(f"新しい走と raw/ の突き合わせ {n_new}件")
        print(f"  着順の一致          {m_fin}/{n_new}")
        print(f"  外の数字 = 本番進入  {m_outer}/{n_new}")
        print(f"  括弧優先 = 本番進入  {m_paren}/{n_new}")
        print("  → 一致率が高い方が『5(6)／3』の正しい読み方\n")
    print("=" * 50)
    if tot and clean / tot > 0.9:
        print("判定: 白。D日ページはD日開始前の状態。収集して良い。")
    elif tot and leaky / tot > 0.5:
        print("判定: 黒。得点率にその日の結果が混ざっている。")
        print("  このままでは使えない。前日ページの値を使う設計に変える。")
    else:
        print("判定: 不明瞭。別の日・別の場でもう一度。")


# ---------------------------------------------------------------- collect
def collect(sess, args):
    idx = raw_index(args.raw)
    if not idx:
        sys.exit("raw/ がありません")
    dates = sorted(idx)
    if args.start and args.end:
        dates = [d for d in dates if args.start <= d <= args.end]
    elif args.days:
        cut = (date.today() - timedelta(days=args.days)).strftime("%Y%m%d")
        dates = [d for d in dates if d >= cut]
    dates = dates[::-1]                      # 新しい順(途中で止めても直近が揃う)
    os.makedirs(args.out, exist_ok=True)
    print(f"対象 {len(dates)}日  (1日あたり平均 "
          f"{sum(len(idx[d]) for d in dates)//max(len(dates),1)}場, 並列{WORKERS})")

    t0 = time.time()
    n_days = n_races = 0
    for d in dates:
        path = os.path.join(args.out, f"{d}.json.gz")
        if os.path.exists(path) and not getattr(args, "force", False):
            continue
        if args.minutes and (time.time() - t0) / 60 > args.minutes:
            print(f"[stop] 時間切れ。再実行で {d} から再開します。", flush=True)
            break
        t = time.time()
        with ThreadPoolExecutor(max_workers=WORKERS) as ex:
            htmls = list(ex.map(lambda j: (j, fetch(sess, j, d)), idx[d]))
        venues = {}
        for j, html in htmls:
            page = parse_page(html, d) if html else None
            if page:
                venues[str(j)] = page
        with gzip.open(path, "wt", encoding="utf-8") as f:
            json.dump({"date": d, "venues": venues}, f,
                      ensure_ascii=False, separators=(",", ":"))
        got = sum(len(v["races"]) for v in venues.values())
        n_days += 1
        n_races += got
        tok = sum(1 for v in venues.values() for r in v["races"]
                  for x in r["lanes"] if x.get("tokuten") is not None)
        print(f"{d}: {len(venues)}/{len(idx[d])}場 {got}R "
              f"得点率{tok}艇  {time.time()-t:.0f}秒  累計{n_races}R", flush=True)

    print(f"\n完了: {n_days}日 / {n_races}レース / {(time.time()-t0)/60:.1f}分")


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--raw", default="raw")
    ap.add_argument("--out", default="tokuten")
    ap.add_argument("--probe", action="store_true")
    ap.add_argument("--check", nargs=2, metavar=("DATE", "JCD"))
    ap.add_argument("--days", type=int)
    ap.add_argument("--start")
    ap.add_argument("--end")
    ap.add_argument("--minutes", type=float, default=0)
    ap.add_argument("--force", action="store_true",
                    help="保存済みの日も取り直す(項目を増やしたとき)")
    args = ap.parse_args()

    sess = make_session()
    if args.probe:
        do_probe(sess, args.raw)
    elif args.check:
        do_check(sess, args.check[0], int(args.check[1]), args.raw)
    else:
        if not (args.days or (args.start and args.end)):
            args.days = 90
        collect(sess, args)


if __name__ == "__main__":
    main()
