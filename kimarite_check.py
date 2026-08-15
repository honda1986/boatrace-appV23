# ================================================================
# 決り手データ 収集 & 効果検証  (Colab用)
#
# STEP 0  過去ページが取れるか / 内容が当時のものか
# STEP 1  N日ぶん収集して CSV に保存
# STEP 2  決り手に予測力があるか(モデルを触らずに測る)
#
# 上から順に、1セルずつ実行してください。
# ================================================================


# ================================================================
# セル1: セットアップ
# ================================================================
import os, re, sys, time, shutil, subprocess, requests
import pandas as pd, numpy as np

REPO = "boatrace-appV23"
if os.path.exists(REPO):
    shutil.rmtree(REPO)
subprocess.run(["git", "clone", "-q", "--depth", "1",
                "https://github.com/honda1986/boatrace-appV23", REPO], check=True)
sys.path.insert(0, os.path.abspath(REPO))
for m in list(sys.modules):
    if m in ("tokuten",):
        del sys.modules[m]
import tokuten as TK
from bs4 import BeautifulSoup

BASE = "https://www.boatrace.jp/owpc/pc/race"
SESS = requests.Session()
SESS.headers["User-Agent"] = "Mozilla/5.0 (research; contact via GitHub honda1986)"

SLEEP = 1.5          # 1リクエストあたりの待ち。下げないこと。

print("tokuten 読み込み OK / 決り手対応:",
      "c_sasare" in open(f"{REPO}/tokuten.py", encoding="utf-8").read())


# ================================================================
# セル2: 払戻一覧(公式)から、その日の結果をまとめて取る
# ================================================================
def fetch_results(date):
    """{(jcd, rno): {'hit': '1-2-3', 'pay': 720, 'henkan': False}}"""
    try:
        r = SESS.get(f"{BASE}/pay", params={"hd": date}, timeout=25)
        r.raise_for_status()
        r.encoding = "utf-8"
    except requests.RequestException as e:
        print(f"  払戻一覧 取得失敗 {date} {type(e).__name__}")
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
                z = re.fullmatch(r"([1-6])([1-6])([1-6])",
                                 prev.get_text(strip=True))
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


# ================================================================
# セル3: STEP 0  過去ページが取れるか
# ================================================================
def peek(jcd, date):
    h = TK.fetch(SESS, jcd, date)
    time.sleep(SLEEP)
    if not h:
        return f"{date} jcd={jcd}: 取得できず"
    p = TK.parse_page(h, date)
    if not p:
        return f"{date} jcd={jcd}: {len(h)}文字だがパース不可"
    x = p["races"][0]["lanes"][0]
    return (f"{date} jcd={jcd}: {len(h)}文字 "
            f"{p['races'][0].get('name','')} 日目={p.get('day_no')} "
            f"| {x.get('name')} c_win={x.get('c_win')} "
            f"差され={x.get('c_sasare')} 今節ST={x.get('st_setsu')} "
            f"今節走数={x.get('n_runs')}")

print("=== 過去ページの可否 ===")
for d in ("20260814", "20260701", "20260401", "20251101"):
    print(peek(21, d))

print("""
確認ポイント
  1. 古い日付でもページが返り、c_sasare に数値が入るか
  2. 「今節走数」が当時の日数に見合っているか
     → 最終日を過ぎた節なのに走数が少ない等の不自然が無ければ
       ページは当時のまま保存されている(リークなし)と判断できる
""")


# ================================================================
# セル4: STEP 1  収集
#   1日あたり: 払戻1回 + 開催場ぶんの出走表
#   30日で概ね400〜600リクエスト、SLEEP=1.5 なら15〜25分
# ================================================================
def collect_day(date):
    res = fetch_results(date)
    time.sleep(SLEEP)
    if not res:
        return []
    jcds = sorted({j for j, _ in res})
    rows = []
    for jcd in jcds:
        h = TK.fetch(SESS, jcd, date)
        time.sleep(SLEEP)
        if not h:
            continue
        p = TK.parse_page(h, date)
        if not p:
            continue
        for race in p["races"]:
            r = res.get((jcd, race["rno"]))
            if not r or r["henkan"]:
                continue
            fin = [int(v) for v in r["hit"].split("-")]
            for x in race["lanes"]:
                lane = x["lane"]
                rows.append({
                    "date": date, "jcd": jcd, "rno": race["rno"],
                    "lane": lane,
                    "hit": r["hit"], "pay": r["pay"],
                    "fin": fin.index(lane) + 1 if lane in fin else 4,
                    # 既存の特徴量
                    "c_win": x.get("c_win"), "c_ren3": x.get("c_ren3"),
                    "c_st": x.get("c_st"), "n_win": x.get("n_win"),
                    "m_2ren": x.get("m_2ren"), "tok": x.get("tokuten"),
                    # 決り手
                    "sasare": x.get("c_sasare"),
                    "makurare": x.get("c_makurare"),
                    "sasare_from": x.get("c_sasare_from"),
                    "makurare_from": x.get("c_makurare_from"),
                    "sashi": x.get("c_sashi"),
                    "makurizashi": x.get("c_makurizashi"),
                    "makuri": x.get("c_makuri"),
                    "ktype": x.get("c_type"),
                })
    return rows


def collect(dates, out_csv="kimarite.csv"):
    all_rows = []
    for i, d in enumerate(dates, 1):
        rows = collect_day(d)
        all_rows += rows
        print(f"[{i}/{len(dates)}] {d}  {len(rows)//6:>3}レース  "
              f"累計 {len(all_rows)//6:>5}レース")
        if i % 10 == 0 and all_rows:
            pd.DataFrame(all_rows).to_csv(out_csv, index=False)
    df = pd.DataFrame(all_rows)
    df.to_csv(out_csv, index=False)
    print(f"\n保存 {out_csv}  {len(df)}行 / {len(df)//6}レース")
    return df


# まず30日で様子を見る。足りなければ日付を増やす。
DATES = [d.strftime("%Y%m%d") for d in
         pd.date_range("2026-07-15", "2026-08-13")]
df = collect(DATES)

# Google Drive に残す場合
# from google.colab import drive; drive.mount('/content/drive')
# df.to_csv('/content/drive/MyDrive/kimarite.csv', index=False)


# ================================================================
# セル5: STEP 2  決り手に予測力があるか
#
# 前提: 2〜6号艇の 差し+捲り差し+捲り = 1着率 なので、
#       1着予想には新情報が無い。効くとすれば2着・3着。
#       そこを直接測る。
# ================================================================
# df = pd.read_csv("kimarite.csv")     # 再開する場合

def auc(x, y):
    """簡易AUC。0.5=無意味、0.55以上なら見込みあり"""
    m = ~(pd.isna(x) | pd.isna(y))
    x, y = np.asarray(x)[m], np.asarray(y)[m]
    if len(set(y)) < 2:
        return np.nan, 0
    r = pd.Series(x).rank().values
    n1, n0 = (y == 1).sum(), (y == 0).sum()
    return (r[y == 1].sum() - n1 * (n1 + 1) / 2) / (n1 * n0), len(x)


print("=" * 60)
print("検証A: 1号艇が負けたとき、2着に残るか")
print("  差され率が高い選手は「かわされても2着」が多いはず")
print("=" * 60)
b1 = df[(df.lane == 1) & (df.fin != 1)].copy()
b1["stay2"] = (b1.fin == 2).astype(int)
for col in ("sasare", "makurare", "c_win", "c_ren3"):
    a, n = auc(b1[col], b1.stay2)
    print(f"  {col:<12} AUC={a:.3f}  n={n}")
print(f"  ※ 1号艇が負けたレース {len(b1)}件 / "
      f"うち2着 {b1.stay2.sum()}件 ({b1.stay2.mean():.1%})")

print()
print("=" * 60)
print("検証B: 外枠が勝ったとき、1号艇を何着まで落とすか")
print("  捲りで勝つ艇は1号艇を3着以下に飛ばすはず")
print("=" * 60)
win = df[(df.lane != 1) & (df.fin == 1)][["date", "jcd", "rno",
                                          "lane", "sashi",
                                          "makurizashi", "makuri"]]
one = df[df.lane == 1][["date", "jcd", "rno", "fin"]].rename(
    columns={"fin": "fin1"})
mg = win.merge(one, on=["date", "jcd", "rno"])
mg["drop1"] = (mg.fin1 >= 3).astype(int)      # 1号艇が3着以下
tot = mg[["sashi", "makurizashi", "makuri"]].sum(axis=1)
mg["mk_ratio"] = (mg.makuri + mg.makurizashi) / tot.replace(0, np.nan)
a, n = auc(mg["mk_ratio"], mg["drop1"])
print(f"  捲り系比率     AUC={a:.3f}  n={n}")
print(f"  ※ 外枠が勝ったレース {len(mg)}件 / "
      f"うち1号艇3着以下 {mg.drop1.sum()}件 ({mg.drop1.mean():.1%})")

print()
print("=" * 60)
print("検証C: タイプ別の着順分布")
print("=" * 60)
if "ktype" in df and df.ktype.notna().any():
    print(df.pivot_table(index="ktype", columns="fin",
                         values="lane", aggfunc="count",
                         fill_value=0))

print("""
読み方
  AUC 0.50前後      → 効果なし。ここで打ち切って構わない
  AUC 0.53〜0.55    → 弱い。再学習しても回収率は動かない可能性が高い
  AUC 0.55以上      → 見込みあり。p2/p3 の再学習に進む価値がある

回収率そのものは、p2/p3 を再学習してからでないと測れません。
このセルは「再学習する価値があるか」を判断するためのものです。
""")
