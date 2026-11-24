"""Подбор по реальному каталогу keeper.kz: строка ТЗ → подходящий товар.
Проверяем свойства товара (экран, сечение, размер, Мп), а не артикул: цены и остатки меняются.
Если каталог ещё не загружен — тесты пропускаются."""
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from zakup import autopick, bom, store, supplier  # noqa: E402

HAVE_CATALOG = bool(store.q("SELECT 1 FROM supplier_products LIMIT 1"))


def pick(name, desc="", qty=1, unit="шт"):
    return bom.match_item({"name": name, "desc": desc, "qty": qty, "unit": unit}, "")


@unittest.skipUnless(HAVE_CATALOG, "каталог поставщика не загружен")
class Match(unittest.TestCase):
    def name_of(self, p):
        self.assertIsNotNone(p, "товар не подобран")
        return p["name"]

    def test_ftp_shielded_outdoor_copper(self):
        p, kind, _ = pick('Кабель FTP HYPERLINK "https://x"', "медь, cat5e, уличный, экранированный, 4 пары", 915, "м")
        n = self.name_of(p).lower()
        self.assertRegex(n, r"ftp|f/utp")
        self.assertRegex(n, r"\bpe\d?\b|уличн|наруж")
        self.assertNotRegex(n, r"\bcca\b")

    def test_channel_nearest_bigger(self):
        p, _, _ = pick("Кабель-канал 10х15", qty=300, unit="м")
        a, b = bom._size(self.name_of(p))
        self.assertGreaterEqual(a, 10)
        self.assertGreaterEqual(b, 15)

    def test_coax_with_power_section(self):
        p, kind, _ = pick("Коаксиальный кабель с питанием 2х 0,75( 300мвбухте)", qty=1250, unit="м")
        self.assertEqual(kind, "кабель с питанием")
        self.assertRegex(self.name_of(p), r"0[.,]75")

    def test_bnc_soft_sign(self):
        p, kind, _ = pick("Разьем  BNC")
        self.assertEqual(kind, "разъём BNC")
        self.assertIn("BNC", self.name_of(p))

    def test_unknown_is_manual(self):
        p, _, _ = pick("Розетка накладная двойная")
        self.assertIsNone(p)  # → строка «впишите цену»

    def test_hdd_size(self):
        p, _, _ = pick("Жесткий диск 4 ТБ")
        self.assertGreaterEqual(bom.hdd_tb(self.name_of(p)) or 0, 4)


@unittest.skipUnless(HAVE_CATALOG, "каталог поставщика не загружен")
class Analog(unittest.TestCase):
    def test_camera_mp_and_stock(self):
        req = {"kind": "цилиндр", "tech": "ip", "mp_min": 8.0, "ch_min": None, "wifi_ok": False}
        p, why = autopick.type_analog(req, 21)
        self.assertIsNotNone(p, why)
        self.assertGreaterEqual(autopick.product_mp(p["name"], p.get("brand")) or 0, 8)
        # если в каталоге есть подходящая камера в нужном количестве — берётся она
        enough = [r for r in store.q("SELECT * FROM supplier_products WHERE quantity>=21")
                  if (autopick.product_mp(r["name"], r["brand"]) or 0) >= 8 and re.search(r"камер|ipc|ds-2cd", r["name"] or "", re.I)]
        if enough:
            self.assertGreaterEqual(p["quantity"], 21)


@unittest.skipUnless(HAVE_CATALOG, "каталог поставщика не загружен")
class Search(unittest.TestCase):
    def test_channel_reversed_size(self):
        r = supplier.search("Кабель-канал 10х15")
        self.assertTrue(r)
        self.assertRegex(r[0]["name"], r"15\s*[xх]\s*10|10\s*[xх]\s*15")

    def test_rare_word_first(self):
        r = supplier.search("Разьем BNC")
        self.assertIn("BNC", r[0]["name"])

    def test_model(self):
        r = supplier.search("DS-2CD1043")
        self.assertIn("DS-2CD1043", r[0]["name"])


if __name__ == "__main__":
    unittest.main()
