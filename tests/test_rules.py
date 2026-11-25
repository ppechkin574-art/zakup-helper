"""Правила без каталога поставщика: монтаж, лицензия, сигнализация, сравнение замен, поиск по словам.
Запуск: python3 -m unittest discover tests   (docs/ECONOMICS.md — что и почему)"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import server  # noqa: E402  (импорт не запускает сервер — только функции)
from zakup import bom, source, supplier  # noqa: E402


def row(name, brand=""):
    return {"name": name, "brand": brand}


class Install(unittest.TestCase):
    """2.4.2: монтаж при поставке — только при явных словах о работе."""

    def check(self, text, want):
        self.assertEqual(server.install_of("поставка", text)["install"], want, text)

    def test_works_and_repair_always(self):
        self.assertTrue(server.install_of("монтаж", "")["install"])
        self.assertTrue(server.install_of("ремонт", "")["install"])

    def test_explicit_install(self):
        self.check("Поставка видеорегистратора с установкой и настройкой на объекте заказчика", True)
        self.check("Доставка и установка силами поставщика", True)
        self.check("Поставщик осуществляет монтаж оборудования", True)
        self.check("Монтаж и подключение камер", True)

    def test_not_install(self):
        self.check("Видеокамера с кронштейном для монтажа на стену", False)
        self.check("установка ПО на компьютер", False)
        self.check("Тип установки: настенный", False)
        self.check("Видеорегистратор 16 каналов, HDD до 10 ТБ", False)


class License(unittest.TestCase):
    """1: «нужна лицензия» — только лицензия на работу, не на ПО."""

    def test_yes(self):
        for s in ["Наличие лицензии на монтаж, наладку и техническое обслуживание средств охранной сигнализации",
                  "наличие государственной лицензии на строительно-монтажные работы",
                  "лицензия III категории на СМР",
                  "Поставщик должен иметь лицензию на занятие охранной деятельностью"]:
            self.assertTrue(server.LICENSE_RX.search(s), s)

    def test_no(self):
        for s in ["программное обеспечение с бессрочной лицензией на 2016 камер.", "лицензионное ПО Windows"]:
            self.assertFalse(server.LICENSE_RX.search(s), s)


class Alarm(unittest.TestCase):
    """1: стандартное название кода «…охранной сигнализации/системы видеонаблюдения» — не сигнализация."""

    def count(self, text):
        return len(server.ALARM_RX.findall(server.STD_NAME_RX.sub(" ", text)))

    def test_standard_name_ignored(self):
        self.assertEqual(self.count("Работы по устройству (монтажу) охранной сигнализации/системы видеонаблюдения"), 0)
        self.assertEqual(self.count("Услуги по ТО пожарной/охранной сигнализации/системы видеонаблюдения"), 0)

    def test_real_alarm(self):
        self.assertGreaterEqual(self.count("Монтаж охранной сигнализации: извещатели, прибор приемно-контрольный, оповещатель"), 2)


class NotWorse(unittest.TestCase):
    """2.4: «Другие варианты» — не хуже исходного."""

    def ok(self, base, cand):
        return bom.not_worse(row(base), row(cand))[0]

    def test_camera_mp(self):
        self.assertTrue(self.ok("IP-камера 8 Мп 2,8 мм", "IP-камера 8MP"))
        self.assertFalse(self.ok("IP-камера 8 Мп 2,8 мм", "IP-камера 2 Мп 2,8 мм"))
        self.assertFalse(self.ok("DS-2CD1083G2-LIUF (2.8mm)", "DS-2CD1043G2-I (2.8mm)"))  # 8 Мп → 4 Мп по артикулу

    def test_hdd(self):
        self.assertTrue(self.ok("Жёсткий диск 6 ТБ", "Жёсткий диск 8 ТБ"))
        self.assertFalse(self.ok("Жёсткий диск 6 ТБ", "Жёсткий диск 4 ТБ"))

    def test_switch_poe(self):
        self.assertFalse(self.ok("Коммутатор 16 портов PoE", "Коммутатор 16 портов"))
        self.assertFalse(self.ok("Коммутатор 16 портов PoE", "Коммутатор 8 портов PoE"))

    def test_cable(self):
        self.assertFalse(self.ok("U/UTP Cat6 4PR 23AWG Cu", "U/UTP Cat5e 4PR 24AWG Cu"))
        self.assertFalse(self.ok("F/UTP Cat5e 4PR Cu PE", "U/UTP Cat5e 4PR Cu PE"))   # экран
        self.assertFalse(self.ok("U/UTP Cat5e 4PR Cu PE кабель", "U/UTP Cat5e 4PR CCA PE кабель"))  # медь → омеднённый

    def test_channel_size(self):
        self.assertTrue(self.ok("Кабель-канал 20х10", "Кабель-канал 25х16"))
        self.assertFalse(self.ok("Кабель-канал 20х10", "Кабель-канал 12х12"))

    def test_power_supply(self):
        self.assertTrue(self.ok("Источник питания SVC PS-12V5A 12 В 5 А", "Резервированный источник питания SVC BPS-12V10A9CH"))
        self.assertFalse(self.ok("Источник питания 12 В 5 А", "Источник питания 12 В 2 А"))
        self.assertFalse(self.ok("Источник питания 12 В 5 А", "Источник бесперебойного питания 650 ВА"))


class Parsers(unittest.TestCase):
    def test_amp_volt(self):
        self.assertEqual(bom._amp_volt("Источник питания SVC PS-12V5A"), (5.0, 12))
        self.assertEqual(bom._amp_volt("Блок питания 12 В 2 А"), (2.0, 12))
        self.assertEqual(bom._amp_volt("Камера 2 Мп"), (None, None))

    def test_size(self):
        self.assertEqual(bom._size("Кабель-канал 15х10 EKT (200)"), [10, 15])
        self.assertIsNone(bom._size("Гофра ф20"))


class SearchTokens(unittest.TestCase):
    """2.4: «Найти» — по словам, окончания и порядок размеров не важны."""

    def test_size_both_orders(self):
        kinds = dict(supplier._tokens("Кабель-канал 10х15"))
        self.assertEqual(kinds["size"], ("10x15", "15x10"))

    def test_soft_sign(self):
        self.assertEqual(supplier._plain("Разьем"), supplier._plain("Разъем"))

    def test_stop_words(self):
        words = [v for k, v in supplier._tokens("Розетка для монтажа на стену") if k == "word"]
        self.assertNotIn("для", words)
        self.assertNotIn("на", words)


class TextGaps(unittest.TestCase):
    """2.4.1: оборудование из текста ТЗ, которого нет в смете, и работы вне ставки."""

    TEXT = ("Выполнение работ по демонтажу существующего оборудования, монтажу, пусконаладочным работам. "
            "Сервер видеонаблюдения. Источник бесперебойного питания. Шкаф телекоммуникационный 19\".")

    def test_missing_and_works(self):
        missing, works = bom.text_gaps(self.TEXT, [{"name": "IP-камера 4 Мп", "src": "", "tier": ""}])
        self.assertIn("сервер", missing)
        self.assertIn("ИБП", missing)
        self.assertIn("шкаф / стойка", missing)
        self.assertIn(("демонтаж старого оборудования", True), works)
        self.assertIn(("пусконаладка, программирование, интеграция", False), works)  # в ставке — не обязательно

    def test_present_in_estimate(self):
        missing, _ = bom.text_gaps(self.TEXT, [{"name": "ИБП Smart-UPS 1000 ВА", "src": "", "tier": ""}])
        self.assertNotIn("ИБП", missing)

    def test_gap_items_required(self):
        items = bom.gap_items(["сервер"], [("демонтаж старого оборудования", True), ("обучение персонала", False)])
        self.assertEqual([i["name"] for i in items], ["сервер", "демонтаж старого оборудования"])
        self.assertTrue(all(i["manual"] and i["cost"] is None for i in items))
        self.assertTrue(items[1]["work"])


if __name__ == "__main__":
    unittest.main()


class Noise(unittest.TestCase):
    """Слова поиска «видеотрансляции», «системы безопасности» дают и чужие лоты — их в мусор."""
    def lot(self, name):
        return {"name_ru": name, "announcement_name_ru": name, "enstrus": []}

    def test_simbase(self):
        self.assertEqual(source.classify(self.lot(
            "Услуги по технической поддержке «Информационной системы Комплекса услуг безопасности» "
            "на базе лицензионного программного обеспечения SimBASE на 2027 год")), "шум")

    def test_security_maintenance_kept(self):
        self.assertEqual(source.classify(self.lot(
            "Услуги по техническому обслуживанию комплексной инженерно-технической системы безопасности")), "ТО")
        self.assertEqual(source.classify(self.lot(
            "Услуги по техническому обслуживанию оборудования видеотрансляции оперативной обстановки")), "ТО")
