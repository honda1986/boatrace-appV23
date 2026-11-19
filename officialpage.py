#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""officialpage.py -- uchisankaku が空のとき、公式(boatrace.jp)の出走表から同じ形のページを作る

★2026-11-19 から uchisankaku が更新されていない（トップページは全場「-R/--:--」、
  その日のページは選手名も無いひな形だけ）。v23 は出走表をまるごとそこから取っていたので、
  11/19 は「日付が一致した場 0 / 本日の開催はありません」で予想が空になった。

公式の出走表（1レース1ページ）から tokuten.parse_page と同じ形を作る。v24 の officialsetsu.py と同じ作り。
  出走表の数値 … 級別・年齢・体重・F数・L数・全国/当地の勝率と2連率・モーター2連率・登録番号・氏名
  今節成績     … その日より前の走から 出走数（6走まで）・今節ST（一致を確かめた）・得点率（近似）。
                 得点率は着順点（1着10・2着8・3着6・4着4・5着2・6着1）の平均で、ドリーム戦などではずれる。
                 順位はその日の出走表の選手の中での得点率の順位（近似）、減点は 0.0
  コース別     … 公式の出走表に無い。c_st には全体の平均STを入れる（v23 は c_st を平均STの代わりに使う）
  日程・レース名 … 日程タブとレースの見出し（uchisankaku と一致を確かめた）
★公式は GitHub Actions からの接続を断ることがある（2026-11-19 から Access Denied）。PC からは届く。
"""
import json
import os
import re
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor

import requests
from bs4 import BeautifulSoup

import tokuten as TK

BASE = "https://www.boatrace.jp/owpc/pc/race"
PTS = {1: 10, 2: 8, 3: 6, 4: 4, 5: 2, 6: 1}
MAX_RUNS = 6
NUM = r"-?\d+(?:\.\d+)?"
CACHE = ".cache"                  # git に入れない（.gitignore）。その日のうちだけ使う
MISS_RETRY_MIN = 60               # 「開催なし」と分かった場は、この分数のあいだ取り直さない


def _nz(s):
    return unicodedata.normalize("NFKC", s or "").strip()


def _cells(tr):
    return [_nz(td.get_text(" ", strip=True)) for td in tr.find_all("td", recursive=False)]


def _nums(s):
    return [float(x) for x in re.findall(NUM, s or "")]


def _st(s):
    m = re.fullmatch(r"0?\.(\d{2})", s or "")
    return int(m.group(1)) / 100.0 if m else None


def _rows(soup):
    """6艇ぶんの tbody（登録番号 / 級別 を手がかりに）"""
    return [tb for tb in soup.find_all("tbody")
            if re.search(r"\b\d{4}\s*/\s*(A1|A2|B1|B2)\b", tb.get_text(" ", strip=True))]


def race_name(soup):
    for h in soup.find_all(["h3", "h2", "span", "div"]):
        t = _nz(h.get_text(" ", strip=True))
        m = re.fullmatch(r"(\S.{0,18}?)\s*\d{3,4}m", t)
        if m:
            return m.group(1)
    return ""


def parse(html, date):
    """公式の出走表1ページ → (sched, day_no, n_days, race)。形が違えば None"""
    soup = BeautifulSoup(html, "html.parser")
    sched, day_no, n_days = TK.parse_tabs(soup, date)
    tbs = _rows(soup)
    if len(tbs) != 6:
        return None
    lanes = []
    for lane, tb in enumerate(tbs, start=1):
        rows = tb.find_all("tr", recursive=False)
        if len(rows) < 4:
            return None
        c0 = _cells(rows[0])
        i = next((k for k, c in enumerate(c0) if re.match(r"F\s*\d+\s+L\s*\d+", c)), None)
        if i is None or i < 2:
            return None
        m = re.search(r"(\d{4})\s*/\s*(A1|A2|B1|B2)\s*(.*?)\s+\S+/\S+\s+(\d{1,2})歳\s*/\s*(\d{2}(?:\.\d)?)kg", c0[i - 1])
        fl = re.match(r"F\s*(\d+)\s+L\s*(\d+)\s*(\S*)", c0[i])
        st = fl.group(3)
        avg_st = float(st) if re.fullmatch(r"\d?\.\d{2}", st) else (0.0 if st == "-" else None)
        nat, loc, mot = (_nums(c0[i + k]) if i + k < len(c0) else [] for k in (1, 2, 3))
        # 今節成績: モーター・ボートの右に1日2枠ずつ。2〜4行目が 進入・ST・着順
        s0 = i + 6
        R, C, ST, CH = c0[s0:s0 + 14], _cells(rows[1]), _cells(rows[2]), _cells(rows[3])
        runs = []
        for k in range(min(14, len(R))):
            if not R[k]:
                continue
            day = k // 2 + 1
            if day_no is not None and day >= day_no:
                continue                       # その日より前の走だけ（uchisankaku と同じ）
            fin = CH[k] if k < len(CH) else ""
            runs.append({"day": day, "r": R[k], "course": C[k] if k < len(C) else "",
                         "st": _st(ST[k] if k < len(ST) else ""),
                         "fin": int(fin) if fin.isdigit() else fin})
        sts = [x["st"] for x in runs if x["st"] is not None]
        pts = [PTS.get(x["fin"], 0) if isinstance(x["fin"], int) else 0 for x in runs]
        lanes.append({
            "lane": lane,
            "toban": int(m.group(1)) if m else None, "cls": m.group(2) if m else None,
            "name": re.sub(r"\s+", " ", m.group(3)).strip() if m else "",
            "age": float(m.group(4)) if m else None, "weight": float(m.group(5)) if m else None,
            "f_count": float(fl.group(1)), "l_count": float(fl.group(2)),
            # uchisankaku と同じく 2連率は % のまま
            "n_win": nat[0] if len(nat) == 3 else None, "n_2ren": nat[1] if len(nat) == 3 else None,
            "l_win": loc[0] if len(loc) == 3 else None, "l_2ren": loc[1] if len(loc) == 3 else None,
            "m_2ren": mot[1] if len(mot) == 3 else None,
            "n_runs": min(len(runs), MAX_RUNS),
            "st_setsu": round(sum(sts) / len(sts), 2) if sts else None,
            "tokuten": round(sum(pts) / len(pts), 2) if pts else None,
            "rank": None, "genten": 0.0,
            "c_win": None, "c_ren3": None, "c_st": avg_st,     # ★全体の平均STで代える
            "src": "official", "runs": runs})
    return sched, day_no, n_days, {"rno": None, "name": race_name(soup), "lanes": lanes}


def fetch(sess, date, jcd, rno, tries=2):
    for i in range(tries):
        try:
            r = sess.get(f"{BASE}/racelist", params={"rno": rno, "jcd": f"{jcd:02d}", "hd": date},
                         timeout=25)
            r.encoding = "utf-8"
            if r.status_code == 200 and len(r.text) > 20000:
                return r.text
            if r.status_code == 403:
                return None                    # 断られた（GitHub から）。やり直しても同じ
        except requests.RequestException:
            pass
        if i + 1 < tries:
            time.sleep(1.5)
    return None


def _cache_path(date, jcd):
    return os.path.join(CACHE, f"op_{date}_{jcd:02d}.json")


def fetch_venue(sess, jcd, date, workers=3):
    """1場12レースぶんを uchisankaku のページと同じ形で返す。開催が無い・取れなければ None。
    その日のうちは .cache/ に覚えておく（取れたページは1日1回、「無い」は MISS_RETRY_MIN 分ごと）"""
    p = _cache_path(date, jcd)
    try:
        with open(p, encoding="utf-8") as f:
            c = json.load(f)
        if c.get("page"):
            return c["page"]
        if time.time() - c.get("t", 0) < MISS_RETRY_MIN * 60:
            return None
    except (OSError, ValueError):
        pass
    page = _build(sess, jcd, date, workers)
    os.makedirs(CACHE, exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        json.dump({"t": time.time(), "page": page}, f, ensure_ascii=False)
    return page


def _build(sess, jcd, date, workers):
    first = fetch(sess, date, jcd, 1)
    got = parse(first, date) if first else None
    if not got or got[1] is None:
        return None                            # 開催が無い（別の日のページ）か取れない
    sched, day_no, n_days, r1 = got
    r1["rno"] = 1

    def one(rno):
        h = fetch(sess, date, jcd, rno)
        g = parse(h, date) if h else None
        if not g:
            return None
        g[3]["rno"] = rno
        return g[3]
    with ThreadPoolExecutor(max(1, workers)) as ex:
        rest = list(ex.map(one, range(2, 13)))
    races = [r1] + [r for r in rest if r]
    set_rank(races)
    return {"date": date, "sched": sched, "day_no": day_no, "n_days": n_days,
            "races": races, "src": "official"}


def set_rank(races):
    """順位 = その日の出走表に出てくる選手の中での得点率の順位（同じ得点率は同じ順位）。
    uchisankaku の順位は節の全選手が相手なので、その日に走らない選手のぶんだけずれる（近似）"""
    tok = {}
    for r in races:
        for x in r["lanes"]:
            if x.get("toban") and x.get("tokuten") is not None:
                tok[x["toban"]] = x["tokuten"]
    vals = sorted(tok.values(), reverse=True)
    for r in races:
        for x in r["lanes"]:
            t = tok.get(x.get("toban"))
            x["rank"] = None if t is None else 1 + sum(1 for v in vals if v > t)
