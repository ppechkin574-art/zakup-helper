"""Сборка сайта для GitHub Pages: обновляет данные и сохраняет всё, что страница берёт у сервера, в файлы site/.

Запускается по расписанию на GitHub (.github/workflows/pages.yml), можно и вручную: python3 build_static.py
Ключ keeper.kz — из переменной KEEPER_TOKEN (на GitHub — секрет репозитория) или из .env.
Что меняется на сайте по сравнению с локальной версией — docs/DEPLOY.md.
"""
import json
import shutil
import sys
import time
from pathlib import Path

import server
from zakup import store, supplier

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "site"


def log(s):
    print(s, flush=True)


def age(key):
    m = store.meta_get(key)
    return time.time() - m["at"] if m else 1e12


def update():
    """Каталог поставщика — раз в сутки, остатки и цены — каждый запуск, лоты и ТЗ — каждый запуск."""
    try:
        if age("supplier_categories") > 7 * 24 * 3600:
            supplier.sync_categories(log)
        if age("supplier_catalog") > 23 * 3600:
            supplier.sync_catalog(log)
        else:
            supplier.sync_prices(log)
    except Exception as e:  # без поставщика лоты всё равно обновим — со старыми ценами
        log(f"поставщик: ошибка {e}")
    server.refresh()
    if server.state["error"]:
        log(f"обновление лотов: ошибка {server.state['error']}")


def get(path):
    code, body, *_ = server.api_get(path)
    if code != 200:
        raise RuntimeError(f"{path}: {code} {body}")
    return body


def dump(rel, obj):
    f = OUT / rel
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(obj, ensure_ascii=False, separators=(",", ":")))


def export():
    shutil.rmtree(OUT, ignore_errors=True)
    OUT.mkdir()
    html = (ROOT / "web" / "index.html").read_text()
    html = html.replace("<head>", "<head>\n<script>window.STATIC=true</script>", 1)  # страница читает файлы, а не сервер
    (OUT / "index.html").write_text(html)
    (OUT / ".nojekyll").write_text("")
    (OUT / "data").mkdir()
    (OUT / "data" / "doc.md").write_bytes((ROOT / "docs" / "AUTOPICK.md").read_bytes())

    lots = get("/api/lots")
    dump("data/lots.json", lots)
    status = get("/api/status") | {"built_at": time.time()}
    dump("data/status.json", status)

    articles = set()
    for l in lots:
        dump(f"data/lot/{l['id']}.json", get(f"/api/lot/{l['id']}"))
        m = get(f"/api/match/{l['id']}")
        dump(f"data/match/{l['id']}.json", m)
        articles |= {c["article"] for c in m.get("candidates") or []}
        articles |= {i["article"] for i in ((l.get("auto") or {}).get("items") or []) if i.get("article")}
    for a in sorted(articles):  # «Другие варианты» — заранее для всех товаров из смет и подбора
        dump(f"data/similar/{a}.json", get(f"/api/supplier/similar?article={a}"))

    # каталог для поиска «Найти» в браузере: [артикул, название, бренд, категория, цена, источник цены, остаток, камера]
    src_code = {"платина": 0, "золото (платина по запросу)": 1}
    cat = []
    for r in store.q("SELECT * FROM supplier_products"):
        d = dict(r)
        cost = supplier.cost_of(d)
        cat.append([r["article"], r["name"] or "", r["brand"] or "", r["category"], cost,
                    src_code.get(supplier.cost_src(d), 2), r["quantity"] or 0,
                    1 if supplier.CAMERA_RE.search(r["name"] or "") else 0])
    dump("data/catalog.json", cat)
    size = sum(f.stat().st_size for f in OUT.rglob("*") if f.is_file())
    log(f"сайт собран: {len(lots)} лотов, {len(articles)} списков замен, каталог {len(cat)}, {size / 1e6:.1f} МБ")


if __name__ == "__main__":
    if "--no-update" not in sys.argv:
        update()
    export()
