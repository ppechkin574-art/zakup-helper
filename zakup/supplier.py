"""Поставщик keeper.kz: каталог, цены, остатки + сопоставление с техспекой лота.

Лимиты API: 1 запрос / 3 с на токен (categories — 1 / 60 с), до 200 позиций за запрос.
Наша закупочная цена = final_price (если > 0), иначе price (розница).
"""
import json
import os
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


def sync_categories(progress=lambda s: None):
    """Дерево категорий: верхний уровень + подкатегории (лимит 60 с — делаем редко)."""
    cats = {}
    top = call("categories", {"per_page": 200, "offset": 0}, min_gap=61)
    for c in top:
        cats[int(c["ID"])] = c
    store.meta_set("supplier_categories", {"at": time.time(), "cats": cats})
    used = {r["category"] for r in store.q("SELECT DISTINCT category FROM supplier_products")}
    missing = [c for c in used if c and c not in cats]
    for i, c in enumerate(missing, 1):
        progress(f"поставщик: категории {i}/{len(missing)} (1 в минуту)")
        try:
            for sub in call("categories", {"id": c, "per_page": 200, "offset": 0}, min_gap=61) or []:
                cats[int(sub["ID"])] = sub
        except Exception as e:
            print("cat", c, e)
        store.meta_set("supplier_categories", {"at": time.time(), "cats": cats})
    return len(cats)


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


MODEL_RE = re.compile(r"\b[A-Za-zА-Яа-я]{1,6}[-‐–][A-Za-z0-9]{1,8}(?:[-‐–/][A-Za-z0-9()]{1,12}){0,6}\b")
NOT_MODEL = re.compile(r"^(IEEE|RJ|H\.?26|DC|AC|PoE|IP\d|WDR|USB|HDMI|VGA|SATA|ONVIF|ГОСТ|СТ|ISO|ЕНС|Wi|Full|CMOS|MP|Smart|PTZ)", re.I)


def extract_models(text):
    out = []
    for m in MODEL_RE.findall(text or ""):
        if NOT_MODEL.match(m) or not re.search(r"\d", m) or not re.search(r"[A-Za-z]", m):
            continue
        n = norm(m)
        if len(n) >= 6 and n not in out:
            out.append(n)
    return out


def row_out(r, why, score):
    return {"article": r["article"], "name": r["name"], "brand": r["brand"], "category": r["category"],
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
    models = extract_models(text)
    for m in models[:30]:
        if len(m) < 6:
            continue
        rows = store.q("SELECT * FROM supplier_products WHERE norm=? OR norm LIKE ? LIMIT 5", (m, m + "%"))
        for r in rows:
            if r["article"] not in seen:
                seen.add(r["article"])
                cands.append(row_out(r, f"модель {m}", 100 if r["norm"] == m else 90))
    if len(cands) < limit:
        cats = (store.meta_get("supplier_categories") or {}).get("cats") or {}
        low = text.lower()
        want = [tag for rx, tag in TYPE_HINTS if re.search(rx, low)]
        if not want and re.search(r"камер", low):
            want = ["камер"]
        if not want and re.search(r"регистратор", low):
            want = ["регистратор"]
        ids = [int(k) for k, c in cats.items() if any(w in (c.get("NAME") or "").lower() for w in want)]
        if ids:
            ip = "ip" in low or "сетев" in low
            mp = re.search(r"(\d{1,2})\s*(?:mp|мп|мегапиксел)", low)
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
                if mp and re.search(rf"(?<!\d){mp.group(1)}\d?(?:MP|M\b)|{mp.group(1)}(?:2|4)?1?\d?", r["name"]):
                    s += 0  # в названии модели мегапиксели не всегда читаются — не штрафуем
                scored.append(row_out(r, f"категория «{cname}»", s))
            scored.sort(key=lambda x: (-x["score"], x["cost"] or 1e18))
            cands += scored[: limit - len(cands)]
    return {"models": models[:20], "candidates": cands[:limit]}
