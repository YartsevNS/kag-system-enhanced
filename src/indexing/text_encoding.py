"""Единое чтение текста с контролем кодировки: русский не должен портиться молча.

Зачем: в корпусе найдены документы, где текст лежал испорченным (UTF-8, прочитанный как Latin-1:
«µÑ�Ñ�Ð²Ð¾» вместо русских слов) — 18% фрагментов, 29 документов. Исходные файлы при этом целые,
то есть виноват конвейер, а дефект шёл МОЛЧА: никто не проверял, что после чтения получился осмысленный
русский текст. Каждая точка чтения текста решала вопрос кодировки по-своему (latin-1 вместо utf-8,
`read_text()` без указания кодировки, `errors='replace'`) — отсюда и потери.

Этот модуль — единственное место, где решается вопрос кодировки. Правила:

1. Пробуем по порядку: utf-8-sig → utf-8 → cp1251 → cp1252 → latin-1. Русский текст в UTF-8 ловится
   первым и портиться не может.
2. После чтения проверяем ДОЛЮ КИРИЛЛИЦЫ. Если текст выглядит русским, но кириллицы в нём почти нет —
   он испорчен: пробуем обратное преобразование, а если не выходит, помечаем файл подозрительным.
3. Подозрительный текст НЕ остаётся незамеченным: возвращается признак `suspicious`, который конвейер
   обязан занести в метаданные документа и показать в журнале. Дефект, который невозможно заметить, —
   это дефект, который живёт месяцами.
4. `errors='replace'` на входе запрещён: он превращает потерю в необратимую. Потери допустимы только
   на последнем шаге (latin-1), где иначе не прочитать вовсе.

Модуль без внешних зависимостей: работает и в контейнере, и на ноутбуке.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

_CYRILLIC = re.compile(r"[а-яА-ЯёЁ]")
# Характерные «диакритические» серии, которые получаются из кириллицы при чтении UTF-8 как Latin-1.
_MOJIBAKE_RUN = re.compile(r"[ÐÑÂÃÅÆ][\x80-\xbf\u0080-\u00ff\u0400-\u04ff]{1,}")
# Знаки, которых в нормальном русском тексте почти не бывает, а в порче — сколько угодно:
# латинские буквы с диакритикой (Ð, Ñ, Â, µ), редкие кириллические (ђ, ў, ѕ) и типографские кавычки.
_WEIRD = re.compile(
    "[\u0080-\u00bf\u00c0-\u00ff\u2018-\u201e\u2039\u203a"
    "\u0452\u0453\u0454\u0455\u0456\u0457\u0458\u0459\u045a\u045b\u045c\u045d\u045e\u045f]"
)
_MIN_CYRILLIC = 0.55  # ниже этого текст, претендующий на русский, считаем испорченным
_MAX_WEIRD = 0.03     # выше этой доли «странных» знаков текст считаем порчей

# Порядок пробуем осознанно: UTF-8 первым.
_ENCODINGS = ("utf-8-sig", "utf-8", "cp1251", "cp1252", "latin-1")


@dataclass
class Decoded:
    """Результат чтения текста."""

    text: str
    encoding: str
    repaired: bool = False
    suspicious: bool = False
    note: str = ""


def cyrillic_share(text: str) -> float:
    """Доля кириллицы среди букв (0..1). У пустого текста — 0."""
    letters = [c for c in (text or "") if c.isalpha()]
    if not letters:
        return 0.0
    return sum(1 for c in letters if _CYRILLIC.match(c)) / len(letters)


def weird_share(text: str) -> float:
    """Доля знаков, которых в нормальном русском тексте почти не бывает."""
    t = text or ""
    if not t:
        return 0.0
    return len(_WEIRD.findall(t)) / len(t)


def looks_like_mojibake(text: str) -> bool:
    """Похоже ли, что текст — это испорченный русский (UTF-8 как Latin-1 или порча второй ступени)."""
    t = (text or "").strip()
    if len(t) < 40:
        return False
    if weird_share(t) >= _MAX_WEIRD:
        return True
    if cyrillic_share(t) >= _MIN_CYRILLIC:
        return False
    runs = [r for r in _MOJIBAKE_RUN.findall(t) if len(r) >= 2]
    return len(runs) >= 3


def repair_cp1251_as_latin1(text: str) -> str:
    """Обратное преобразование «cp1251, прочитанный как Latin-1» (частая порча текстового слоя PDF).

    Посимвольно: в тексте бывают знаки вне Latin-1 (тире, кавычки, знак номера), из-за которых
    пакетное кодирование падает и текст остаётся битым.
    """
    out = []
    for ch in text or "":
        code = ord(ch)
        if code < 256:
            try:
                out.append(bytes([code]).decode("cp1251"))
                continue
            except UnicodeDecodeError:
                pass
        out.append(ch)
    return "".join(out)


def repair_mojibake(text: str) -> tuple[str, bool]:
    """Восстановить испорченный русский текст. Пробуем оба направления порчи.

    Направление 1: UTF-8, прочитанный как Latin-1/cp1252 (наш случай в корпусе — 29 документов).
    Направление 2: cp1251, прочитанный как Latin-1 (частая порча текстового слоя PDF).

    Возвращает (текст, без_потерь). Без потерь получается только если порча была чистой:
    если на каком-то шаге уже стоял `errors='replace'`, часть знаков потеряна навсегда
    (в тексте символы-заменители) — тогда честно сообщаем об этом.
    """
    variants: list[tuple[str, bool]] = []
    # Направление 1 — пакетно, с проверкой полноты.
    for enc in ("latin-1", "cp1252"):
        try:
            variants.append((text.encode(enc).decode("utf-8"), True))
        except Exception:
            continue
    # Направление 2 — посимвольно (потери возможны, поэтому помечаем).
    variants.append((repair_cp1251_as_latin1(text), False))

    best: tuple[float, str, bool] | None = None
    for fixed, lossless in variants:
        share = cyrillic_share(fixed)
        if share > _MIN_CYRILLIC and (best is None or share > best[0]):
            best = (share, fixed, lossless)
    if best:
        return best[1], best[2]

    # Совсем без вариантов: вытаскиваем что можем, но честно помечаем как неполное.
    try:
        fixed = text.encode("latin-1", errors="ignore").decode("utf-8", errors="ignore")
    except Exception:
        return text, False
    if cyrillic_share(fixed) > _MIN_CYRILLIC:
        return fixed, False
    return text, False


def decode_bytes(data: bytes) -> Decoded:
    """Прочитать байты текста: осознанный порядок кодировок + проверка русского.

    Логика: сначала берём первого кандидата, который не выглядит порчей. Если ВСЕ кандидаты
    выглядят порчей, то для каждого пробуем обратное преобразование и выбираем тот, что даёт
    осмысленный русский — иначе легко принять порчу за валидную cp1251 (проверено на живом случае).
    """
    if not data:
        return Decoded(text="", encoding="empty")

    candidates: list[Decoded] = []
    for enc in _ENCODINGS:
        try:
            text = data.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
        candidates.append(Decoded(text=text, encoding=enc))

    for candidate in candidates:
        if not looks_like_mojibake(candidate.text):
            candidate.note = f"кириллицы {cyrillic_share(candidate.text):.2f}"
            return candidate

    # Все кандидаты похожи на порчу: ищем восстановление с наибольшей долей кириллицы.
    best: tuple[float, str, bool, str] | None = None
    for candidate in candidates:
        fixed, lossless = repair_mojibake(candidate.text)
        if looks_like_mojibake(fixed):
            continue
        share = cyrillic_share(fixed)
        if share > _MIN_CYRILLIC and (best is None or share > best[0]):
            best = (share, fixed, lossless, candidate.encoding)

    if best:
        share, fixed, lossless, enc = best
        return Decoded(
            text=fixed, encoding=enc + "+repair", repaired=True, suspicious=not lossless,
            note=("текст был испорчен (UTF-8 как Latin-1), восстановлен"
                  + (" без потерь" if lossless else " — часть знаков потеряна навсегда")),
        )

    # Восстановить не удалось: отдаём наиболее правдоподобного кандидата и честно помечаем.
    worst = max(candidates, key=lambda c: cyrillic_share(c.text)) if candidates else None
    if worst is None:
        text = data.decode("utf-8", errors="replace")
        return Decoded(text=text, encoding="utf-8/replace", suspicious=True,
                       note="ни одна кодировка не подошла, часть знаков потеряна")
    return Decoded(text=worst.text, encoding=worst.encoding, suspicious=True,
                   note=f"текст не читается как русский (кириллицы {cyrillic_share(worst.text):.2f}, "
                        f"странных знаков {weird_share(worst.text):.2f})")


def decode_file(path: str | Path) -> Decoded:
    """Прочитать текстовый файл через единый разбор кодировки."""
    p = Path(path)
    try:
        data = p.read_bytes()
    except Exception as exc:  # noqa: BLE001 — причину надо видеть
        return Decoded(text="", encoding="error", suspicious=True, note=f"файл не прочитан: {exc}")
    return decode_bytes(data)


def guard(text: str) -> tuple[str, bool, str]:
    """Проверить уже готовый текст (например, пришедший из OCR или из сети).

    Возвращает (текст, подозрительный, пояснение). Если текст выглядит испорченным — пытаемся
    восстановить; если не удаётся, помечаем подозрительным, но текст не выбрасываем.
    """
    if not text:
        return text, False, ""
    if not looks_like_mojibake(text):
        return text, False, ""
    fixed, lossless = repair_mojibake(text)
    if cyrillic_share(fixed) > _MIN_CYRILLIC:
        return fixed, not lossless, "испорченный русский текст восстановлен" + ("" if lossless else " (с потерями)")
    return text, True, "текст похож на испорченный русский и не восстановился"


if __name__ == "__main__":  # быстрая самопроверка
    russian = "Требования к средствам защиты информации изложены в ГОСТ Р 50922-2006."
    print("utf-8        :", decode_bytes(russian.encode("utf-8")))
    print("cp1251       :", decode_bytes(russian.encode("cp1251")))
    mangled = russian.encode("utf-8").decode("latin-1")
    print("испорченный  :", decode_bytes(mangled.encode("utf-8")))
    lossy = mangled.encode("latin-1", errors="replace").decode("utf-8", errors="replace")
    print("с потерями   :", decode_bytes(lossy.encode("utf-8")))
    print("английский   :", decode_bytes(b"Security requirements are described in ISO 27001."))
