"""Состав монтажного лота из таблиц ТЗ → позиции поставщика.

Правила утверждены владельцем 25.11.2026 (docs/AUTOPICK.md, раздел «Монтаж по таблице ТЗ»):
  • неподобранные позиции не считаем, показываем списком;
  • кабель — как в ТЗ: написано «медь» → только медный, иначе самый дешёвый нужной категории;
  • работы — только 20 000 ₸ за камеру, прокладка кабеля не оплачивается (считаем только цену кабеля);
  • таблица важнее фразы «общее количество камер», расхождение показываем.
"""
import re

from . import store, supplier

KAZ = re.compile(r"[әғқңөұүһіӘҒҚҢӨҰҮҺІ]|\bдана\b|\bжинақ\b", re.I)
UNIT = r"шт|штук\w*|м|метр\w*|п\.?\s*м|компл\w*|ед|упак\w*|бухт\w*|катуш\w*|пар\w*"
QTY_CELL = re.compile(rf"^\s*(\d+(?:[.,]\d+)?)\s*({UNIT})\.?\s*$", re.I)
NUM_CELL = re.compile(r"^\s*(\d+(?:[.,]\d+)?)\s*$")
UNIT_CELL = re.compile(rf"^\s*({UNIT})\.?\s*$", re.I)
NOT_ITEM = re.compile(r"^(количеств|разрешени|тип\b|назначени|мест|срок|гаранти|общее|наименование|описание|№)", re.I)
DEFECT = re.compile(r"^(износ|повреждени|отсутстви|неисправн|несоответстви|необходимост|наличие|состояни)", re.I)
PURE_WORK = re.compile(r"^(демонтаж|диагностик|проверк|тестирован|уборк|вывоз|обучени|пусконалад|настройк|крепление, маркировк)", re.I)
DOCS = re.compile(r"инструкц|документ|паспорт|гарантийн|сертификат", re.I)
PLACE = re.compile(r"въезд|площадк|улиц|территори|вход|двор|перекр|подъезд|коридор|фасад", re.I)
GOODS = re.compile(r"камер|регистратор|диск|коммутатор|кабел|коробк|канал|монитор|ибп|шкаф|кронштейн|разъ[её]м|коннектор|гофр|труб|рукав|блок питан|ups|nvr|utp|ftp", re.I)
WORK = re.compile(r"монтаж(?!н)|демонтаж|пусконалад|наладк|настройк|работ|услуг|прокладк|обучени|проектир|диагностик", re.I)


LINE = re.compile(rf"^\s*\d{{1,3}}[.)]\s*(.{{3,120}}?)\s*[-–—:]\s*(\d+(?:[.,]\d+)?)\s*({UNIT})\.?\s*$", re.I | re.M)


def text_rows(text):
    """Списки строками: «3.Коннектор Rj 45 - 2 шт» → строки таблицы."""
    return [["text", m.group(1).strip(), f"{m.group(2)} {m.group(3)}"] for m in LINE.finditer(text or "")]


def _num(x):
    return float(x.replace(",", "."))


def extract(tables):
    """Позиции из строк таблиц: [{name, desc, qty, unit}]. Казахские дубли пропускаем."""
    items, seen = [], set()
    for row in tables or []:
        cells = [c for c in row[1:] if c is not None]  # row[0] — имя файла
        if not cells or any(KAZ.search(c) for c in cells):
            continue
        qty = unit = None
        qi = None
        for i, c in enumerate(cells):
            m = QTY_CELL.match(c)
            if m and i > 0:
                qty, unit, qi = _num(m.group(1)), m.group(2).lower(), i
        if qty is None:  # «9» и «шт» в соседних ячейках: сначала «число, ед.», потом «ед., число»
            for i in range(1, len(cells) - 1):
                if NUM_CELL.match(cells[i]) and UNIT_CELL.match(cells[i + 1]):
                    qty, unit, qi = _num(cells[i]), cells[i + 1].strip().lower(), i
                    break
            else:
                for i in range(1, len(cells) - 1):
                    if UNIT_CELL.match(cells[i]) and NUM_CELL.match(cells[i + 1]):
                        qty, unit, qi = _num(cells[i + 1]), cells[i].strip().lower(), i
                        break
        if qty is None:
            # «комплект» без числа = 1
            for i, c in enumerate(cells):
                if i > 0 and re.match(r"^\s*компл\w*\s*$", c, re.I):
                    qty, unit, qi = 1.0, "компл", i
        if qty is None or qty <= 0:
            continue
        texts = [c for i, c in enumerate(cells) if i != qi and not NUM_CELL.match(c) and len(c.strip()) > 2
                 and not UNIT_CELL.match(c)]
        if not texts:
            continue
        name, desc = texts[0], " ".join(texts[1:])
        if NOT_ITEM.match(name) or DEFECT.match(name) or DOCS.search(name):
            continue
        if PLACE.search(desc) and not GOODS.search(name):
            continue  # таблица мест установки: «Жалгызкудук | въезд в село | 1 шт»
        key = (name.lower()[:60], qty)
        if key in seen:
            continue
        seen.add(key)
        if unit.startswith("бухт") or unit.startswith("катуш"):
            qty, unit = qty * 305, "м"  # стандартная бухта витой пары
        items.append({"name": name, "desc": desc[:600], "qty": qty, "unit": unit})
    return items


# ---------- параметры товаров поставщика ----------

def hdd_tb(name):
    n = name or ""
    m = re.search(r"(\d+(?:[.,]\d)?)\s*(?:TB|ТБ|Тб|tb)\b", n)
    if m:
        return _num(m.group(1))
    m = re.search(r"\bST(\d{3,5})[A-Z]", n)      # Seagate ST4000VX015 → 4 ТБ
    if m:
        return int(m.group(1)) / 1000
    m = re.search(r"\bWD(\d{2,3})[A-Z]{3,4}", n)  # WD40PURZ → 4 ТБ
    if m:
        return int(m.group(1)) / 10
    return None


def switch_ports(name):
    m = re.search(r"(\d{1,2})\s*-?\s*(?:порт|port|x\s*(?:10/100|FE|GE|RJ))", name or "", re.I)
    return int(m.group(1)) if m else None


def is_poe(name):
    return bool(re.search(r"poe|\d{2}P(?:\b|-)|\dP\b", name or "", re.I))


def diag(name):
    m = re.search(r"(\d{2}(?:[.,]\d)?)\s*(?:\"|”|''|дюйм|inch|″)", name or "", re.I)
    return _num(m.group(1)) if m else None


def ups_va(name):
    m = re.search(r"(\d{3,5})\s*(?:ВА|VA)\b", name or "", re.I)
    return int(m.group(1)) if m else None


def rack_u(name):
    m = re.search(r"(\d{1,2})\s*U\b", name or "")
    return int(m.group(1)) if m else None


# ---------- подбор ----------

def cat_ids(rx, exclude=None):
    cats = supplier.categories()
    ids = set()
    for k, c in cats.items():
        nm = c.get("NAME") or ""
        if re.search(rx, nm, re.I) and not (exclude and re.search(exclude, nm, re.I)):
            ids |= supplier.descendants(cats, k)
    return ids


def cheapest_in(ids, ok=lambda r: True, name_rx=None):
    if not ids and not name_rx:
        return None
    if ids:
        rows = store.q(f"SELECT * FROM supplier_products WHERE category IN ({','.join('?' * len(ids))})", list(ids))
    else:
        rows = store.q("SELECT * FROM supplier_products")
    best = None
    for r in rows:
        if name_rx and not re.search(name_rx, r["name"] or "", re.I):
            continue
        cost = supplier.cost_of(dict(r))
        if not cost or not ok(r):
            continue
        key = (r["quantity"] <= 0, cost)
        if best is None or key < best[0]:
            best = (key, r)
    return supplier.row_out(best[1], "по таблице ТЗ", 0) if best else None


def _need(rx, text, cast=float):
    m = re.search(rx, text, re.I)
    return cast(m.group(1).replace(",", ".")) if m else None


def match_item(it, lot_text):
    """Возвращает (товар|None, вид позиции, пояснение)."""
    from . import autopick as A
    name, full = it["name"], f"{it['name']} {it['desc']}"
    low = full.lower()

    # точная/похожая модель в строке
    p, tier, _ = A.pick_by_model(full, "")
    if p:
        return p, "модель", tier

    if PURE_WORK.match(name) or (WORK.search(name) and not GOODS.search(name)):
        return None, "работы", "в ставке за камеру"
    if re.search(r"расходн|крепёж|крепеж|хомут|дюбел|метиз", name, re.I):
        return None, "мелочи", "расходные материалы"

    if re.search(r"камер", name, re.I) and not re.search(r"коробк|кронштейн|кожух|для\s+(?:монтажа|установки)", name, re.I):
        req = A.lot_requirements(full)
        req["kind"] = req["kind"] if req["kind"] in ("ptz", "купол", "цилиндр") else "камера"
        if not req["tech"]:
            req["tech"] = A.lot_requirements(lot_text)["tech"]
        a, why = A.type_analog(req)
        return a, "камера", why or "аналог по типу"
    if re.search(r"регистратор|\bnvr\b|\bdvr\b|\bxvr\b", name, re.I):
        req = A.lot_requirements(full)
        req["kind"] = "регистратор"
        if re.search(r"\bxvr|\bdvr|гибридн|hdcvi|ahd|tvi|аналогов", low):
            req["tech"] = "analog"
        a, why = A.type_analog(req)
        return a, "регистратор", why or "аналог по типу"
    if re.search(r"ж[её]стк\w* диск|\bhdd\b|накопител", name, re.I):
        tb = _need(r"(\d+(?:[.,]\d)?)\s*(?:тб|tb)", low) or 1
        ids = cat_ids(r"^hdd$|жестк")
        p = cheapest_in(ids, lambda r: (hdd_tb(r["name"]) or 0) >= tb
                        and not re.search(r"внешн|portable|expansion|external|usb|ssd", r["name"] or "", re.I))
        return p, "диск", f"от {tb:g} ТБ"
    if re.search(r"коммутатор|switch", name, re.I):
        ports = _need(r"(\d{1,2})\s*-?\s*(?:порт|port)", low, int)
        poe = "poe" in low
        outdoor = bool(re.search(r"уличн|наружн", low))
        ids = cat_ids(r"уличные коммутатор" if outdoor else r"^коммутатор|управляемые коммутатор|коммутаторы poe",
                      exclude=None if outdoor else r"индустриал|уличн")
        p = cheapest_in(ids, lambda r: (not ports or (switch_ports(r["name"]) or 0) >= ports)
                        and (not poe or is_poe(r["name"])))
        return p, "коммутатор", ", ".join(filter(None, [f"от {ports} портов" if ports else "", "PoE" if poe else ""]))
    if re.search(r"utp|ftp|витая пара|кабель.*cat|cat\.?\s*5|cat\.?\s*6", low) and re.search(r"кабел|utp|ftp|витая", name, re.I):
        copper = bool(re.search(r"медь|медн|copper|\bcu\b", low)) and not re.search(r"омедн", low)
        cat6 = bool(re.search(r"cat\.?\s*6|категори\w* 6", low))
        outdoor = bool(re.search(r"уличн|наружн|outdoor|\bpe\b", low))
        shield = bool(re.search(r"\bf/?ftp|\bs/?ftp|\bftp|экранир", name.lower())) and "u/utp" not in name.lower()
        pairs4 = bool(re.search(r"4\s*[*xх×]\s*2|4\s*пар|4\s*pr", low))
        ids = cat_ids(r"^utp$|витая")

        def ok(r):
            n = (r["name"] or "").lower()
            if copper and re.search(r"cca|омедн", n):
                return False
            if cat6 and not re.search(r"cat\.?\s*6|кат\.?\s*6", n):
                return False
            if not cat6 and not re.search(r"cat\.?\s*5e|кат\.?\s*5e|5e", n):
                return False
            if outdoor and not re.search(r"\bpe\b|outdoor|наруж|уличн", n):
                return False
            if shield and not re.search(r"ftp|экран", n):
                return False
            if pairs4 and not re.search(r"4\s*pr|4\s*[xх×*]\s*2|4\s*пар", n):
                return False
            return True
        p = cheapest_in(ids, ok)
        why = ", ".join(filter(None, ["медь" if copper else "любой проводник", "cat6" if cat6 else "cat5e",
                                       "уличный" if outdoor else "", "экранированный" if shield else "",
                                       "4 пары" if pairs4 else ""]))
        return p, "кабель", why + " · цена за метр"
    if re.search(r"кабель.?канал|кабельный канал", low):
        size = re.search(r"(\d{2,3})\s*[*xх×]\s*(\d{2,3})", low)
        rx = rf"{size.group(1)}\s*[*xх×]\s*{size.group(2)}" if size else None
        p = cheapest_in(cat_ids(r"^кабель канал"), name_rx=rx) if rx else cheapest_in(cat_ids(r"^кабель канал"))
        return p, "кабель-канал", f"{size.group(1)}×{size.group(2)}" if size else ""
    if re.search(r"коробк", name, re.I):
        for_cam = bool(re.search(r"камер", low))
        p = cheapest_in(cat_ids(r"монтажные коробки для видеокамер" if for_cam else r"^коробки (монтажные|распаячные)"),
                        name_rx=r"коробк")
        return p, "коробка", "для камер" if for_cam else ""
    if re.search(r"монитор", name, re.I):
        d = _need(r"(\d{2}(?:[.,]\d)?)\s*(?:\"|”|дюйм|″)", low) or 0
        p = cheapest_in(cat_ids(r"^ мониторы|^мониторы"), lambda r: (diag(r["name"]) or 0) >= d)
        return p, "монитор", f"от {d:g}\"" if d else ""
    if re.search(r"ибп|бесперебойн|ups", name, re.I):
        va = _need(r"(\d{3,5})\s*(?:ва|va)", low, int) or 0
        p = cheapest_in(cat_ids(r"^блоки бесперебойного питания$"), lambda r: (ups_va(r["name"]) or 0) >= va)
        return p, "ИБП", f"от {va} ВА" if va else ""
    if re.search(r"шкаф", name, re.I):
        u = _need(r"(\d{1,2})\s*u\b", low, int) or 0
        p = cheapest_in(cat_ids(r"шкафы настенные" if "наполь" not in low else r"шкафы напольные"),
                        lambda r: (rack_u(r["name"]) or 0) >= u)
        return p, "шкаф", f"от {u}U" if u else ""
    if re.search(r"кронштейн", name, re.I):
        return cheapest_in(cat_ids(r"кронштейны для видеокамер")), "кронштейн", ""
    sec = re.search(r"(\d)\s*[xх×*]\s*(\d+(?:[.,]\d+)?)", name)
    if re.search(r"ввг|швв|пвс|ппв|силов|кабель питан|кввг", name, re.I) and sec:
        brand = re.search(r"ввг\w*|швв\w*|пвс|ппв|кввг", name, re.I)
        b = brand.group(0)[:3] if brand else ""
        n1, n2 = sec.group(1), sec.group(2).replace(".", ",")
        rx = (rf"{b}.*" if b else "") + rf"\b{n1}\s*[xх×]\s*{re.escape(n2)}\b"
        p = cheapest_in(cat_ids(r"силовые кабели|гибкие силовые"), name_rx=rx) or cheapest_in(set(), name_rx=rx)
        return p, "силовой кабель", f"{b.upper()} {n1}×{n2} · цена за метр"
    if re.search(r"гофр", name, re.I):
        d = re.search(r"(\d{2})", name)
        p = cheapest_in(set(), name_rx=rf"гофр.*(?:d\s*|ф\s*|\b){d.group(1)}\b" if d else r"гофр")
        return p, "гофра", (f"d{d.group(1)} · " if d else "") + "цена за метр"
    if re.search(r"металлорукав", name, re.I):
        return cheapest_in(cat_ids(r"^металлорукав")), "металлорукав", "цена за метр"
    if re.search(r"блок питани|источник питани", name, re.I):
        amp = _need(r"(\d+(?:[.,]\d)?)\s*а\b", low) or 0
        volt = _need(r"(\d{1,2})\s*в\b", low, int) or 12

        def ok(r):
            n = r["name"] or ""
            if not re.search(r"блок питани|источник питани|\bбп\b|адаптер питани|power supply", n, re.I):
                return False
            a = re.search(r"(?<![\w.,])(\d+(?:[.,]\d)?)\s*[AА]\b", n)
            v = re.search(r"(\d{1,2})\s*[VВ]\b", n)
            return a and _num(a.group(1)) >= amp and (not v or int(v.group(1)) == volt)
        p = cheapest_in(cat_ids(r"^источники питания$|блоки питания", exclude=r"бесперебой|ибп"), ok)
        return p, "блок питания", f"{volt} В, от {amp:g} А"
    if re.search(r"rj-?45|коннектор", name, re.I):
        return cheapest_in(set(), name_rx=r"rj-?45"), "коннектор", ""
    return None, "прочее", "не знаю, как подобрать"


def build(tables, lot_text):
    """Смета по таблице ТЗ: позиции с товарами + неподобранное + число камер по таблице."""
    rows = extract(tables) or extract(text_rows(lot_text))
    items, unmatched, works = [], [], []
    cams = 0
    for it in rows:
        p, kind, why = match_item(it, lot_text)
        if kind == "работы":
            works.append(it["name"])
            continue
        if p is None:
            unmatched.append(f"{it['name']} — {it['qty']:g} {it['unit']} ({why})")
            continue
        qty = it["qty"]
        items.append({"article": p["article"], "name": p["name"], "cost": p["cost"], "cost_src": p["cost_src"],
                      "is_camera": kind == "камера" or (kind == "модель" and p["is_camera"]),
                      "qty": qty, "stock": p["quantity"], "tier": f"{kind}: {why}" if why else kind,
                      "src": f"{it['name']} — {it['qty']:g} {it['unit']}"})
        if items[-1]["is_camera"]:
            cams += it["qty"]
    return {"rows": len(rows), "items": items, "unmatched": unmatched, "works": works, "cams": int(cams)}
