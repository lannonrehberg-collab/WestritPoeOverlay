"""Русский оверлей: проход «скриншот -> таблица цен» без запуска игры.

    py -m aldur_appraiser.ru_cli <скриншот> [--league "Forbidden Rites"] [--offline]

Удобно для проверки: берём готовый снимок панели наград и получаем цены.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np

from aldur_appraiser.localize import Reward, parse_lines
from aldur_appraiser.pricing.ru_prices import PriceBook
from aldur_appraiser.resources import resource_path
from aldur_appraiser.vision import read_image
from aldur_appraiser.vision.detect import PanelDetector
from aldur_appraiser.vision.ocr import get_engine

RU_TEMPLATE = "assets/templates/header_runeshape_ru.png"


def ocr_rewards(frame: np.ndarray, *, language: str = "ru", verbose: bool = True):
    """Находит панель и читает строки наград. -> (строки, инфо о панели)."""
    template = resource_path(RU_TEMPLATE)
    engine = get_engine("windows", language=language)

    if template.exists():
        detector = PanelDetector(template_path=template, scales=np.linspace(0.5, 1.6, 23))
        panel = detector.find_panel(frame)
        if panel is not None:
            roi = detector.reward_image(frame, panel)
            if verbose:
                print(f"панель найдена: x={panel.x} y={panel.y} {panel.w}x{panel.h} "
                      f"совпадение={panel.confidence:.3f}")
            lines = [ln.text for ln in engine.lines(roi)]
            return lines, panel

    if verbose:
        print("панель не найдена — читаю весь кадр")
    lines = [ln.text for ln in engine.lines(frame)]
    return lines, None


def price_rewards(rewards: list[Reward], book: PriceBook, *, verbose: bool = True):
    """Считает цену каждой награды в exalted. -> [(Reward, цена|None)]."""
    priced: list[tuple[Reward, float | None]] = []
    for reward in rewards:
        value: float | None = None
        if reward.en_name:
            if reward.kind == "gem":
                value = book.price_gem(reward.en_name, reward.gem_level)
            else:
                value = book.price_item(reward.en_name)
        priced.append((reward, value))
        if verbose:
            shown = f"{value:,.1f} exalted" if value else "?"
            print(f"   {reward.raw:44s} -> {shown}")
    return priced


def report(priced: list[tuple[Reward, float | None]]) -> None:
    """Итоговая таблица: что брать."""
    known = [(r, v) for r, v in priced if v]
    print("\n=== ИТОГ ===")
    if not known:
        print("цены определить не удалось")
        return
    best = max(known, key=lambda item: item[1] or 0)
    for reward, value in sorted(known, key=lambda item: -(item[1] or 0)):
        mark = "  <-- ЛУЧШИЙ ВЫБОР" if reward is best[0] else ""
        print(f"  {value:>12,.1f} exalted   {reward.ru_name or reward.en_name}{mark}")
    unknown = [r for r, v in priced if not v]
    if unknown:
        print("  без цены: " + ", ".join(r.ru_name or r.en_name or r.raw for r in unknown))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ru_cli", description="Цены наград PoE2 (русский клиент)")
    parser.add_argument("image", type=Path, help="скриншот с открытой панелью наград")
    parser.add_argument("--league", default="", help="лига (по умолчанию — автоопределение)")
    parser.add_argument("--offline", action="store_true", help="только кэш, без сети")
    args = parser.parse_args(argv)

    frame = read_image(args.image)
    if frame is None:
        print(f"не удалось прочитать изображение: {args.image}")
        return 2
    print(f"кадр: {frame.shape[1]}x{frame.shape[0]}")

    if not args.offline:
        book = PriceBook(league=args.league)
        book.load()
        print(f"лига: {book.league} | позиций в таблице: {book.size} | "
              f"курс: exalted=1, divine={book._rates.get('divine', 0):.0f}, "
              f"chaos={book._rates.get('chaos', 0):.1f}")
    else:
        book = PriceBook(league=args.league or "Forbidden Rites")
        book._load_cache()
        print(f"офлайн-режим: позиций из кэша {book.size}")

    lines, panel = ocr_rewards(frame)
    # Внутри найденной панели каждая строка — награда; на всём кадре засчитываем
    # только то, что совпало со словарём (иначе в список лезет HUD игры).
    rewards = parse_lines(lines, allow_unknown=panel is not None)
    print(f"\nстрок OCR: {len(lines)}, распознано наград: {len(rewards)}")
    priced = price_rewards(rewards, book)
    report(priced)

    # Сохраним кадр с областью наград — полезно для отладки
    return 0


if __name__ == "__main__":
    sys.exit(main())
