# -*- coding: utf-8 -*-
"""Kファイルからモーター純度を計算し motor/latest.json に出す。

mot_pure = そのモーターの直近10走について
           (その走の展示レース内偏差) - (その選手の平常の展示偏差)
           を直近重み付きで平均したもの。
当日ぶんは使わない(前日までのKファイルだけで作れる)。
出力は登録番号(toban)を引数にした辞書。節の間は選手とモーターが
固定されるので、翌日の出走表からは toban だけで引ける。
"""
import argparse, datetime, glob, gzip, json, os
import numpy as np
from collections import defaultdict, deque

MAXLEN, MINRUN, RACER_LEN = 10, 5, 100


def load(kdir, days):
    """直近days日ぶんのKファイルを新しい順に読み、古い順で返す"""
    ps = sorted(glob.glob(f"{kdir}/*.json.gz"))[-days:]
    recs = []
    for p in ps:
        d = os.path.basename(p)[:8]
        with gzip.open(p, "rt", encoding="utf-8") as f:
            kd = json.load(f)
        for r in kd["races"]:
            es = r.get("entries") or []
            if len(es) != 6:
                continue
            lane = [e.get("lane", 0) for e in es]
            if sorted(lane) != [1, 2, 3, 4, 5, 6]:
                continue
            o = np.argsort(lane)
            tj = np.array([es[i].get("tenji") or np.nan for i in o], float)
            tj[tj <= 0] = np.nan
            recs.append((d, int(r["jcd"]),
                         [es[i].get("motor") or 0 for i in o],
                         [es[i].get("toban") or 0 for i in o], tj))
    recs.sort(key=lambda x: x[0])
    return recs


def build(recs):
    """最終日時点での (jcd,motor) -> 純度 と toban -> (jcd,motor) を作る"""
    mot = defaultdict(lambda: deque(maxlen=MAXLEN * 3))
    rac = defaultdict(lambda: deque(maxlen=RACER_LEN))
    seen = {}                              # toban -> (日付, jcd, motor)
    for d, jcd, mts, tbs, tj in recs:
        with np.errstate(invalid="ignore"):
            dev = tj - np.nanmean(tj)
        for j in range(6):
            v = dev[j]
            t = tbs[j]
            seen[t] = (d, jcd, mts[j])
            if np.isnan(v):
                continue
            base = np.mean(rac[t]) if len(rac[t]) >= 5 else 0.0
            mot[(jcd, mts[j])].append((d, v - base))
            rac[t].append(v)

    pure = {}
    for key, dq in mot.items():
        a = [v for _, v in dq][-MAXLEN:]
        if len(a) < MINRUN:
            continue
        w = np.linspace(0.5, 1.5, len(a))
        pure[key] = float(np.average(a, weights=w))
    return pure, seen


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--kfile", default="kfile")
    ap.add_argument("--out", default="motor/latest.json")
    ap.add_argument("--days", type=int, default=400)
    args = ap.parse_args()

    recs = load(args.kfile, args.days)
    if not recs:
        print("Kファイルがありません")
        return
    last = recs[-1][0]
    print(f"Kファイル {args.days}日  {recs[0][0]}〜{last}  {len(recs):,}レース")

    pure, seen = build(recs)
    print(f"モーター {len(pure):,}台ぶんの純度")

    # 直近7日に走っている選手だけ(節が終わればモーターも変わる)
    lim = (datetime.date(int(last[:4]), int(last[4:6]), int(last[6:8]))
           - datetime.timedelta(days=7)).strftime("%Y%m%d")
    vals = {}
    for t, (d, jcd, mt) in seen.items():
        if d < lim or not t:
            continue
        v = pure.get((jcd, mt))
        if v is not None:
            vals[str(t)] = round(v, 5)

    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    json.dump({"date": last, "maxlen": MAXLEN, "racer_len": RACER_LEN,
               "values": vals},
              open(args.out, "w", encoding="utf-8"), ensure_ascii=False)
    a = np.array(list(vals.values()))
    print(f"出力 {args.out}  選手 {len(vals):,}人  "
          f"平均{a.mean():+.4f} 標準偏差{a.std():.4f}")


if __name__ == "__main__":
    main()
