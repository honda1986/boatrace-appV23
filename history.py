#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
history.py -- 日別の予想ファイルから、成績と収支の集計を作る

■ ファイル
  yosou/days/YYYYMMDD.json … その日の予想と結果(yosou.py が書く。data.json と同じ形)
  yosou/history.json       … 日別の集計 / 勝負レースの明細 / 確率の較正
  yosou/races.json         … 結果の出た全レースの予想1〜3位と決まり目・払戻
                             (アプリの収支画面で、v24 と同じ買い方で比べるのに使う)

■ 収支の数え方
  勝負レース(shobu)の買い目(buy)を、記録された金額 y で買ったものとする。
  払戻は実際の払戻金で計算する(y × 払戻金 / 100)。
  買い目のオッズは取得した時点のもので、締切直前とは違うため使わない。
  結果が出ていないレース(発売中・中止)は数えない。
  オッズを使わなかった頃の勝負レース(買い目が無い日)は収支に入れない。

■ 使い方
  python history.py                # days/ から history.json を作り直す
  python history.py --backfill     # git の履歴から過去の日を days/ に復元する
                                   # (全履歴が要る: git fetch --unshallow)
"""

import argparse
import glob
import json
import os
import re
import subprocess
from datetime import datetime, timedelta, timezone

JST = timezone(timedelta(hours=9))
VERSION = 2

# 1着確率の帯(較正を見る用)。全艇ぶん数える
BANDS = [0, 0.05, 0.10, 0.20, 0.30, 0.45, 0.60, 1.01]
# 本命の判定(画面の 混戦 / 中位 / 堅い と同じ境目)
JUDGE = [0, 0.38, 0.55, 1.01]
RACES_VERSION = 2               # races.json の形。変えたら上げる(古い形は作り直す)


def band(p, edges):
    for i in range(len(edges) - 1):
        if edges[i] <= p < edges[i + 1]:
            return i
    return len(edges) - 2


def is_retro(d):
    """後から作った予想か。当日の実行なら updated の日付は対象日と同じになる"""
    up = (d.get("updated") or "")[:10].replace("-", "")
    return bool(up) and up != d.get("date")


def summarize(d):
    """1日ぶんの集計と、勝負レースの明細"""
    date = d.get("date")
    fav = [0, 0, 0.0]                 # 本命(1着確率が最大の艇): 数 / 1着 / 確率の和
    t8 = [0, 0, 0.0]                  # 3連単 出やすい順の上位8組: 数 / 的中 / 確率の和
    t1 = [0, 0]                       # 3連単 予想1位: 数 / 的中
    sb = [0, 0, 0, 0, 0]              # 勝負: 数 / 結果あり / 的中 / 投資 / 払戻
    cal = [[0, 0, 0.0] for _ in BANDS[:-1]]
    fj = [[0, 0, 0.0] for _ in JUDGE[:-1]]
    bets = []
    nr = ns = legacy_sb = 0
    has_buy = False
    for v in d.get("venues") or []:
        for r in v.get("races") or []:
            nr += 1
            res = r.get("result") or {}
            hit = res.get("hit")
            boats = r.get("boats") or []
            if r.get("buy"):
                has_buy = True
            if hit:
                ns += 1
                first = int(hit.split("-")[0])
                tp = r.get("top_p") if r.get("top_p") is not None else r.get("p1")
                tl = r.get("top_lane") or 1
                if tp is not None:
                    fav[0] += 1
                    fav[1] += int(first == tl)
                    fav[2] += tp
                    j = fj[band(tp, JUDGE)]
                    j[0] += 1
                    j[1] += int(first == tl)
                    j[2] += tp
                for b in boats:
                    if b.get("p") is None:
                        continue
                    c = cal[band(b["p"], BANDS)]
                    c[0] += 1
                    c[1] += int(b["lane"] == first)
                    c[2] += b["p"]
                cs = [z["c"] for z in (r.get("combos") or [])[:8]]
                if cs:
                    t8[0] += 1
                    t8[1] += int(hit in cs)
                    t8[2] += sum(z["p"] for z in r["combos"][:8])
                    t1[0] += 1
                    t1[1] += int(cs[0] == hit)
            if not r.get("shobu"):
                continue
            buy = r.get("buy") or []
            if not buy:
                legacy_sb += 1               # オッズを見ない頃の勝負。金額が無い
                continue
            cost = sum(int(z.get("y") or 0) for z in buy)
            row = {"d": date, "j": v.get("jcd"), "v": v.get("venue"),
                   "r": r.get("rno"), "t": r.get("net") or r.get("close"),
                   "n": len(buy), "c": cost, "x": r.get("X"),
                   "oa": r.get("odds_at"), "nf": bool(r.get("notified_final"))}
            sb[0] += 1
            if hit and res.get("pay") is not None:
                z = next((z for z in buy if z["c"] == hit), None)
                ret = int(round((z.get("y") or 0) * res["pay"] / 100)) if z else 0
                row.update({"h": hit, "p": res["pay"], "rt": ret,
                            "k": (buy.index(z) + 1) if z else None})
                sb[1] += 1
                sb[2] += int(z is not None and ret > 0)
                sb[3] += cost
                sb[4] += ret
            bets.append(row)

    r3 = lambda x: round(x, 3)
    out = {"date": date, "upd": d.get("updated"),
           "nv": len(d.get("venues") or []), "nr": nr, "ns": ns,
           "fav": [fav[0], fav[1], r3(fav[2])],
           "t8": [t8[0], t8[1], r3(t8[2])], "t1": t1, "sb": sb,
           "cal": [[a, b, r3(c)] for a, b, c in cal],
           "fj": [[a, b, r3(c)] for a, b, c in fj]}
    if is_retro(d):
        out["retro"] = True
    if legacy_sb and not has_buy:
        out["legacy"] = legacy_sb
    return out, bets


def race_rows(d):
    """結果の出た全レースを [場, R, [予想1〜3位], 決まり目, 払戻] で返す。
    後から作った予想の日は入れない"""
    if is_retro(d):
        return []
    rows = []
    for v in d.get("venues") or []:
        for r in v.get("races") or []:
            res = r.get("result") or {}
            cs = r.get("combos") or []
            if not res.get("hit") or res.get("pay") is None or len(cs) < 2:
                continue
            rows.append([v.get("jcd"), r.get("rno"), [z["c"] for z in cs[:3]],
                         res["hit"], res["pay"]])
    return rows


def write_json(path, data):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, path)


def load_day(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def build(days_dir="yosou/days", out="yosou/history.json", only=None, full=False,
          races_out=None):
    """days/ を集計して history.json と races.json を書く。
    前回の集計を使い回し、only に入っている日(と未集計の日)だけ計算し直す。"""
    races_out = races_out or os.path.join(os.path.dirname(out) or ".", "races.json")
    cache = {}
    rcache = {}
    if not full and os.path.exists(races_out):
        try:
            rj = json.load(open(races_out, encoding="utf-8"))
            if rj.get("v") == RACES_VERSION:
                rcache = rj.get("days") or {}
        except Exception:
            rcache = {}
    if not full and os.path.exists(out):
        try:
            h = json.load(open(out, encoding="utf-8"))
            if h.get("v") == VERSION:
                for s in h.get("days") or []:
                    cache[s["date"]] = s
                for b in h.get("bets") or []:
                    if b["d"] in cache:
                        cache[b["d"]].setdefault("_bets", []).append(b)
        except Exception:
            cache = {}
    only = set(only or [])
    days, bets, races = [], [], {}
    for p in sorted(glob.glob(os.path.join(days_dir, "*.json"))):
        dt = os.path.basename(p)[:8]
        if not re.fullmatch(r"\d{8}", dt):
            continue
        if dt in cache and dt in rcache and dt not in only:
            s = cache[dt]
            bl = s.pop("_bets", [])
            rows = rcache[dt]
        else:
            try:
                d = load_day(p)
                s, bl = summarize(d)
                rows = race_rows(d)
            except Exception as e:
                print(f"  {dt} を集計できません {type(e).__name__}: {e}")
                continue
        days.append(s)
        bets.extend(bl)
        races[dt] = rows                  # 結果の無い日も空で残す(次回に読み直さないため)
    days.sort(key=lambda s: s["date"], reverse=True)
    bets.sort(key=lambda b: (b["d"], b.get("t") or "", b.get("j") or 0, b.get("r") or 0),
              reverse=True)
    data = {"v": VERSION,
            "updated": datetime.now(JST).strftime("%Y-%m-%d %H:%M"),
            "latest": days[0]["date"] if days else None,
            "bands": BANDS, "judge": JUDGE,
            "days": days, "bets": bets}
    write_json(out, data)
    write_json(races_out, {"v": RACES_VERSION, "updated": data["updated"],
                           "cols": ["jcd", "rno", "top", "hit", "pay"],
                           "days": races})
    return data


def record(stats):
    """通知などに添える、これまでの勝負レースの実績"""
    n = hit = cost = ret = 0
    for s in stats.get("days") or []:
        if s.get("retro"):
            continue
        _, settled, h, c, r = s["sb"]
        n += settled
        hit += h
        cost += c
        ret += r
    if not n or not cost:
        return None
    return {"n": n, "hit": hit, "cost": cost, "ret": ret,
            "hit_rate": hit / n, "roi": ret / cost}


# ---------------------------------------------------------------- 履歴からの復元
DATE_PAT = re.compile(rb'"date"\s*:\s*"(\d{8})"')


def backfill(days_dir="yosou/days", path="yosou/data.json", ref="HEAD",
             overwrite=False):
    """git の履歴にある data.json から、日ごとの最終版を days/ に書き出す。
    同じ日付の版が複数あるときは「その日のうちに作られた最後の版」を選ぶ。
    後日 --date で作り直した版(当日の版が無い日だけ)は、後から作った予想になる。"""
    log = subprocess.check_output(
        ["git", "log", "--format=%H %ct", ref, "--", path], text=True).split()
    commits = list(zip(log[0::2], log[1::2]))       # 新しい順
    print(f"{path} の履歴 {len(commits)}件を調べます")
    proc = subprocess.Popen(["git", "cat-file", "--batch"], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE)
    best = {}                                       # date -> (live, ct, sha)
    for sha, ct in commits:
        proc.stdin.write(f"{sha}:{path}\n".encode())
        proc.stdin.flush()
        head = proc.stdout.readline().split()
        if len(head) < 3 or head[1] != b"blob":
            continue
        size = int(head[2])
        blob = proc.stdout.read(size)
        proc.stdout.read(1)
        m = DATE_PAT.search(blob[:200])
        if not m:
            continue
        date = m.group(1).decode()
        made = datetime.fromtimestamp(int(ct), JST).strftime("%Y%m%d")
        live = made == date
        cur = best.get(date)
        # 新しい順に見ているので、最初に見つけた「当日の版」が最終版
        if cur is None or (live and not cur[0]):
            best[date] = (live, int(ct), blob)
    proc.stdin.close()
    proc.wait()
    os.makedirs(days_dir, exist_ok=True)
    wrote = []
    for date in sorted(best):
        live, ct, blob = best[date]
        dst = os.path.join(days_dir, f"{date}.json")
        if os.path.exists(dst) and not overwrite:
            continue
        try:
            json.loads(blob)
        except ValueError:
            print(f"  {date} は壊れているので飛ばします")
            continue
        with open(dst, "wb") as f:
            f.write(blob)
        wrote.append(date)
        print(f"  {date} {'当日版' if live else '後日版'}")
    print(f"{len(wrote)}日ぶん書き出しました")
    return wrote


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", default="yosou/days")
    ap.add_argument("--out", default="yosou/history.json")
    ap.add_argument("--backfill", action="store_true",
                    help="git の履歴から過去の日を復元してから集計する")
    ap.add_argument("--ref", default="HEAD")
    ap.add_argument("--overwrite", action="store_true",
                    help="--backfill で既にある日も書き直す")
    args = ap.parse_args()
    if args.backfill:
        backfill(args.days, ref=args.ref, overwrite=args.overwrite)
    h = build(args.days, args.out, full=True)
    rec = record(h)
    print(f"{args.out} を更新 ({len(h['days'])}日 / 勝負 {len(h['bets'])}件)")
    if rec:
        print(f"勝負レース実績 {rec['n']}件 的中率{rec['hit_rate']:.1%} "
              f"回収率{rec['roi']:.1%} 収支{rec['ret'] - rec['cost']:+,}円")


if __name__ == "__main__":
    main()
