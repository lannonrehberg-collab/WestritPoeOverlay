"""OCR of the reward text column.

Primary engine: RapidOCR (pure pip wheel, no system dependency, robust on the
italic serif font). Fallback: Tesseract, used only if its binary is present.
The reward name is later fuzzy-snapped (parse.py), so raw name accuracy is
forgiving; the quantity has no fuzzy net, so we expose per-line confidence.

Heavy deps (rapidocr, cv2, pytesseract) are imported lazily so the pricing-only
install keeps working.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from typing import Protocol

import numpy as np


@dataclass(frozen=True)
class OcrLine:
    text: str
    conf: float          # 0..1; -1.0 if the engine reports none
    top: float           # y of the line's top edge, for ordering
    # bounding box (x0, y0, x1, y1) in ROI pixels; None if the engine omits it.
    box: tuple[float, float, float, float] | None = None


class OcrEngine(Protocol):
    def lines(self, img: np.ndarray) -> list[OcrLine]: ...


# --- preprocessing -----------------------------------------------------------


def preprocess(img: np.ndarray, *, scale: float = 2.0, threshold: bool = False) -> np.ndarray:
    """Upscale (helps small text); optionally grayscale+Otsu for Tesseract.

    RapidOCR handles colour/texture well, so threshold defaults off. Tesseract
    benefits from black-on-white, so its engine passes threshold=True.
    """
    import cv2

    out = img
    if scale and scale != 1.0:
        out = cv2.resize(out, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
    if threshold:
        gray = cv2.cvtColor(out, cv2.COLOR_BGR2GRAY) if out.ndim == 3 else out
        _, out = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    return out


# --- engines -----------------------------------------------------------------


class RapidOcrEngine:
    """Wraps rapidocr_onnxruntime. One detected text box -> one OcrLine."""

    def __init__(self, scale: float = 2.0):
        from rapidocr_onnxruntime import RapidOCR

        self._ocr = RapidOCR()
        self._scale = scale

    def lines(self, img: np.ndarray) -> list[OcrLine]:
        proc = preprocess(img, scale=self._scale, threshold=False)
        result, _ = self._ocr(proc)
        if not result:
            return []
        s = self._scale or 1.0
        out: list[OcrLine] = []
        for box, text, score in result:
            xs = [pt[0] for pt in box]
            ys = [pt[1] for pt in box]
            # OCR ran on the upscaled ROI -> divide back to original ROI pixels
            bbox = (min(xs) / s, min(ys) / s, max(xs) / s, max(ys) / s)
            out.append(
                OcrLine(text=str(text).strip(), conf=float(score), top=bbox[1], box=bbox)
            )
        out.sort(key=lambda ln: ln.top)
        return out


class TesseractEngine:
    """Fallback engine. Requires the tesseract binary on PATH."""

    def __init__(self, scale: float = 3.0, psm: int = 6):
        import pytesseract  # noqa: F401  (import-time check)

        self._scale = scale
        self._psm = psm

    def lines(self, img: np.ndarray) -> list[OcrLine]:
        import pytesseract
        from pytesseract import Output

        proc = preprocess(img, scale=self._scale, threshold=True)
        data = pytesseract.image_to_data(
            proc, config=f"--psm {self._psm}", output_type=Output.DICT
        )
        # Group words into lines by (block, paragraph, line).
        groups: dict[tuple[int, int, int], list[int]] = {}
        for i, word in enumerate(data["text"]):
            if not word.strip():
                continue
            key = (data["block_num"][i], data["par_num"][i], data["line_num"][i])
            groups.setdefault(key, []).append(i)

        out: list[OcrLine] = []
        for idxs in groups.values():
            text = " ".join(data["text"][i] for i in idxs)
            confs = [float(data["conf"][i]) for i in idxs if float(data["conf"][i]) >= 0]
            conf = (sum(confs) / len(confs) / 100.0) if confs else -1.0
            s = self._scale or 1.0
            x0 = min(data["left"][i] for i in idxs) / s
            y0 = min(data["top"][i] for i in idxs) / s
            x1 = max(data["left"][i] + data["width"][i] for i in idxs) / s
            y1 = max(data["top"][i] + data["height"][i] for i in idxs) / s
            box = (float(x0), float(y0), float(x1), float(y1))
            out.append(OcrLine(text=text.strip(), conf=conf, top=float(y0), box=box))
        out.sort(key=lambda ln: ln.top)
        return out


def tesseract_available() -> bool:
    return shutil.which("tesseract") is not None


# Сколько полос максимум досчитываем по отдельности за один вызов. Панель
# наград — это ~12 строк; ограничение не даёт случайному «пёстрому» кадру
# превратиться в сотню обращений к движку.
MAX_BAND_REPAIRS = 16


def _ink_profile(gray: np.ndarray, x0: int, x1: int) -> np.ndarray:
    """Сколько пикселей в строке отличается от фона этой же строки.

    Медиана берётся по каждой строке отдельно, поэтому градиент пергамента и
    тень панели текстом не считаются: важно только локальное отличие.
    """
    zone = gray[:, x0:x1].astype(np.int16)
    out = np.empty(zone.shape[0], dtype=np.int32)
    for y in range(zone.shape[0]):
        row = zone[y]
        out[y] = int((np.abs(row - np.median(row)) > 40).sum())
    return out


def text_bands(gray: np.ndarray, *, zone: tuple[float, float] = (0.30, 0.72),
               min_height: int = 8, max_bands: int = 40) -> list[tuple[int, int]]:
    """Полосы строк текста по вертикальному профилю чернил.

    Зона по ширине взята серединой кадра: она нужна только чтобы найти, ГДЕ идут
    строки. Столбец иконок рун слева, наши плашки с ценами справа и пейзаж за
    панелью в неё не попадают — на живом кадре панели эта зона даёт ровно 11
    полос, по одной на строку награды.
    """
    h, w = gray.shape[:2]
    if h < min_height * 2 or w < 40:
        return []
    ink = _ink_profile(gray, int(w * zone[0]), int(w * zone[1]))
    bands: list[tuple[int, int]] = []
    start: int | None = None
    for i in range(h + 1):
        value = int(ink[i]) if i < h else 0
        if value > 2 and start is None:
            start = i
        elif value <= 2 and start is not None:
            if i - start >= min_height:
                bands.append((start, i))
            start = None
    if len(bands) > max_bands:
        return []
    return bands


def _covered(lines: list[OcrLine], top: int, bottom: int) -> bool:
    """Есть ли среди прочитанного строка, попадающая в эту полосу."""
    for line in lines:
        if not line.box:
            continue
        overlap = min(line.box[3], bottom) - max(line.box[1], top)
        height = min(line.box[3] - line.box[1], bottom - top)
        if height > 0 and overlap > 0.5 * height:
            return True
    return False


def dedupe_rows(lines: list[OcrLine]) -> list[OcrLine]:
    """Убирает повторные прочтения одной строки, оставляя самое полное.

    Считаем повтором только то, что совпало И ПО ВЫСОТЕ, И ПО ГОРИЗОНТАЛИ:
    иначе склеивались разные строки, стоящие на одной высоте (например, текст
    награды и мусор, прочитанный с иконок слева).
    """
    out: list[OcrLine] = []
    for line in sorted(lines, key=lambda ln: (ln.top, ln.box[0] if ln.box else 0.0)):
        placed = False
        for index, other in enumerate(out):
            if not (line.box and other.box):
                continue
            overlap = min(line.box[3], other.box[3]) - max(line.box[1], other.box[1])
            height = min(line.box[3] - line.box[1], other.box[3] - other.box[1])
            if height <= 0 or overlap <= 0.5 * height:
                continue
            if (min(line.box[2], other.box[2]) - max(line.box[0], other.box[0])) <= 0:
                continue
            if len(line.text) > len(other.text):
                out[index] = line
            placed = True
            break
        if not placed:
            out.append(line)
    out.sort(key=lambda ln: ln.top)
    return out


class WindowsOcrEngine:
    """Русский OCR встроенным движком Windows (Windows.Media.Ocr).

    Ничего скачивать не нужно: движок и языковые модели уже в системе.
    Именно он читает стилизованный шрифт Path of Exile 2 на русском клиенте,
    тогда как RapidOCR (китайский/английский) на нём бесполезен.

    Читаем область целиком, а затем ДОБИРАЕМ полосами те строки, которых при
    чтении целиком не оказалось. Замер на живом кадре панели наград
    (audit_2026-10-13_panel1.png): когда панель не найдена и в кадр попадает
    весь экран, чтение целиком теряет строки «Умение: Фрагменты прошлого» и
    «Умение: Вечный марш» — они вообще не появляются. С добором полосами
    на этом же кадре читаются все 11 строк. Причина потерь в том, что движок
    сам решает, где проходят строки, и на высоком кадре с иконками, рамками и
    нашими плашками часть строк склеивает или пропускает.
    """

    def __init__(self, language: str = "ru", scale: float = 2.0):
        from winsdk.windows.globalization import Language
        from winsdk.windows.media.ocr import OcrEngine

        engine = OcrEngine.try_create_from_language(Language(language))
        if engine is None:
            raise RuntimeError(f"Windows OCR: язык {language!r} недоступен в системе")
        self._engine = engine
        self._scale = scale

    async def _recognize(self, img: np.ndarray, offset: tuple[int, int]) -> list[OcrLine]:
        import cv2
        from winsdk.windows.graphics.imaging import BitmapPixelFormat, SoftwareBitmap
        from winsdk.windows.security.cryptography import CryptographicBuffer

        bgra = cv2.cvtColor(img, cv2.COLOR_BGR2BGRA)
        height, width = bgra.shape[:2]
        buffer = CryptographicBuffer.create_from_byte_array(bgra.tobytes())
        bitmap = SoftwareBitmap.create_copy_from_buffer(
            buffer, BitmapPixelFormat.BGRA8, width, height
        )
        result = await self._engine.recognize_async(bitmap)

        scale = self._scale or 1.0
        dx, dy = offset
        out: list[OcrLine] = []
        for line in result.lines:
            words = list(line.words)
            if not words:
                continue
            x0 = min(w.bounding_rect.x for w in words) / scale + dx
            y0 = min(w.bounding_rect.y for w in words) / scale + dy
            x1 = max(w.bounding_rect.x + w.bounding_rect.width for w in words) / scale + dx
            y1 = max(w.bounding_rect.y + w.bounding_rect.height for w in words) / scale + dy
            text = " ".join(w.text for w in words)
            # Windows OCR не сообщает уверенность -> -1.0 (как у Tesseract без данных).
            out.append(OcrLine(text=text.strip(), conf=-1.0, top=y0, box=(x0, y0, x1, y1)))
        out.sort(key=lambda ln: ln.top)
        return out

    def _read(self, img: np.ndarray, offset: tuple[int, int] = (0, 0)) -> list[OcrLine]:
        import asyncio

        proc = preprocess(img, scale=self._scale, threshold=False)
        return asyncio.run(self._recognize(proc, offset))

    def lines(self, img: np.ndarray) -> list[OcrLine]:
        """Строки текста: обычное чтение области плюс добор пропущенных строк.

        Сначала читаем область целиком — как раньше. Потом смотрим, какие полосы
        текста остались БЕЗ единой прочитанной строки, и читаем только их, по
        отдельности. Уже прочитанное не трогаем: испортить то, что работает,
        такой добор не может.
        """
        gray = img if img.ndim == 2 else img.mean(axis=2)
        gray = np.ascontiguousarray(gray.astype(np.uint8))
        base = self._read(img)
        bands = text_bands(gray)
        if not bands:
            return base
        extra: list[OcrLine] = []
        repaired = 0
        for top, bottom in bands:
            if repaired >= MAX_BAND_REPAIRS:
                break
            pad = max(2, int((bottom - top) * 0.15))
            y0 = max(0, top - pad)
            y1 = min(img.shape[0], bottom + pad)
            if _covered(base, y0, y1):
                continue
            repaired += 1
            # Полоса целиком по ширине и отдельно её середина: столбец иконок
            # слева и наши плашки с ценами справа иногда мешают движку целиком.
            z0, z1 = int(img.shape[1] * 0.25), int(img.shape[1] * 0.85)
            extra.extend(self._read(img[y0:y1], (0, y0)))
            if z1 - z0 >= 8:
                extra.extend(self._read(img[y0:y1, z0:z1], (z0, y0)))
        # Добор не должен ни задваивать, ни затирать уже прочитанное.
        extra = dedupe_rows(extra)
        added = [ln for ln in extra
                 if ln.box and not _covered(base, ln.box[1], ln.box[3])]
        if not added:
            return base
        return sorted(base + added, key=lambda ln: ln.top)

    def read_full(self, img: np.ndarray) -> list[OcrLine]:
        """Прочитать область целиком, без добора полосами (для сверки)."""
        return self._read(img)


def get_engine(prefer: str = "rapidocr", *, language: str | None = None) -> OcrEngine:
    """Return an OCR engine.

    prefer="windows" -> встроенный Windows OCR (нужен для русского клиента);
    иначе RapidOCR, с откатом на Tesseract, если он есть.
    Никогда не падает из-за отсутствия одного движка — только если недоступны все.
    """
    if prefer == "windows":
        order = ["windows"]
    else:
        order = ["rapidocr", "tesseract"] if prefer == "rapidocr" else ["tesseract", "rapidocr"]
    errors: list[str] = []
    for name in order:
        try:
            if name == "windows":
                return WindowsOcrEngine(language=language or "ru")
            if name == "rapidocr":
                return RapidOcrEngine()
            if name == "tesseract":
                if not tesseract_available():
                    errors.append("tesseract: binary not found on PATH")
                    continue
                return TesseractEngine()
        except ImportError as exc:
            errors.append(f"{name}: {exc}")
        except RuntimeError as exc:
            errors.append(f"{name}: {exc}")
    raise RuntimeError("no OCR engine available: " + "; ".join(errors))


def read_reward_rows(img: np.ndarray, *, engine: OcrEngine | None = None) -> list[str]:
    """Convenience: ordered text lines from a reward-column ROI."""
    engine = engine or get_engine()
    return [ln.text for ln in engine.lines(img) if ln.text]
