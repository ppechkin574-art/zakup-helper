"""Выкладка сайта на GitHub Pages с этого Mac (docs/DEPLOY.md).

Данные собирает Mac: госпортал zakup.gov.kz не отвечает серверам GitHub (они в США).
Сайт собирается из локальной базы (build_static.export) и отправляется в ветку gh-pages одним
сохранением — старые версии затираются, чтобы репозиторий не рос на 5 МБ каждый час.

Запускается сервером после каждого обновления лотов; можно и вручную: python3 publish.py
"""
import shutil
import subprocess
import time
from pathlib import Path

import build_static

ROOT = Path(__file__).resolve().parent
WORK = ROOT / "data" / "pages"   # data/ в .gitignore: рабочая папка ветки gh-pages
BRANCH = "gh-pages"


def git(*args, cwd=WORK):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def publish():
    lots = build_static.export()
    if not lots:
        raise RuntimeError("0 лотов — сайт не обновляю, чтобы не выложить пустой")
    remote = git("remote", "get-url", "origin", cwd=ROOT)
    shutil.rmtree(WORK, ignore_errors=True)
    shutil.copytree(build_static.OUT, WORK)
    git("init", "-q", "-b", BRANCH)
    # автор — как в основном репозитории (служебный адрес GitHub, почта скрыта)
    for k in ("user.name", "user.email"):
        git("config", k, git("config", k, cwd=ROOT))
    git("add", "-A")
    git("commit", "-q", "-m", f"Данные сайта {time.strftime('%d.%m.%Y %H:%M')}: {len(lots)} лотов")
    git("push", "-q", "-f", remote, f"HEAD:{BRANCH}")
    return len(lots)


if __name__ == "__main__":
    print(f"сайт выложен: {publish()} лотов")
