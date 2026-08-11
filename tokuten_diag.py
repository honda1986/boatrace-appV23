#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
tokuten_diag.py -- コース別1着率が取れない原因を調べる

  実ページの表の行ラベルと値をそのまま出す。
  どのラベルがどのブロックに割り当てられているかも表示する。

  使い方 (Colab)
    !pip -q install requests beautifulsoup4
    !rm -rf v22 && git clone --depth 1 https://github.com/honda1986/v22.git
    !cd v22 && python tokuten_diag.py 12 20260811
"""

import re
import sys

import requests
from bs4 import BeautifulSoup

import tokuten as TK


def main():
    jcd = int(sys.argv[1]) if len(sys.argv) > 1 else 12
    date = sys.argv[2] if len(sys.argv) > 2 else "20260811"
    sess = requests.Session()
    sess.headers.update(TK.UA if hasattr(TK, "UA") else
                        {"User-Agent": "Mozilla/5.0"})
    html = TK.fetch(sess, jcd, date)
    if not html:
        sys.exit(f"取得できません jcd={jcd} date={date}")
    print(f"取得 {len(html):,}バイト\n")

    soup = BeautifulSoup(html, "html.parser")
    blk = ""
    sub = ""
    n = 0
    print("=" * 70)
    print("表の各行  [ブロック] ラベル → 6艇の値")
    print("=" * 70)
    for tr in soup.find_all("tr"):
        cells = tr.find_all(["td", "th"])
        if len(cells) < 7:
            continue
        label = TK.norm("".join(c.get_text() for c in cells[:-6]))
        lab = re.sub(r"\s+", "", label)
        vals = [TK.norm(c.get_text())[:8] for c in cells[-6:]]

        if "今節" in lab:
            blk = "setsu"
        elif "コース別" in lab or "直近" in lab:
            blk = "course"
        elif "モータ" in lab:
            blk = "motor"
        elif "成績" in lab and "今節" not in lab:
            blk = "grade"
        elif "選手" in lab:
            blk = "info"

        mark = ""
        if blk == "course":
            if lab.endswith("ST"):
                mark = " → c_st"
            elif "追い風" in lab:
                mark = " → c_st_oi"
            elif "向い風" in lab:
                mark = " → c_st_muk"
            elif "1着率" in lab:
                mark = " → c_win  ★これが欲しい"
            elif "2着率" in lab:
                mark = " → c_2nd"
            elif "3着率" in lab:
                mark = " → c_3rd"
            elif "3連率" in lab:
                mark = " → c_ren3"
            else:
                mark = " → (割り当てなし)"
        print(f"[{blk:6}] {lab[:26]:<28} {vals}{mark}")
        n += 1
        if n > 40:
            print("... (以下省略)")
            break

    print("\n" + "=" * 70)
    print("parse_page の結果 (1R の6艇)")
    print("=" * 70)
    page = TK.parse_page(html, date)
    if not page:
        print("★ parse_page が None を返しました(日付が一致しない可能性)")
        return
    print(f"day_no={page.get('day_no')}  n_days={page.get('n_days')}  "
          f"races={len(page.get('races', []))}")
    if not page.get("races"):
        return
    for x in page["races"][0]["lanes"]:
        print(f"  {x.get('lane')}号 c_win={x.get('c_win')} "
              f"c_ren3={x.get('c_ren3')} c_st={x.get('c_st')} "
              f"n_win={x.get('n_win')} tok={x.get('tokuten')}")


if __name__ == "__main__":
    main()
