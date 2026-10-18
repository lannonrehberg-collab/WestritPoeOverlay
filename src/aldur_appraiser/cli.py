"""Command-line interface for the pricing core (game-independent).

    appraiser price "Divine Orb" 3            # value a reward option
    appraiser price "divin orb" 3 --fuzzy     # fuzzy-snap a noisy name
    appraiser table --top 15                  # dump the price table
    appraiser image panel.png                 # appraise rewards from an image
    appraiser run                             # live overlay loop (capture+detect)
    appraiser                                 # no-op (Phase-0 smoke test)
"""

from __future__ import annotations

import argparse
import sys

from aldur_appraiser.config import load_config
from aldur_appraiser.pricing.cache import get_or_fetch
from aldur_appraiser.pricing.client import PricingError
from aldur_appraiser.pricing.valuation import evaluate


def _load_prices(args: argparse.Namespace):
    cfg = load_config().pricing
    league = args.league or cfg.league
    base = args.base or cfg.base
    return get_or_fetch(
        league,
        base,
        ttl_minutes=cfg.cache_ttl_minutes,
        realm=cfg.realm,
        categories=cfg.categories,
    ), base


def _snap(name: str, names, *, cutoff: int = 80) -> str | None:
    # Delegate to the pipeline's snapper so `price --fuzzy` matches exactly what
    # the overlay does (fuzz.ratio, not WRatio -> no bogus token-overlap matches).
    from aldur_appraiser.parse import snap_name

    return snap_name(name, names, score_cutoff=cutoff)


def cmd_price(args: argparse.Namespace) -> int:
    cached, base = _load_prices(args)
    name = args.name
    if name not in cached.table and args.fuzzy:
        snapped = _snap(name, list(cached.table))
        if snapped:
            print(f"(snapped {name!r} -> {snapped!r})")
            name = snapped

    from aldur_appraiser.pricing.valuation import divine_rate, format_value

    result = evaluate([(args.qty, name)], cached.table)
    v = result.items[0]
    stale = " [STALE]" if cached.stale else ""
    if v.known:
        val, unit = format_value(v.total, divine_rate(cached.table), base_unit=base)
        print(f"{v.qty}x {v.name} = {val:.2f} {unit} ({v.unit:.4f} {base} each){stale}")
    else:
        print(f"{v.qty}x {v.name} = unknown (no market price){stale}")
    return 0


def cmd_image(args: argparse.Namespace) -> int:
    from aldur_appraiser.pipeline import appraise_image
    from aldur_appraiser.vision import read_image
    from aldur_appraiser.vision.detect import PanelDetector

    image = read_image(args.path)
    if image is None:
        print(f"error: could not read image {args.path!r}", file=sys.stderr)
        return 1

    detector = None if args.no_detect else PanelDetector()
    cached, base = _load_prices(args)
    result = appraise_image(image, cached.table, detector=detector)
    if not result.items:
        print("no reward options recognised in image")
        return 0

    from aldur_appraiser.pricing.valuation import divine_rate, format_value

    dr = divine_rate(cached.table)

    def fmt(total: float) -> str:
        val, unit = format_value(total, dr, base_unit=base)
        return f"{val:.2f} {unit}"

    stale = " [STALE PRICES]" if cached.stale else ""
    print(f"Reward ranking (base={base}){stale}:")
    for v in result.items:
        marker = " <-- BEST" if v.is_best else ""
        if v.known:
            print(f"  {v.qty}x {v.name:<24} {fmt(v.total):>14}{marker}")
        else:
            print(f"  {v.qty}x {v.name:<24} {'unknown':>14}{marker}")
    if result.incomplete:
        print("  (comparison incomplete: an option has no market price)")
    for v in result.bonus_items:
        val = fmt(v.total) if v.known else "unknown"
        print(f"  + bonus (always paid): {v.qty}x {v.name} — {val}")
    return 0


def cmd_update_check(args: argparse.Namespace) -> int:
    from aldur_appraiser import __version__, updates

    latest = updates.latest_version()
    if latest is None:
        print("could not reach the update server", file=sys.stderr)
        return 1
    if updates.is_newer(latest, __version__):
        print(f"update available: v{latest} (you have v{__version__})")
        print(f"  download: {updates.RELEASES_PAGE}")
    else:
        print(f"up to date (v{__version__})")
    return 0


def cmd_selftest(args: argparse.Namespace) -> int:
    """Offline check that bundled vision deps load (used by the Windows CI)."""
    import numpy as np

    from aldur_appraiser.vision import ocr
    from aldur_appraiser.vision.detect import PanelDetector

    PanelDetector()  # loads the detection template from the bundle
    engine = ocr.get_engine()  # loads RapidOCR + onnxruntime models from the bundle
    engine.lines(np.zeros((64, 256, 3), dtype=np.uint8))  # exercise inference
    from PySide6 import QtWidgets  # noqa: F401  -> verify the Qt overlay is bundled

    print("selftest OK: detection template + OCR engine + Qt all loaded")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    from aldur_appraiser.app import run_app

    mode = "console" if args.console else ("overlay" if args.overlay else "auto")
    style = "corner" if args.corner else "inline"  # inline is the default
    return run_app(backend=args.backend, mode=mode, style=style, refresh=args.refresh)


def cmd_capture_test(args: argparse.Namespace) -> int:
    import cv2

    from aldur_appraiser.vision.capture import default_backend, open_capture

    backend = args.backend or default_backend()
    print(f"capture backend: {backend}")
    if backend == "portal":
        print("A one-time screen-share dialog may appear — pick your game monitor "
              "and 'Share'. The choice is remembered (restore token).")
    try:
        with open_capture(monitor=args.monitor, backend=backend) as cap:
            frame = cap.grab()
    except Exception as exc:  # noqa: BLE001 - surface any backend error to the user
        print(f"capture failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    print(f"grabbed frame: {frame.shape[1]}x{frame.shape[0]} max={int(frame.max())}")
    if int(frame.max()) == 0:
        print("frame is all black — capture is not seeing screen content", file=sys.stderr)
        return 1
    from aldur_appraiser.vision import write_image

    if not write_image(args.out, frame):
        print(f"error: could not save image to {args.out!r}", file=sys.stderr)
        return 1
    print(f"saved {args.out}")
    return 0


def cmd_trigger(args: argparse.Namespace) -> int:
    """Poke a running `appraiser run` to appraise now (bind to a desktop shortcut)."""
    from aldur_appraiser.trigger import send

    if send():
        return 0
    print("no running 'appraiser run' found to trigger", file=sys.stderr)
    return 1


def cmd_temple(args: argparse.Namespace) -> int:
    try:
        import PySide6  # noqa: F401
    except ImportError:
        print('temple planner needs PySide6 (pip install -e ".[overlay]")', file=sys.stderr)
        return 1
    from aldur_appraiser.temple.editor import run_editor

    return run_editor()


def cmd_table(args: argparse.Namespace) -> int:
    cached, base = _load_prices(args)
    rows = sorted(cached.table.items(), key=lambda kv: kv[1], reverse=True)
    if args.top:
        rows = rows[: args.top]
    stale = " [STALE CACHE]" if cached.stale else ""
    print(f"{len(cached.table)} currencies, base={base}, age={cached.age_minutes:.0f}min{stale}")
    for name, price in rows:
        print(f"  {name:<28} {price:>14.4f}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="appraiser", description=__doc__)
    p.add_argument("--league", help="override league (default: config)")
    p.add_argument("--base", help="override base currency (default: config)")
    sub = p.add_subparsers(dest="command")

    pp = sub.add_parser("price", help="value a single reward option")
    pp.add_argument("name", help="currency name (canonical)")
    pp.add_argument("qty", type=int, help="quantity")
    pp.add_argument("--fuzzy", action="store_true", help="fuzzy-snap a noisy name")
    pp.set_defaults(func=cmd_price)

    pi = sub.add_parser("image", help="appraise rewards from a panel image")
    pi.add_argument("path", help="path to a screenshot / reward-panel image")
    pi.add_argument(
        "--no-detect",
        action="store_true",
        help="skip panel detection and OCR the whole frame",
    )
    pi.set_defaults(func=cmd_image)

    sub.add_parser("selftest", help="offline check that bundled vision deps load").set_defaults(
        func=cmd_selftest
    )
    sub.add_parser("update-check", help="check GitHub for a newer release").set_defaults(
        func=cmd_update_check
    )

    pr = sub.add_parser("run", help="live loop: capture + detect + appraise (overlay by default)")
    pr.add_argument("--backend", choices=["portal", "mss"], help="force a capture backend")
    grp = pr.add_mutually_exclusive_group()
    grp.add_argument("--overlay", action="store_true", help="force the Qt overlay HUD")
    grp.add_argument("--console", action="store_true", help="force plain console output")
    sgrp = pr.add_mutually_exclusive_group()
    sgrp.add_argument(
        "--inline", action="store_true", help="inline per-row value chips (default)"
    )
    sgrp.add_argument(
        "--corner", action="store_true", help="corner HUD list instead of inline chips"
    )
    pr.add_argument(
        "--refresh", action="store_true", help="force a fresh price fetch (ignore cache)"
    )
    pr.set_defaults(func=cmd_run)

    pc = sub.add_parser("capture-test", help="grab one screen frame (tests the capture backend)")
    pc.add_argument("--backend", choices=["portal", "mss"], help="force a capture backend")
    pc.add_argument("--monitor", type=int, default=1, help="monitor index (mss backend)")
    pc.add_argument("--out", default="/tmp/aldur_capture.png", help="where to save the frame")
    pc.set_defaults(func=cmd_capture_test)

    pt = sub.add_parser("table", help="dump the price table")
    pt.add_argument("--top", type=int, default=0, help="show only top N by value")
    pt.set_defaults(func=cmd_table)

    sub.add_parser("temple", help="open the Vaal Temple planner (interactive editor)").set_defaults(
        func=cmd_temple
    )
    sub.add_parser(
        "trigger", help="appraise now in a running 'run' (bind to a keyboard shortcut)"
    ).set_defaults(func=cmd_trigger)
    return p


def main(argv: list[str] | None = None) -> int:
    # Double-clicking the frozen .exe passes no args -> launch the live overlay.
    if argv is None and len(sys.argv) == 1 and getattr(sys, "frozen", False):
        argv = ["run"]
    args = build_parser().parse_args(argv)
    if not getattr(args, "command", None):
        print("aldur-appraiser: pricing core ready. Try 'appraiser table --top 10'.")
        return 0
    try:
        return args.func(args)
    except PricingError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
