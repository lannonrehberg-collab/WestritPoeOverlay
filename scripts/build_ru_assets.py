"""Сборка русских ресурсов: словарь RU->EN и шаблон заголовка панели.

Запуск:  py scripts/build_ru_assets.py <путь_к_скриншоту>

Словарь берётся из данных Exiled Exchange 2 (MIT, Kvan7 / Awakened PoE Trade):
  dev/artifacts/items_ru.ndjson содержит пары "name" (русское) и "refName" (английское).
Шаблон заголовка вырезается из скриншота панели наград на русском клиенте.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "dev" / "artifacts" / "items_ru.ndjson"
OUT_LANG = ROOT / "assets" / "lang"
OUT_TPL = ROOT / "assets" / "templates"

# Заголовок панели, найденный OCR на живом скриншоте 1601x925.
HEADER_BOX = (147, 116, 348, 164)

# Пары, которых нет в дампе Exiled Exchange 2. Это не выдуманные имена: данные
# самой игры, опубликованные poe2db (страница /ru/Lesser_Charging_Rune):
#   BaseType  Lesser Charging Rune
#   BaseType  Малая руна заряда
#   NoteCode  lesser-charging-rune
#   Type      Metadata/Items/SoulCores/RuneWard2Lesser
#   Tags      rune, rune_lesser        DropLevel 5
# Без пары строка панели остаётся без цены: цену искать не по чему.
EXTRA_PAIRS = {
    "Малая руна заряда": "Lesser Charging Rune",
}

# Опечатки в поле refName русского дампа. Английский дамп на той же строке и
# список предметов торгового сайта дают другое имя — берём его, иначе цена
# ищется по несуществующему имени и предмет остаётся без цены.
#   строка 3326: дамп RU `Brynabas`      / дамп EN `Byrnabas`      / сайт `Byrnabas`
#   строка 3846: дамп RU `Splinter of Loratta` / дамп EN `Splinter of Lorrata`
#   строка 3921: дамп RU `The Immortan`  / дамп EN `The Road Warrior`
FIXUPS = {
    "Барнабас": "Byrnabas",
    "Воин дорог": "The Road Warrior",
    "Осколок Лоратты": "Splinter of Lorrata",
}


def build_dict() -> int:
    mapping: dict[str, str] = {}
    with SRC.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            ru, en = rec.get("name"), rec.get("refName")
            if ru and en:
                mapping.setdefault(ru, en)
    for ru, en in EXTRA_PAIRS.items():
        mapping.setdefault(ru, en)
    for ru, en in FIXUPS.items():
        if ru in mapping:
            mapping[ru] = en
    OUT_LANG.mkdir(parents=True, exist_ok=True)
    dst = OUT_LANG / "ru_items.json"
    dst.write_text(json.dumps(mapping, ensure_ascii=False, indent=0, sort_keys=True),
                   encoding="utf-8")
    print(f"словарь RU->EN: {len(mapping)} пар -> {dst.relative_to(ROOT)}")
    return len(mapping)


def build_template(shot: Path) -> None:
    OUT_TPL.mkdir(parents=True, exist_ok=True)
    img = Image.open(shot)
    crop = img.crop(HEADER_BOX)
    dst = OUT_TPL / "header_runeshape_ru.png"
    crop.save(dst)
    print(f"шаблон заголовка: {crop.size} -> {dst.relative_to(ROOT)}")


def main() -> None:
    shot = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(
        r"D:\Project_AI\Testers\POE2 OVERLE\2026-10-01 03-12-08.png")
    build_dict()
    if shot.exists():
        build_template(shot)
    else:
        print(f"скриншот не найден: {shot}")


if __name__ == "__main__":
    main()
