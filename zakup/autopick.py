"""Автоподбор: сам собирает черновой расчёт по каждому лоту.

Правила утверждены владельцем 24.11.2026, подробное описание — docs/AUTOPICK.md.
Коротко:
  • товар: точная модель из техспеки → похожая (та же модель без суффикса) → аналог по типу;
    в каждом уровне — самый дешёвый по цене «Платина», при этом в наличии важнее, чем «нет на складе»;
  • не хватает на складе — всё равно берём, ставим пометку;
  • аналог по типу: тип корпуса и IP/аналог как в лоте; Мп не ниже требуемого (если лот его указал);
  • монтаж/ремонт: число камер ищем в тексте ТЗ; не нашли — без оценки; всегда «грубая оценка»;
  • ТО и «шум» не оцениваем.
"""
import re

from . import store, supplier

# ---------- требования лота ----------

HANDHELD = re.compile(r"фотосъ[её]м|видеосъ[её]мк|аккумулятор|карт\w* памяти|стабилизац|экшн|оптическ\w* (?:зум|увелич)|видоискател", re.I)

TYPE_RULES = [  # (тип, regex в тексте лота)
    ("портатив", r"(?:портатив|нагрудн|носим)\w*\s+(?:видео)?регистратор|(?:видео)?регистратор\w*\s+(?:портатив|нагрудн|носим)"),
    ("авто", r"автомобил"),
    ("веб", r"\bweb|веб-?камер"),
    ("ptz", r"\bptz\b|поворотн|скоростн[а-я]* купол|360°"),
    ("купол", r"купол|күмбез|dome|турел"),
    ("цилиндр", r"цилиндр|bullet|буллет"),
    ("регистратор", r"видеорегистратор|\bnvr\b|\bxvr\b|\bdvr\b|рекордер"),
]
# какие категории поставщика подходят под тип (по названию категории)
CAT_RULES = {
    "купол": r"купол",
    "цилиндр": r"цилиндр",
    "ptz": r"поворот|ptz|скоростн",
    "регистратор": r"регистратор",
    "портатив": r"портатив",
}


# спецкатегории — не берём как аналог обычной камеры/регистратора
SPECIAL_CAT = re.compile(r"wi-?fi|тепловиз|взрыв|кран|парков|дорожн|мобильн|автоном|портатив|встраив|панорам|модул|комплект", re.I)


CONSUMER = re.compile(r"imou|ezviz|wi-?fi|tp-link tapo|xiaomi", re.I)


def lot_requirements(text, works=False):
    low = (text or "").lower()
    kind = None
    # тип — то, что упомянуто раньше всех (для монтажа — только типы камер)
    hits = []
    for t, rx in TYPE_RULES:
        if works and t not in ("ptz", "купол", "цилиндр"):
            continue
        m = re.search(rx, low)
        if m:
            hits.append((m.start(), t))
    special = [t for _, t in hits if t in ("авто", "веб", "портатив")]
    if special:  # особый товар важнее порядка слов: «Видеорегистратор … автомобильный»
        kind = special[0]
    elif not works and HANDHELD.search(low) and not re.search(r"\bip\b|ip-|сетев|poe", low):
        kind = "ручная"  # ручная видеокамера (фотосъёмка, аккумулятор, карта памяти)
    elif hits:
        kind = min(hits)[1]
    if kind is None and re.search(r"камер", low):
        kind = "камера"
    if re.search(r"\bip\b|ip-|сетев|цифров", low):  # «цифровая камера» в госзакупках = IP
        tech = "ip"
    elif re.search(r"\bahd\b|hdcvi|hd-?tvi|аналогов(?:ая|ые|ую|ой|ых)", low):
        tech = "analog"
    else:
        tech = None
    return {"kind": kind, "tech": tech, "mp_min": lot_mp(low), "ch_min": lot_channels(low),
            "wifi_ok": bool(re.search(r"wi-?fi|беспровод", low))}


def lot_channels(low):
    """Требуемые каналы регистратора: «16-канальный», «16 каналов», «каналов … не менее 16»."""
    vals = []
    for m in re.finditer(r"(?<![\d.,])(\d{1,3})\s*-?\s*(?:х\s*)?канал", low):
        ctx = low[max(0, m.start() - 25):m.start()]
        if re.search(r"до\s*$|аудио|тревож|звук", ctx):
            continue
        vals.append(int(m.group(1)))
    for m in re.finditer(r"канал\w*(?:\s+подключения)?\s+(?:ip-?)?(?:видео)?камер\w*[^.;\d]{0,20}?не менее\s*(\d{1,3})", low):
        vals.append(int(m.group(1)))
    vals = [v for v in vals if 1 <= v <= 256]
    return max(vals) if vals else None


def product_channels(name):
    n = (name or "").upper()
    m = re.search(r"(\d{1,3})\s*-?\s*КАНАЛ", n)
    if m:
        return int(m.group(1))
    m = re.search(r"DS-7\d(\d\d)", n) or re.search(r"(?:NVR|XVR|DVR)-?\d{0,2}?(\d\d)(?:[A-Z-]|$)", n) \
        or re.search(r"TC-R3\d(\d\d)\b", n)
    return int(m.group(1)) if m else None


RES_MP = {"1920": 2, "2560": 4, "2688": 4, "2592": 5, "2880": 5, "3072": 6, "3200": 6, "3840": 8}


def lot_mp(low):
    """Требуемые Мп: явные «4 Мп / 4MP / 4.0МР» или разрешение «2560×1440». Берём максимум."""
    vals = []
    for m in re.finditer(r"(?<![\d.,])(\d{1,2}(?:[.,]\d)?)\s*(?:mp|мп|мр|mр|мегапикс|mpx)", low):
        if re.search(r"фото", low[max(0, m.start() - 60):m.start()]):
            continue  # разрешение фотосъёмки
        if re.search(r"до\s*$", low[max(0, m.start() - 6):m.start()]):
            continue  # «разрешением до 8Мп» — про регистратор, не требование
        if low[m.end():m.end() + 2].strip().startswith("/") or low[max(0, m.start() - 2):m.start()].strip().endswith("/"):
            continue  # «12 мп/8 мп/6 мп» — список режимов записи
        vals.append(float(m.group(1).replace(",", ".")))
    if not vals:  # пиксели — только если Мп не названы явно (3840×2160 часто про монитор)
        for w in re.findall(r"(\d{4})\s*[×xх*]\s*\d{3,4}", low):
            if w in RES_MP:
                vals.append(RES_MP[w])
    vals = [v for v in vals if 1 <= v <= 32]
    return max(vals) if vals else None


def product_mp(name, brand):
    """Мп товара: из названия («4МП», «4MP») или по схеме артикула Hikvision/HiLook/Dahua."""
    n = name or ""
    m = re.search(r"(\d{1,2}(?:[.,]\d)?)\s*(?:MP|Мп|МП|Mp|мп|Mpx)\b", n)
    if m:
        return float(m.group(1).replace(",", "."))
    u = n.upper()
    m = re.search(r"DS-2C[DE](\d)(\d)(\d)", u)          # Hikvision DS-2CD1043 → 3-я цифра
    if m:
        return float(m.group(3)) or None
    m = re.search(r"IPC-[A-Z](\d)(\d)\d", u)              # HiLook IPC-B140 → 2-я цифра
    if m and (brand or "").lower().startswith("hilook"):
        return float(m.group(2)) or None
    m = re.search(r"(?:IPC|HAC)-[A-Z]+(\d)(\d)\d\d", u)   # Dahua IPC-HDW2441 → 2-я цифра
    if m:
        return float(m.group(2)) or None
    return None


# ---------- количество камер в ТЗ монтажа ----------

CAP_BEFORE = re.compile(r"(до|не менее|не более|на|от|отображать|подключени[ея]|поддержк\w*|рассчитан\w*)\s*$", re.I)


N = r"(?<![\d.,])(\d{1,3})(?![\d.,])"  # отдельное число, не кусок «802.3af» или «2016»


def count_cameras(text):
    """Ищем число камер в русском тексте ТЗ. None — если не уверены."""
    t = re.sub(r"\s+", " ", text or "")
    # 1) явные формулировки: «общее количество камер – 9», «количество — 5 (пять) видеокамер»
    strong = re.findall(r"количеств\w*\s+(?:ip-?)?(?:видео)?камер\w*[^\d.;•:]{0,40}?[:–—-]?\s*" + N, t, re.I)
    strong += re.findall(r"количеств\w*[^\d.;•]{0,15}?" + N + r"\s*(?:\([а-я]+\)\s*)?(?:ip-?)?(?:видео)?камер", t, re.I)
    strong += re.findall(r"(?:установк\w*|монтаж\w*|подключени\w*)\s+(?:и\s+\w+\s+)?(?:всех\s+)?" + N + r"\s*(?:ip-?)?(?:видео)?камер", t, re.I)
    if strong:
        return int(strong[0])
    # 2) таблица: «Уличная IP-видеокамера PTZ 5 шт.» или «IP-видеокамера, 5 Мп | шт. | 45» — суммируем строки
    total = 0
    for m in re.finditer(r"(?:видео)?камер\w*[^.;•]{0,40}?(?:" + N + r"\s*шт|\bшт\.?\s*" + N + r")", t, re.I):
        if re.search(r"(?:для|монтажа|установки|крепления)\s+(?:ip-?)?(?:видео)?$", t[max(0, m.start() - 20):m.start()], re.I):
            continue  # «коробка для монтажа камер, 9 шт» — не камеры
        total += int(m.group(1) or m.group(2))
    if total:
        return total
    # 3) самое частое «N видеокамер», кроме ёмкости («до 16», «отображать 32»)
    found = []
    for m in re.finditer(N + r"\s*(?:\([а-я]+\)\s*)?(?:шт\.?\s*|комплект\w*\s+)?(?:уличн\w*\s+|внутренн\w*\s+|наружн\w*\s+|неработ\w*\s+)?(?:ip-?)?(?:видео)?камер", t, re.I):
        if CAP_BEFORE.search(t[max(0, m.start() - 25):m.start()]):
            continue
        found.append(int(m.group(1)))
    found += [int(x) for x in re.findall(r"камер\w*[^.()]{0,40}\(" + N + r"\s*шт", t, re.I)]
    found = [n for n in found if 0 < n <= 300]
    if not found:
        return None
    return max(set(found), key=found.count)


def russian_part(text):
    """В ТЗ часто сначала казахский текст — для поиска чисел берём русскую часть, если она есть."""
    t = text or ""
    i = t.find("Техническая спецификация")
    return t[i:] if i > 0 else t


# ---------- выбор товара ----------

def _better(a, b):
    """a лучше b: в наличии важнее, потом дешевле."""
    ka = (a["quantity"] <= 0, a["cost"] or 1e18)
    kb = (b["quantity"] <= 0, b["cost"] or 1e18)
    return ka < kb


def cheapest(rows):
    best = None
    for r in rows:
        if not r.get("cost"):
            continue
        if best is None or _better(r, best):
            best = r
    return best


def type_analog(req):
    """Самый дешёвый товар нужного типа; Мп не ниже требуемого, если лот его указал."""
    cats = supplier.categories()
    if not cats:
        return None, "дерево категорий поставщика ещё загружается"
    kind = req["kind"]
    if kind in (None, "авто", "веб", "ручная"):
        return None, f"нет подходящей категории у поставщика для типа «{kind or 'не определён'}»"
    ids = set()
    for k, c in cats.items():
        name = (c.get("NAME") or "").lower()
        if kind == "камера":
            ok = "камер" in name
        else:
            ok = bool(re.search(CAT_RULES.get(kind, kind), name))
        if not ok or (kind != "портатив" and SPECIAL_CAT.search(name)):
            continue
        # IP или аналог — смотрим на название категории и её предков
        chain = name + " " + " ".join((cats.get(str(p)) or {}).get("NAME", "").lower() for p in _ancestors(cats, k))
        if req["tech"] == "ip" and not re.search(r"\bip\b|ip ", chain):
            continue
        if req["tech"] == "analog" and not re.search(r"ahd|hdcvi|tvi|аналог", chain):
            continue
        ids |= supplier.descendants(cats, k)
    if not ids:
        return None, "не нашёл категорию нужного типа у поставщика"
    rows = store.q(f"SELECT * FROM supplier_products WHERE category IN ({','.join('?' * len(ids))})", list(ids))
    cand = []
    for r in rows:
        if not req.get("wifi_ok") and CONSUMER.search(r["name"] or ""):
            continue  # бытовые Wi-Fi камеры — только если лот сам про Wi-Fi
        o = supplier.row_out(r, "аналог по типу", 0)
        if kind == "регистратор":
            if req.get("ch_min"):
                ch = product_channels(r["name"])
                if ch is None or ch < req["ch_min"]:
                    continue
        elif req["mp_min"]:
            mp = product_mp(r["name"], r["brand"])
            if mp is None or mp < req["mp_min"]:
                continue
        cand.append(o)
    best = cheapest(cand)
    if best is None:
        need = (f" от {req['ch_min']} каналов" if kind == "регистратор" and req.get("ch_min")
                else f" от {req['mp_min']:g} Мп" if req["mp_min"] and kind != "регистратор" else "")
        return None, f"нет аналога нужного типа{need} с ценой"
    return best, None


def _ancestors(cats, k):
    c = cats.get(str(k)) or {}
    out, seen = [], set()
    while c.get("parent") and c["parent"] not in seen:
        seen.add(c["parent"])
        out.append(c["parent"])
        c = cats.get(str(c["parent"])) or {}
    return out


def pick_by_model(text, name):
    """Точная модель → похожая. Возвращает (товар, уровень) или (None, None)."""
    m = supplier.match_lot(text, name, limit=30)
    model_rows = [c for c in m["candidates"] if c["why"].startswith("модель")]
    exact = [c for c in model_rows if c["score"] >= 100]
    similar = [c for c in model_rows if c["score"] < 100]
    if cheapest(exact):
        return cheapest(exact), "точная модель", m["models"]
    if cheapest(similar):
        return cheapest(similar), "похожая модель", m["models"]
    return None, None, m["models"]


def item_of(p, qty, tier):
    return {"article": p["article"], "name": p["name"], "cost": p["cost"], "cost_src": p["cost_src"],
            "is_camera": p["is_camera"], "qty": qty, "stock": p["quantity"], "tier": tier}


# ---------- главный вход ----------

def autopick(lot, spec):
    """lot — lot_summary(), spec — техспека. Возвращает черновой расчёт или причину отказа."""
    cat = lot["category"]
    if cat == "ТО":
        return {"ok": False, "reason": "техобслуживание — товара нет, считайте вручную"}
    if cat == "шум":
        return {"ok": False, "reason": "не про видеонаблюдение (интернет, связь)"}
    raw = (spec or {}).get("raw") or (spec or {}).get("desc") or ""
    text = " ".join(filter(None, [lot["name"], lot["short"], lot["announcement"], raw]))
    works = cat in ("монтаж", "ремонт")
    req = lot_requirements(" ".join(filter(None, [lot["name"], lot["short"], russian_part((spec or {}).get("desc") or raw)])), works)
    notes = []

    if works:
        cams = count_cameras(russian_part(raw))
        if not cams:
            return {"ok": False, "reason": "в ТЗ не нашёл число камер — посчитайте вручную", "req": req}
        req["kind"] = req["kind"] if req["kind"] in ("купол", "цилиндр", "ptz") else "камера"
        items = []
        p, tier, models = pick_by_model(raw, "")
        if p and p["is_camera"]:
            items.append(item_of(p, cams, tier))
        else:
            a, why = type_analog(req)
            if not a:
                return {"ok": False, "reason": f"камер в ТЗ: {cams}, но {why}", "req": req}
            items.append(item_of(a, cams, "аналог по типу"))
            if p:  # найдена модель, но не камера (напр. регистратор) — добавляем 1 шт.
                items.append(item_of(p, 1, tier))
        notes.append(f"камер по ТЗ: {cams}")
        notes.append("грубая оценка: кабель, коробки, регистратор и т.п. из ТЗ не учтены")
        result = {"ok": True, "items": items, "cams": cams, "rough": True}
    else:
        qty = (spec or {}).get("qty") or lot.get("qty_api") or 1
        p, tier, models = pick_by_model(text, lot["name"])
        if not p:
            p, why = type_analog(req)
            if not p:
                return {"ok": False, "reason": why, "req": req, "models": models}
            tier = "аналог по типу"
            if models:
                notes.append("модели из ТЗ у поставщика нет: " + ", ".join(models[:4]))
        result = {"ok": True, "items": [item_of(p, qty, tier)], "rough": tier != "точная модель"}

    for it in result["items"]:
        if it["stock"] < it["qty"]:
            notes.append(f"не хватает на складе: {it['name']} — есть {it['stock']} из {it['qty']:g}")
        if it["cost_src"] != "платина":
            notes.append(f"{it['name']}: {it['cost_src']}")
    if req.get("kind") == "регистратор" and req.get("ch_min"):
        notes.append(f"требование лота: от {req['ch_min']} каналов")
    elif req.get("mp_min"):
        notes.append(f"требование лота: от {req['mp_min']:g} Мп")
    result.update(tier=result["items"][0]["tier"], notes=notes, req=req)
    return result
