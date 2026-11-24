"""Помощник по госзакупкам: видеонаблюдение.  Запуск:  python3 server.py  → http://localhost:8765"""
import datetime as dt
import json
import re
import sys
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path

from zakup import autopick, bom, source, store, supplier

PORT = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else 8765
ROOT = Path(__file__).resolve().parent
REFRESH_EVERY = 30 * 60          # раз в полчаса перечитываем открытые лоты
HISTORY_TTL = 7 * 24 * 3600      # историю цен обновляем раз в неделю
HISTORY_DAYS = 180

state = {"running": False, "stage": "", "error": None}


def utcnow():
    return dt.datetime.now(dt.timezone.utc)


def refresh():
    if state["running"]:
        return
    state.update(running=True, error=None, stage="ищу лоты")
    try:
        now = utcnow().strftime("%Y-%m-%dT%H:%M:%S")
        lots = source.search_open_lots(now)
        t = time.time()
        for lot in lots:
            old = store.q("SELECT first_seen FROM lots WHERE id=?", (lot["id"],))
            store.x("INSERT OR REPLACE INTO lots VALUES(?,?,?,?,?)",
                    (lot["id"], json.dumps(lot, ensure_ascii=False), source.classify(lot),
                     old[0]["first_seen"] if old else t, t))
        store.meta_set("last_refresh", {"at": t, "found": len(lots)})

        # пока работаем только по западу; лоты без адреса тоже берём (см. docs/ECONOMICS.md)
        relevant = [l for l in lots if source.classify(l) != "шум" and source.west(l) is not False]
        state["stage"] = f"техспецификации (0/{len(relevant)})"
        todo = [l for l in relevant if store.get_json("techspec", "lot_id", l["id"])[0] is None]
        done = 0

        def ts(l):
            nonlocal done
            try:
                store.put_json("techspec", "lot_id", l["id"], source.lot_techspec(l))
            except Exception as e:
                print("techspec", l["id"], e)
            done += 1
            state["stage"] = f"техспецификации ({done}/{len(todo)})"

        with ThreadPoolExecutor(4) as ex:
            list(ex.map(ts, todo))

        codes = sorted({e["code"] for l in relevant for e in l.get("enstrus") or []})
        since = (utcnow() - dt.timedelta(days=HISTORY_DAYS)).strftime("%Y-%m-%d")
        for i, code in enumerate(codes, 1):
            _, fetched = store.get_json("history", "enstru_code", code)
            if fetched and time.time() - fetched < HISTORY_TTL:
                continue
            state["stage"] = f"история цен ({i}/{len(codes)}) {code}"
            try:
                store.put_json("history", "enstru_code", code, source.price_history(code, since))
            except Exception as e:
                print("history", code, e)
        state["stage"] = "автоподбор"
        run_autopick()
        state["stage"] = "готово"
    except Exception as e:
        traceback.print_exc()
        state["error"] = str(e)
    finally:
        state["running"] = False


sup_state = {"stage": "", "error": None}


def supplier_loop():
    """Каталог — раз в сутки, остатки/цены — раз в час, категории — раз в неделю."""
    def age(k):
        m = store.meta_get(k)
        return time.time() - m["at"] if m else 1e12
    prog = lambda s: sup_state.update(stage=s)
    while True:
        try:
            if age("supplier_catalog") > 24 * 3600:
                supplier.sync_catalog(prog)
            elif age("supplier_prices") > 3600:
                supplier.sync_prices(prog)
            if age("supplier_categories") > 7 * 24 * 3600:
                supplier.sync_categories(prog)
            sup_state.update(stage="автоподбор", error=None)
            if not state["running"]:
                run_autopick()
            sup_state.update(stage="")
        except Exception as e:
            traceback.print_exc()
            sup_state.update(error=str(e))
        time.sleep(300)


def run_autopick():
    """Черновой расчёт по всем открытым лотам (правила — docs/AUTOPICK.md)."""
    now = utcnow().isoformat()[:19]
    n = 0
    for row in store.q("SELECT * FROM lots"):
        s = lot_summary(row)
        if (s["end"] or "") < now or s["west"] is False:
            continue
        spec = (store.get_json("techspec", "lot_id", s["id"])[0] or {}).get("spec") or {}
        try:
            res = autopick.autopick(s, spec)
        except Exception as e:
            traceback.print_exc()
            res = {"ok": False, "reason": f"ошибка автоподбора: {e}"}
        store.put_json("auto", "lot_id", s["id"], res)
        n += 1
    store.meta_set("autopick", {"at": time.time(), "lots": n})


def autopick_one(lid):
    row = store.q("SELECT * FROM lots WHERE id=?", (lid,))[0]
    s = lot_summary(row)
    spec = (store.get_json("techspec", "lot_id", lid)[0] or {}).get("spec") or {}
    try:
        res = autopick.autopick(s, spec)
    except Exception as e:
        traceback.print_exc()
        res = {"ok": False, "reason": f"ошибка автоподбора: {e}"}
    store.put_json("auto", "lot_id", lid, res)


def upload_techspec(lid, name, data):
    """Файл ТЗ от владельца: читаем как обычное ТЗ, добавляем к загруженным и пересчитываем лот."""
    if not store.q("SELECT id FROM lots WHERE id=?", (lid,)):
        raise ValueError("нет такого лота")
    if len(data) > 40 * 1024 * 1024:
        raise ValueError("файл больше 40 МБ")
    text, rows = source.content_of(data)
    if not text.strip():
        raise ValueError("не смог прочитать файл: нужен PDF, Word (doc, docx, rtf) или Excel (xlsx)")
    ts = store.get_json("techspec", "lot_id", lid)[0] or {"files": [], "spec": None}
    ups = [u for u in ts.get("uploads") or [] if u["name"] != name]
    ups.append({"name": name, "text": text[:40000], "rows": rows[:400], "at": time.time()})
    ts["uploads"] = ups
    ts["spec"] = source.spec_from_uploads(ups)
    store.put_json("techspec", "lot_id", lid, ts)
    autopick_one(lid)
    return {"ok": True, "chars": len(text), "rows": len(rows), "files": [u["name"] for u in ups]}


def loop():
    while True:
        refresh()
        time.sleep(REFRESH_EVERY)


def lot_summary(row):
    lot = json.loads(row["data"])
    ts, _ = store.get_json("techspec", "lot_id", lot["id"])
    spec = (ts or {}).get("spec") or {}
    calc, _ = store.get_json("calc", "lot_id", lot["id"])
    auto, _ = store.get_json("auto", "lot_id", lot["id"])
    codes = [e["code"] for e in lot.get("enstrus") or []]
    hist = store.get_json("history", "enstru_code", codes[0])[0] if codes else None
    # данные для типичной скидки; какую брать — решает панель по параметрам (docs/ECONOMICS.md)
    method = lot.get("purchase_method_name")
    by_method = ((hist or {}).get("by_method") or {}).get(method)
    return {
        "id": lot["id"], "category": row["category"], "first_seen": row["first_seen"],
        "name": lot.get("name_ru") or lot.get("name_kk") or "", "announcement": lot.get("announcement_name_ru") or lot.get("announcement_name_kk") or "",
        "short": ", ".join(filter(None, [e.get("short_description_ru") for e in lot.get("enstrus") or []])),
        "enstru": codes, "price": lot.get("total_price"), "qty_api": lot.get("quantity"),
        "qty": spec.get("qty"), "unit": spec.get("unit"),
        "status": lot.get("status_name"), "method": lot.get("purchase_method_name"),
        "system": (lot.get("system") or {}).get("name"),
        "end": lot.get("offer_end_date"), "start": lot.get("offer_start_date"),
        "region": ", ".join(a.get("name_ru") or "" for a in lot.get("delivery_addresses") or []),
        "addresses": [a.get("name_ru") or "" for a in lot.get("delivery_addresses") or []],
        "customer": ", ".join(c.get("name_ru") or c.get("name_kk") or "" for c in lot.get("customers") or []) or lot.get("organization_name"),
        "customer_bin": ", ".join(c.get("bin") or "" for c in lot.get("customers") or []),
        "ann_number": lot.get("announcement_number"), "ann_ext": lot.get("announcement_external_id"),
        "lot_number": lot.get("lot_number"), "dumping_price": lot.get("dumping_price"),
        "disc_code": (hist or {}).get("median_discount"), "disc_method": by_method,
        "west": source.west(lot),
        # нужен ли монтаж: работы/ремонт — да; поставка — если в спеке есть установка
        **install_of(row["category"], (spec.get("desc") or "") + " " + (lot.get("announcement_name_ru") or "")),
        "calc": calc,
        "auto": auto,
        "flags": lot_flags(lot, ts, auto),
    }


# Нужен ли монтаж при поставке: только явные слова о работе, не «кронштейн для монтажа» и не «установка ПО»
INSTALL_RX = re.compile(
    r"(?:с|включая|включ\w*|осуществ\w*|произв\w*|выполн\w*|обеспеч\w*|услуг\w*\s+по)\s+(?:доставк\w*\s+(?:и|,)\s+)?(?:установк|монтаж)\w*"
    r"|(?:установк|монтаж)\w*\s+(?:и|,)\s+(?:настройк|подключени|наладк|пусконаладк|ввод\w*\s+в\s+эксплуатац)"
    r"|(?:установк|монтаж)\w*\s+(?:осуществля|производ|выполня)\w*\s+(?:силами\s+)?поставщик"
    r"|силами\s+поставщика[^.]{0,40}(?:установ|монтаж)|доставк\w*,?\s+(?:и\s+)?(?:установк|монтаж)", re.I)
INSTALL_NOT = re.compile(r"установк\w*\s+(?:по|программ|приложени|драйвер)|(?:для|способ\w*|тип\w*)\s+(?:установк|монтаж)", re.I)


def install_of(category, text):
    """Монтаж: работы/ремонт — всегда; поставка — только если в ТЗ прямо сказано про установку силами поставщика."""
    if category in ("монтаж", "ремонт"):
        return {"install": True, "install_why": "лот на работы"}
    for m in INSTALL_RX.finditer(text or ""):
        frag = text[max(0, m.start() - 30):m.end() + 30]
        if not INSTALL_NOT.search(frag):
            return {"install": True, "install_why": "в ТЗ: «…" + re.sub(r"\s+", " ", frag).strip() + "…»"}
    return {"install": False, "install_why": "только поставка"}


# Лицензия на саму работу (а не «лицензия на ПО»): монтаж/охранная/строительная/проектная деятельность
LICENSE_RX = re.compile(
    r"государственн\w*\s+лицензи|лицензи\w*\s+(?:на|по|для)\s+[^.;]{0,80}?(?:монтаж|охран|строител|строительн|изыскат|проектн|"
    r"деятельност|работ)|лицензи\w*[^.;]{0,40}?(?:I{1,3}|IV|[1-4])\s*категори|(?:I{1,3}|IV|[1-4])\s*категори\w*[^.;]{0,40}?лицензи", re.I)
ALARM_RX = re.compile(r"сигнализац|извещател|приемно-контрольн|приёмно-контрольн|\bппк\b|оповещател|дабыл", re.I)
# стандартное название кода ЕНС ТРУ — есть почти у всех лотов на монтаж/ремонт, сигнализацией не считаем
STD_NAME_RX = re.compile(r"(?:пожарн\w*\s*/\s*)?охранн\w*\s+сигнализаци\w*\s*/\s*систем\w*\s+видеонаблюдени\w*|"
                         r"күзет\w*\s+(?:дабыл|сигнализац)\w*\s*/\s*бейнебақылау\w*\s+жүйе\w*", re.I)
# не наш товар: веб-камеры, ручные видеокамеры, автомобильные регистраторы (docs/ECONOMICS.md, «Группы»)
OFFTOPIC_RX = re.compile(r"для типа «(веб|ручная|авто)»")


def lot_flags(lot, ts, auto):
    spec = (ts or {}).get("spec") or {}
    name = " ".join(filter(None, [lot.get("name_ru"), lot.get("announcement_name_ru"), lot.get("description_ru"),
                                  *[(e.get("short_description_ru") or "") for e in lot.get("enstrus") or []]]))
    text = spec.get("raw") or spec.get("desc") or ""
    reason = (auto or {}).get("reason") or ""
    return {
        "alarm": len(ALARM_RX.findall(STD_NAME_RX.sub(" ", name + " " + text))) >= 2,  # сигнализация в самом заказе
        "license": bool(LICENSE_RX.search(text)),
        "no_docs": ts is not None and not (ts.get("files") or ts.get("uploads")),  # None — ещё не скачивали
        "scan": "распознано со скана" in text[:400],
        "offtopic": bool(OFFTOPIC_RX.search(reason)),
    }


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, code, body, ctype="application/json; charset=utf-8"):
        if not isinstance(body, (bytes, bytearray)):
            body = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        p = self.path.split("?")[0]
        if p in ("/", "/index.html"):
            return self.send(200, (ROOT / "web" / "index.html").read_bytes(), "text/html; charset=utf-8")
        if p == "/api/status":
            return self.send(200, state | {"last": store.meta_get("last_refresh"), "supplier": sup_state | {
                "catalog": store.meta_get("supplier_catalog"), "prices": store.meta_get("supplier_prices")}})
        if p == "/api/lots":
            now = utcnow().isoformat()
            rows = store.q("SELECT * FROM lots")
            out = [lot_summary(r) for r in rows]
            out = [o for o in out if (o["end"] or "") >= now[:19] and o["west"] is not False]
            out.sort(key=lambda o: o["end"] or "")
            return self.send(200, out)
        if p.startswith("/api/match/"):
            lid = int(p.rsplit("/", 1)[1])
            r = store.q("SELECT * FROM lots WHERE id=?", (lid,))
            if not r:
                return self.send(404, {"error": "нет такого лота"})
            s = lot_summary(r[0])
            ts = store.get_json("techspec", "lot_id", lid)[0] or {}
            spec = (ts.get("spec") or {})
            text = " ".join(filter(None, [s["announcement"], s["short"], spec.get("desc") or spec.get("raw")]))
            return self.send(200, supplier.match_lot(text, s["name"]))
        if p.startswith("/api/supplier/search"):
            from urllib.parse import parse_qs, urlparse
            qv = (parse_qs(urlparse(self.path).query).get("q") or [""])[0]
            return self.send(200, supplier.search(qv))
        if p.startswith("/api/supplier/similar"):
            # другие варианты к позиции сметы: та же категория поставщика, сначала в наличии и дешевле
            from urllib.parse import parse_qs, urlparse
            art = int((parse_qs(urlparse(self.path).query).get("article") or ["0"])[0] or 0)
            base = store.q("SELECT * FROM supplier_products WHERE article=?", (art,))
            if not base:
                return self.send(200, [])
            rows = store.q("SELECT * FROM supplier_products WHERE category=? AND article<>? "
                           "ORDER BY quantity>0 DESC, COALESCE(NULLIF(final_price,0),price) LIMIT 400",
                           (base[0]["category"], art))
            out = []
            for r in rows:  # только не хуже исходного по главным характеристикам (docs/ECONOMICS.md, 2.4)
                ok, why = bom.not_worse(base[0], r)
                if ok and supplier.cost_of(dict(r)):
                    out.append(supplier.row_out(r, why, 0))
            return self.send(200, out[:8])
        if p.startswith("/api/lot/"):
            lid = int(p.rsplit("/", 1)[1])
            r = store.q("SELECT * FROM lots WHERE id=?", (lid,))
            if not r:
                return self.send(404, {"error": "нет такого лота"})
            s = lot_summary(r[0])
            s["techspec"] = store.get_json("techspec", "lot_id", lid)[0]
            s["history"] = [store.get_json("history", "enstru_code", c)[0] for c in s["enstru"]]
            return self.send(200, s)
        if p == "/api/settings":
            return self.send(200, store.meta_get("settings", {}))
        if p == "/api/doc":
            return self.send(200, (ROOT / "docs" / "AUTOPICK.md").read_bytes(), "text/markdown; charset=utf-8")
        self.send(404, {"error": "not found"})

    def do_POST(self):
        p = self.path
        if p.startswith("/api/upload-ts/"):  # ТЗ, скачанное владельцем вручную (тело — сам файл)
            from urllib.parse import parse_qs, unquote, urlparse
            lid = int(urlparse(p).path.rsplit("/", 1)[1])
            name = unquote((parse_qs(urlparse(p).query).get("name") or ["ТЗ"])[0])[:120]
            data = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            try:
                return self.send(200, upload_techspec(lid, name, data))
            except ValueError as e:
                return self.send(400, {"error": str(e)})
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        if p == "/api/refresh":
            threading.Thread(target=refresh, daemon=True).start()
            return self.send(200, {"ok": True})
        if p.startswith("/api/calc/"):
            lid = int(p.rsplit("/", 1)[1])
            if body.get("reset"):  # вернуть лот к автоподбору
                store.x("DELETE FROM calc WHERE lot_id=?", (lid,))
            else:
                store.put_json("calc", "lot_id", lid, body)
            return self.send(200, {"ok": True})
        if p == "/api/autopick":
            threading.Thread(target=run_autopick, daemon=True).start()
            return self.send(200, {"ok": True})
        if p == "/api/settings":
            store.meta_set("settings", body)
            return self.send(200, {"ok": True})
        self.send(404, {"error": "not found"})


if __name__ == "__main__":
    threading.Thread(target=loop, daemon=True).start()
    threading.Thread(target=supplier_loop, daemon=True).start()
    print(f"Помощник по госзакупкам: http://localhost:{PORT}")
    ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()
