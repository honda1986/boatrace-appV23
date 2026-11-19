"""uchisankaku が空のとき、公式の出走表から同じ形のページを作る（officialpage）。見本の HTML だけを使い、外には触らない

  python -m unittest discover -s tests
"""
import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import officialpage as OP   # noqa: E402


def load(name):
    with open(os.path.join(HERE, "fixtures", name), encoding="utf-8") as f:
        return f.read()


class TestParse(unittest.TestCase):
    """2026/10/17 下関 9R（2日目）。期待値は同じ日の uchisankaku（v22 tokuten）の値"""

    def setUp(self):
        self.g = OP.parse(load("racelist_20261017_19_9.html"), "20261017")

    def test_日程とレース名(self):
        sched, day_no, n_days, race = self.g
        self.assertEqual((day_no, n_days, race["name"]), (2, 5, "予選"))
        self.assertEqual(sched[0], "20261016")

    def test_出走表の数値は_uchisankaku_と同じ形(self):
        x = self.g[3]["lanes"][0]
        self.assertEqual((x["toban"], x["cls"], x["name"], x["age"]), (4531, "A2", "武富 智亮", 37.0))
        self.assertEqual((x["n_win"], x["n_2ren"], x["l_win"], x["l_2ren"], x["m_2ren"]),
                         (6.21, 48.03, 5.53, 36.11, 34.95))     # 2連率は % のまま
        self.assertEqual((x["f_count"], x["c_st"]), (0.0, 0.16))

    def test_今節成績は前日までの走から(self):
        L = self.g[3]["lanes"]
        self.assertEqual([x["n_runs"] for x in L], [2, 2, 1, 1, 2, 1])
        self.assertEqual([x["st_setsu"] for x in L], [0.11, 0.18, 0.26, 0.14, 0.15, 0.31])
        self.assertEqual([x["tokuten"] for x in L], [6.0, 7.0, 2.0, 1.0, 4.5, 2.0])
        self.assertEqual(L[5]["c_st"], 0.0)                   # 新人の平均ST「-」は 0.0

    def test_順位はその日の選手の中での得点率の順位(self):
        races = [self.g[3]]
        OP.set_rank(races)
        self.assertEqual([x["rank"] for x in races[0]["lanes"]], [2, 1, 4, 6, 3, 4])

    def test_初日(self):
        g = OP.parse(load("racelist_20261017_16_1.html"), "20261017")
        self.assertEqual(g[1], 1)
        self.assertTrue(all(x["n_runs"] == 0 and x["tokuten"] is None for x in g[3]["lanes"]))


class TestVenue(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.old = (OP.CACHE, OP.fetch)
        OP.CACHE = self.tmp
        self.n = 0

        def fake(sess, date, jcd, rno, tries=2):
            self.n += 1
            return load("racelist_20261017_19_9.html") if jcd == 19 else None
        OP.fetch = fake

    def tearDown(self):
        OP.CACHE, OP.fetch = self.old
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_12レースを1日1回(self):
        p = OP.fetch_venue(None, 19, "20261017")
        self.assertEqual(([r["rno"] for r in p["races"]], p["day_no"]), (list(range(1, 13)), 2))
        OP.fetch_venue(None, 19, "20261017")
        self.assertEqual(self.n, 12)

    def test_開催が無い場は_None_で覚えておく(self):
        self.assertIsNone(OP.fetch_venue(None, 4, "20261017"))
        self.assertIsNone(OP.fetch_venue(None, 4, "20261017"))
        self.assertEqual(self.n, 1)


if __name__ == "__main__":
    unittest.main()
