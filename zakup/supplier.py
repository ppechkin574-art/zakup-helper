"""Поставщик keeper.kz: каталог, цены, остатки + сопоставление с техспекой лота.

Лимиты API: 1 запрос / 3 с на токен (categories — 1 / 60 с), до 200 позиций за запрос.
Уровни цен на keeper.kz: final_price = «Платина» (наша закупочная), price = «Золото».
Если платина «по запросу» (final_price = 0) — берём золото и помечаем.
"""
import json
import os
import math
import re
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path

from . import store

BASE = "https://keeper.kz/api"
ROOT = Path(__file__).resolve().parent.parent
_last_call = 0.0
_call_lock = threading.Lock()


def token():
    t = os.environ.get("KEEPER_TOKEN")
    if t:
        return t
    env = ROOT / ".env"
    if env.exists():
        for ln in env.read_text().splitlines():
            if ln.startswith("KEEPER_TOKEN="):
                return ln.split("=", 1)[1].strip()
    raise RuntimeError("нет KEEPER_TOKEN в .env")


def call(path, params=None, min_gap=3.2):
    global _last_call
    url = f"{BASE}/{path}/" + ("?" + urllib.parse.urlencode(params) if params else "")
    with _call_lock:
        for attempt in range(5):
            wait = _last_call + min_gap - time.time()
            if wait > 0:
                time.sleep(wait)
            _last_call = time.time()
            req = urllib.request.Request(url, headers={"Access-Token": token(), "User-Agent": "zakup-helper/1.0"})
            try:
                with urllib.request.urlopen(req, timeout=90) as r:
                    return json.load(r)
            except urllib.error.HTTPError as e:
                if e.code == 429:
                    time.sleep(min_gap * (attempt + 2))
                    continue
                raise
    raise RuntimeError(f"keeper: 429 подряд на {path}")


def cost_of(p):
    fp, rp = p.get("final_price") or 0, p.get("price") or 0
    return fp if fp > 0 else (rp if rp > 0 else None)


def cost_src(p):
    if (p.get("final_price") or 0) > 0:
        return "платина"
    if (p.get("price") or 0) > 0:
        return "золото (платина по запросу)"
    return "цена по запросу"


CAMERA_RE = re.compile(r"камер|IPC|HAC|HFW|HDW|DS-2C|DS-2D|DS-2T|PTZ", re.I)


# ---------- синхронизация ----------

def sync_catalog(progress=lambda s: None):
    """Полная выгрузка товаров (раз в сутки)."""
    items, offset = [], 0
    while True:
        d = call("elements", {"per_page": 200, "offset": offset})
        batch = d.get("products") or []
        items += batch
        progress(f"поставщик: каталог {len(items)}")
        if len(batch) < 200:
            break
        offset += 200
    t = time.time()
    with store._lock:
        store.DB.execute("DELETE FROM supplier_products")
        store.DB.executemany(
            "INSERT INTO supplier_products VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            [(p["article"], p.get("name") or "", norm(p.get("name")), p.get("category"),
              p.get("proizvoditel"), p.get("article_pn"), p.get("price"), p.get("final_price"),
              p.get("quantity") or 0, p.get("image"), t) for p in items])
        store.DB.commit()
    store.meta_set("supplier_catalog", {"at": t, "count": len(items)})
    return len(items)


# Ветки каталога, нужные для видеонаблюдения «под ключ»
RELEVANT_TOP = {1536, 1631, 1576, 1586, 1529, 1721, 1558, 1589, 1783}


def sync_categories(progress=lambda s: None):
    """Дерево категорий. API отдаёт только прямых детей и не чаще 1 раза в минуту,
    поэтому обходим лишь узлы с потомками (right-left>1) в нужных ветках."""
    prev = (store.meta_get("supplier_categories") or {}).get("cats") or {}
    cats = {}
    for c in call("categories", {"per_page": 200, "offset": 0}, min_gap=61):
        cats[str(c["ID"])] = c
    queue = [cid for cid in cats if int(cid) in RELEVANT_TOP]
    done = 0
    while queue:
        cid = queue.pop(0)
        progress(f"поставщик: категории, обход {done + 1} (ещё {len(queue)}; 1 запрос в минуту)")
        try:
            kids = call("categories", {"id": cid, "per_page": 200, "offset": 0}, min_gap=61) or []
        except Exception as e:
            print("cat", cid, e)
            kids = []
        for k in kids:
            k["parent"] = cid
            cats[str(k["ID"])] = k
            if int(k["right"]) - int(k["left"]) > 1:
                queue.append(str(k["ID"]))
        done += 1
        store.meta_set("supplier_categories", {"at": (store.meta_get("supplier_categories") or {}).get("at", 0),
                                               "cats": {**prev, **cats}, "partial": True})
    store.meta_set("supplier_categories", {"at": time.time(), "cats": cats})
    return len(cats)


def descendants(cats, cid):
    """Все категории внутри cid (nested sets: left/right)."""
    c = cats.get(str(cid))
    if not c:
        return {int(cid)}
    l, r = int(c["left"]), int(c["right"])
    return {int(k) for k, v in cats.items() if l <= int(v["left"]) and int(v["right"]) <= r}


def sync_prices(progress=lambda s: None):
    """Остатки и цены (чаще, чем каталог)."""
    offset, n = 0, 0
    t = time.time()
    while True:
        d = call("quantity-price", {"per_page": 200, "offset": offset})
        prods = d.get("products") or {}
        with store._lock:
            store.DB.executemany(
                "UPDATE supplier_products SET quantity=?, price=?, updated=? WHERE article=?",
                [(v.get("quantity") or 0, v.get("price"), t, int(k)) for k, v in prods.items()])
            store.DB.commit()
        n += len(prods)
        progress(f"поставщик: остатки {n}")
        if len(prods) < 200:
            break
        offset += 200
    store.meta_set("supplier_prices", {"at": t, "count": n})


def live_quantity(articles):
    if not articles:
        return {}
    return call("quantity", {"article": ",".join(map(str, articles))})


# ---------- сопоставление ----------

def norm(s):
    return re.sub(r"[^A-Z0-9]", "", (s or "").upper().replace("О", "O").replace("С", "C").replace("Е", "E")
                  .replace("А", "A").replace("Р", "P").replace("Х", "X").replace("К", "K").replace("М", "M")
                  .replace("Т", "T").replace("Н", "H").replace("В", "B"))


MODEL_RE = re.compile(r"\b[A-Za-zА-Яа-я][A-Za-z0-9]{0,10}[-‐–][A-Za-z0-9]{1,8}(?:[-‐–/][A-Za-z0-9()]{1,12}){0,6}\b")
NOT_MODEL = re.compile(r"^(IEEE|RJ|H\.?26|DC|AC|PoE|IP\d|WDR|USB|HDMI|VGA|SATA|ONVIF|ГОСТ|СТ|ISO|ЕНС|Wi|Full|CMOS|MP|Smart|PTZ)", re.I)


def extract_models(text):
    """Модели из текста: [(норма, [варианты от полного к укороченному])]."""
    out, seen = [], set()
    for m in MODEL_RE.findall(text or ""):
        if NOT_MODEL.match(m) or not re.search(r"\d", m) or not re.search(r"[A-Za-z]", m):
            continue
        n = norm(m)
        if len(n) < 6 or n in seen:
            continue
        seen.add(n)
        parts = re.split(r"[-‐–/]", m)
        variants = [n]
        for k in range(len(parts) - 1, 1, -1):  # отрезаем суффиксы, оставляя минимум 2 сегмента
            v = norm("".join(parts[:k]))
            if len(v) >= 6 and v not in variants:
                variants.append(v)
        out.append((n, variants))
    return out


_cats = {"t": 0, "v": {}}


def categories():
    """Дерево категорий с кэшем на минуту (row_out зовётся тысячи раз)."""
    if time.time() - _cats["t"] > 60:
        _cats["v"] = (store.meta_get("supplier_categories") or {}).get("cats") or {}
        _cats["t"] = time.time()
    return _cats["v"]


def row_out(r, why, score):
    cats = categories()
    cname = (cats.get(str(r["category"])) or {}).get("NAME", "")
    return {"cost_src": cost_src(dict(r)), "category_name": cname,
            "is_camera": bool(re.search(r"камер", cname, re.I) or CAMERA_RE.search(r["name"] or "")),"article": r["article"], "name": r["name"], "brand": r["brand"], "category": r["category"],
            "cost": cost_of(dict(r)), "price": r["price"], "final_price": r["final_price"],
            "quantity": r["quantity"], "image": r["image"], "why": why, "score": score}


# Ключевые слова → признак категории поставщика (по названию категории)
TYPE_HINTS = [
    (r"купол", "купол"), (r"цилиндр|буллет|bullet", "цилиндр"), (r"поворотн|ptz|скоростн", "поворот"),
    (r"регистратор|nvr|xvr|dvr", "регистратор"), (r"коммутатор|switch", "коммутатор"),
    (r"жестк|hdd|диск", "диск"), (r"кабел|витая", "кабел"),
]


def match_lot(spec_text, lot_name, limit=12):
    """Кандидаты из каталога: сначала точное совпадение модели, затем по типу из категорий."""
    text = f"{lot_name or ''}\n{spec_text or ''}"
    cands, seen = [], set()
    found = extract_models(text)
    models = [m for m, _ in found]
    for m, variants in found[:30]:
        for depth, v in enumerate(variants):
            rows = store.q("SELECT * FROM supplier_products WHERE norm LIKE ? "
                           "ORDER BY quantity>0 DESC, LENGTH(norm) LIMIT 6", (f"%{v}%",))
            hit = False
            for r in rows:
                if r["article"] in seen:
                    continue
                seen.add(r["article"])
                hit = True
                exact = r["norm"] == m or r["norm"].endswith(m)
                cands.append(row_out(r, f"модель {m}" + ("" if depth == 0 else " (похожая)"),
                                     100 if exact else 90 - depth * 10))
            if hit:
                break
    cands.sort(key=lambda c: (-c["score"], -(c["quantity"] > 0), c["cost"] or 1e18))
    if len(cands) < limit:
        cats = (store.meta_get("supplier_categories") or {}).get("cats") or {}
        low = text.lower()
        want = [tag for rx, tag in TYPE_HINTS if re.search(rx, low)]
        if not want and re.search(r"камер", low):
            want = ["камер"]
        if not want and re.search(r"регистратор", low):
            want = ["регистратор"]
        ids = set()
        for k, c in cats.items():
            if any(w in (c.get("NAME") or "").lower() for w in want):
                ids |= descendants(cats, k)
        ids = list(ids)
        if ids:
            ip = "ip" in low or "сетев" in low
            rows = store.q(f"SELECT * FROM supplier_products WHERE category IN ({','.join('?'*len(ids))}) "
                           "AND quantity>0 ORDER BY COALESCE(NULLIF(final_price,0),price) LIMIT 300", ids)
            scored = []
            for r in rows:
                if r["article"] in seen or not cost_of(dict(r)):
                    continue
                cname = (cats.get(str(r["category"])) or cats.get(r["category"]) or {}).get("NAME", "")
                s = 50
                if ip and "ip" in cname.lower():
                    s += 10
                scored.append(row_out(r, f"категория «{cname}»", s))
            scored.sort(key=lambda x: (-x["score"], x["cost"] or 1e18))
            cands += scored[: limit - len(cands)]
    return {"models": models[:20], "candidates": cands[:limit]}


# ---------- поиск по словам (кнопка «Найти») ----------

STOP = {"для", "и", "с", "в", "на", "под", "из", "по", "от", "до", "шт", "мм", "м", "не", "менее", "более", "или", "без", "со"}


def _plain(s):
    s = (s or "").lower().replace("ё", "е").replace("ъ", "").replace("ь", "")  # «разьем» = «разъем»
    return re.sub(r"(\d)\s*[xх×*]\s*(\d)", r"\1x\2", s)  # 10 х 15 → 10x15


def _tokens(q):
    out = []
    for w in re.findall(r"\d+(?:[.,]\d+)?x\d+(?:[.,]\d+)?|[a-zа-я0-9][a-zа-я0-9.,/-]*", _plain(q)):
        w = w.strip(".,/-")
        if len(w) < 2 or w in STOP:
            continue
        if re.fullmatch(r"\d+(?:[.,]\d+)?x\d+(?:[.,]\d+)?", w):
            a, b = w.split("x")
            out.append(("size", (f"{a}x{b}", f"{b}x{a}")))
        elif re.fullmatch(r"[а-я]+", w) and len(w) > 5:
            out.append(("word", w[:max(5, len(w) - 3)]))  # без окончания: «накладная» ≈ «накладн»
        else:
            out.append(("word", w))
    return out


def search(q, limit=30):
    """Сначала точная модель (как раньше), потом товары, где совпало больше слов запроса; в наличии и дешевле — выше."""
    n = norm(q)
    rows = store.q("SELECT * FROM supplier_products WHERE norm LIKE ? OR UPPER(brand) LIKE ? "
                   "ORDER BY quantity>0 DESC, COALESCE(NULLIF(final_price,0),price) LIMIT ?",
                   (f"%{n}%", f"%{(q or '').upper()}%", limit)) if len(n) >= 4 else []
    seen = {r["article"] for r in rows}
    out = [row_out(r, "поиск", 100) for r in rows]
    toks = _tokens(q)
    if not toks:
        return out
    need = max(1, -(-len(toks) // 2))  # не меньше половины слов
    hits = []
    df = [0] * len(toks)
    for r in store.q("SELECT * FROM supplier_products"):
        if r["article"] in seen:
            continue
        name = _plain(r["name"]) + " " + _plain(r["brand"])
        m = [i for i, (kind, v) in enumerate(toks) if (any(s in name for s in v) if kind == "size" else v in name)]
        for i in m:
            df[i] += 1
        if len(m) >= need:
            hits.append((m, r))
    # редкое слово (BNC, размер, модель) важнее частого («кабель», «разъём»)
    total = max(sum(df), 1)
    w = [math.log(1 + total / (d or 1)) for d in df]
    scored = sorted(((sum(w[i] for i in m), len(m), r) for m, r in hits),
                    key=lambda x: (-x[0], x[2]["quantity"] <= 0, cost_of(dict(x[2])) or 1e18))
    out += [row_out(r, f"совпало слов: {n} из {len(toks)}", round(s, 1)) for s, n, r in scored[:limit - len(out)]]
    return out
