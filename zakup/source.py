"""Доступ к открытому API zakup.gov.kz (без авторизации)."""
import json
import re
import subprocess
import tempfile
import time
import io
import os
import zipfile
import xml.etree.ElementTree as ET
from html.parser import HTMLParser
import urllib.parse
import urllib.request

BASE = "https://zakup.gov.kz/api/core/api"
UA = "zakup-helper/1.0"

# Поиск на сайте не учитывает падежи — перебираем формы слов.
KEYWORDS = [
    "видеонаблюдения", "видеонаблюдение", "видеонаблюдением", "видеонаблюдению",
    "видеокамера", "видеокамеры", "видеокамер", "видеорегистратор",
    "видеорегистратора", "видеорегистраторы", "бейнебақылау", "бейнекамера",
    "CCTV", "видеоаналитика", "видеосигнала",
]

# Чтобы не тащить мусор, вроде «интернет для нагрудных видеорегистраторов».
NOISE = re.compile(r"мобильного интернета|услуги связи|интернет|нагрудн", re.I)


def get(path, params=None, timeout=60, retries=3):
    url = path if path.startswith("http") else BASE + path
    if params:
        url += "?" + urllib.parse.urlencode(params, doseq=True)
    last = None
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.load(r)
        except Exception as e:  # сеть/5xx — пробуем ещё раз
            last = e
            time.sleep(1.5 * (i + 1))
    raise last


def search_open_lots(now_iso):
    """Все лоты, где приём заявок ещё не закончился, по всем ключевым словам."""
    seen = {}
    for kw in KEYWORDS:
        offset = 0
        while True:
            d = get("/public/lots/", {"q": kw, "limit": 100, "offset": offset,
                                      "offer_end_date__gte": now_iso})
            for x in d["results"]:
                seen.setdefault(x["id"], x)
            if not d.get("next"):
                break
            offset += 100
    return list(seen.values())


# Запад Казахстана по коду КАТО места поставки: Актюбинская, Атырауская, ЗКО, Мангистауская
WEST_KATO = ("15", "23", "27", "47")


def west(lot):
    """True — запад, False — другой регион, None — адреса нет (Самрук-Казына и др.)."""
    codes = [str(a.get("code") or "") for a in lot.get("delivery_addresses") or []]
    if not codes:
        return None
    return any(c.startswith(WEST_KATO) for c in codes)


def classify(lot):
    name = " ".join(filter(None, [
        lot.get("name_ru"), lot.get("announcement_name_ru"),
        *[(e.get("short_description_ru") or "") for e in lot.get("enstrus") or []],
    ])).lower()
    if NOISE.search(name):
        return "шум"
    if "обслуживан" in name:
        return "ТО"
    if "ремонт" in name or "модерниз" in name:
        return "ремонт"
    if "монтаж" in name or "устройств" in name or "строитель" in name:
        return "монтаж"
    if 1 in (lot.get("purchase_subject_id") or []):
        return "поставка"
    return "прочее"


# ---------- техспецификация ----------

def techspec_files(announcement_id):
    out = []
    for g in get(f"/public/announcements/{announcement_id}/general-files/") or []:
        gname = (g.get("group") or {}).get("name") or ""
        for f in g.get("files") or []:
            fi = f.get("file") or {}
            if fi.get("file_url"):
                out.append({"group": gname, "url": fi["file_url"], "name": fi.get("filename") or ""})
    return out


def _download(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=90) as r:
        return r.read()


def _kind(data):
    head = data[:8]
    if head.startswith(b"%PDF"):
        return ".pdf"
    if head.startswith(b"PK"):
        try:
            names = zipfile.ZipFile(io.BytesIO(data)).namelist()
        except zipfile.BadZipFile:
            return None
        return ".xlsx" if any(n.startswith("xl/") for n in names) else ".docx"
    if head.startswith(b"\xd0\xcf\x11\xe0"):
        return ".doc"
    if head.startswith(b"{\\rtf"):
        return ".rtf"
    return None


def _run(data, suffix, cmd):
    with tempfile.NamedTemporaryFile(suffix=suffix) as tmp:
        tmp.write(data)
        tmp.flush()
        return subprocess.run(cmd + [tmp.name] if cmd[-1] != "-" else cmd[:-1] + [tmp.name, "-"],
                              capture_output=True, text=True, timeout=90).stdout


class _Tables(HTMLParser):
    """Строки таблиц из HTML (textutil): [[ячейка, ячейка, …], …]."""
    def __init__(self):
        super().__init__()
        self.rows, self.row, self.cell, self.depth = [], None, None, 0

    def handle_starttag(self, tag, attrs):
        if tag == "tr":
            self.row = []
        elif tag in ("td", "th") and self.row is not None:
            self.cell = []

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self.cell is not None and self.row is not None:
            self.row.append(re.sub(r"\s+", " ", "".join(self.cell)).strip())
            self.cell = None
        elif tag == "tr" and self.row is not None:
            if any(self.row):
                self.rows.append(self.row)
            self.row = None

    def handle_data(self, d):
        if self.cell is not None:
            self.cell.append(d)


def pdf_rows(text):
    """Строки таблиц из pdftotext -layout: колонки разделены 2+ пробелами."""
    rows = []
    for ln in text.splitlines():
        cells = [c.strip() for c in re.split(r"\s{2,}", ln.strip()) if c.strip()]
        if len(cells) >= 3:
            rows.append(cells)
    return rows


def xlsx_rows(data):
    """Строки всех листов Excel (stdlib: zip + xml)."""
    z = zipfile.ZipFile(io.BytesIO(data))
    ns = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
    shared = []
    if "xl/sharedStrings.xml" in z.namelist():
        for si in ET.fromstring(z.read("xl/sharedStrings.xml")).iter(ns + "si"):
            shared.append("".join(t.text or "" for t in si.iter(ns + "t")))
    rows = []
    for name in sorted(n for n in z.namelist() if n.startswith("xl/worksheets/sheet")):
        for r in ET.fromstring(z.read(name)).iter(ns + "row"):
            cells = []
            for c in r.iter(ns + "c"):
                v = c.find(ns + "v")
                isv = c.find(ns + "is")
                if c.get("t") == "s" and v is not None:
                    val = shared[int(v.text)]
                elif isv is not None:
                    val = "".join(t.text or "" for t in isv.iter(ns + "t"))
                else:
                    val = v.text if v is not None else ""
                cells.append(re.sub(r"\s+", " ", val or "").strip())
            if any(cells):
                rows.append(cells)
    return rows


def ocr_pdf(data, max_pages=15):
    """Скан без текстового слоя → tesseract (rus+kaz). Медленно: ~5 с на страницу."""
    with tempfile.TemporaryDirectory() as d:
        pdf = os.path.join(d, "in.pdf")
        open(pdf, "wb").write(data)
        subprocess.run(["pdftoppm", "-r", "200", "-gray", "-l", str(max_pages), "-png", pdf, os.path.join(d, "p")],
                       capture_output=True, timeout=600)
        out = []
        for img in sorted(f for f in os.listdir(d) if f.endswith(".png")):
            r = subprocess.run(["tesseract", os.path.join(d, img), "-", "-l", "rus+kaz", "--psm", "6"],
                               capture_output=True, text=True, timeout=180)
            out.append(r.stdout)
        return "\n".join(out)


def file_content(url):
    """(текст, строки таблиц) файла по ссылке."""
    return content_of(_download(url))


def content_of(data):
    """(текст, строки таблиц). PDF → pdftotext; DOC/DOCX/RTF → textutil (txt и html для таблиц)."""
    k = _kind(data)
    if k == ".pdf":
        text = _run(data, k, ["pdftotext", "-layout", "-"])
        if len(text.strip()) < 200:  # скан без текста
            text = "(распознано со скана, возможны ошибки)\n" + ocr_pdf(data)
        return text, pdf_rows(text)
    if k == ".xlsx":
        rows = xlsx_rows(data)
        return "\n".join(" | ".join(r) for r in rows), rows
    if k in (".docx", ".doc", ".rtf"):
        text = _run(data, k, ["textutil", "-convert", "txt", "-stdout"])
        p = _Tables()
        p.feed(_run(data, k, ["textutil", "-convert", "html", "-stdout"]))
        return text, p.rows
    return "", []


def file_text(url):
    return file_content(url)[0]


pdf_text = file_text


def _field(text, label):
    m = re.search(label + r"\s*:?\s+(.+)", text)
    return m.group(1).strip() if m else None


def parse_techspec(text):
    """Берём русскую часть (после казахской) и вытаскиваем ключевые поля."""
    i = text.find("Техническая спецификация")
    ru = text[i:] if i >= 0 else text
    qty = _field(ru, r"Количество")
    unit = _field(ru, r"Единица измерения")
    term = _field(ru, r"Срок поставки") or _field(ru, r"Срок выполнения")
    m = re.search(r"Описание и требуемые(.+)", ru, re.S)
    desc = ""
    if m:
        # Правая колонка таблицы: отрезаем левую подпись (до ~30 символов)
        lines = []
        for ln in m.group(1).splitlines():
            ln = ln.rstrip()
            if not ln.strip():
                continue
            parts = re.split(r"\s{3,}", ln.strip(), maxsplit=1)
            lines.append(parts[-1] if len(parts) > 1 or len(ln) - len(ln.lstrip()) < 5 else ln.strip())
        desc = " ".join(lines)
    qn = None
    if qty:
        mm = re.search(r"[\d\s]+[.,]?\d*", qty)
        if mm:
            try:
                qn = float(mm.group(0).replace(" ", "").replace(",", "."))
            except ValueError:
                qn = None
    return {"qty": qn, "qty_raw": qty, "unit": unit, "term": term,
            "desc": desc[:6000], "raw": ru[:20000]}


def lot_techspec(lot):
    files = techspec_files(lot["announcement_id"])
    specs = [f for f in files if "спецификац" in f["group"].lower() or f["name"].startswith("techspec")]
    ext = str(lot.get("external_id") or "")
    auto = [f for f in specs if f["name"].startswith("techspec")]
    mine = [f for f in auto if ext and ext in f["name"]] or auto[:1]
    result = {"files": files, "spec": None}
    if mine:  # стандартная спецификация портала (товары)
        try:
            result["spec"] = parse_techspec(file_text(mine[0]["url"])) | {"url": mine[0]["url"]}
        except Exception as e:
            result["spec"] = {"error": str(e), "url": mine[0]["url"]}
    else:  # работы/услуги: свои ТЗ и сметы в Word/PDF — склеиваем текст
        parts, tables = [], []
        for f in specs[:5]:
            try:
                t, rows = file_content(f["url"])
                t = t.strip()
            except Exception as e:
                t, rows = f"(не удалось прочитать: {e})", []
            if t:
                parts.append(f"══ {f['name']} ══\n{t}")
            tables += [[f["name"]] + r for r in rows[:400]]  # первая ячейка — имя файла
        if parts:
            result["spec"] = spec_from_parts(parts, tables)
    return result


def spec_from_parts(parts, tables):
    """ТЗ работ/услуг из нескольких файлов: склеенный текст + строки таблиц (первая ячейка — имя файла)."""
    text = "\n\n".join(parts)
    return {"qty": None, "unit": None, "term": None, "tables": tables[:1500],
            "desc": text[:15000], "raw": text[:40000], "free_form": True}


def spec_from_uploads(uploads):
    """ТЗ из файлов, которые владелец загрузил сам (лоты Самрук-Қазыны, сканы)."""
    parts = [f"══ {u['name']} ══\n{u['text']}" for u in uploads if u["text"].strip()]
    tables = [[u["name"]] + r for u in uploads for r in u["rows"][:400]]
    return spec_from_parts(parts, tables) if parts else None


# ---------- история цен по коду ЕНС ТРУ ----------

def price_history(enstru_code, since_iso, max_details=60):
    """Договоры по коду за период: плановая сумма vs цена договора → реальная скидка.
    Общая медиана — по конкурентным способам; отдельно медиана по каждому способу закупки."""
    d = get("/public/contracts/", {"enstru_code": enstru_code, "limit": 100,
                                   "sign_date__gte": since_iso, "ordering": "-sign_date"})
    rows = d.get("results", [])
    deals = []
    for r in rows[:max_details]:
        try:
            c = get(f"/public/contracts/{r['id']}/")
        except Exception:
            continue
        plan, price = c.get("plan_sum_with_vat") or 0, c.get("contract_price_with_vat") or 0
        if plan > 0 and price > 0:
            deals.append({
                "sign_date": r.get("sign_date"), "plan": plan, "price": price,
                "discount": round(1 - price / plan, 4),
                "supplier": r.get("supplier_organization_name"),
                "customer": r.get("customer_organization_name"),
                "method": r.get("purchase_method_name"),
            })
    suppliers = {}
    for r in rows:
        s = r.get("supplier_organization_name") or "?"
        suppliers[s] = suppliers.get(s, 0) + 1

    def med(xs):
        xs = sorted(xs)
        return xs[len(xs) // 2] if xs else None
    competitive = [x for x in deals if "одного источника" not in (x["method"] or "")]
    disc = sorted(x["discount"] for x in competitive)
    by_method = {}
    for x in deals:
        by_method.setdefault(x["method"] or "?", []).append(x["discount"])
    return {
        "enstru_code": enstru_code, "contracts_total": d.get("count", 0),
        "competitive_checked": len(competitive), "median_discount": med(disc),
        "p25_discount": disc[len(disc) // 4] if disc else None,
        "p75_discount": disc[(len(disc) * 3) // 4] if disc else None,
        "by_method": {m: {"median": med(v), "n": len(v)} for m, v in by_method.items()},
        "top_suppliers": sorted(suppliers.items(), key=lambda kv: -kv[1])[:8],
        "deals": deals,
    }
