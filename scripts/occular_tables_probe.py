"""Проверка штатного распознавателя таблиц Occular (TableRecognizer) на реальном файле.

Зачем: в Occular начиная с 0.3.0 есть TableRecognizer — он находит таблицу и восстанавливает сетку
строк/колонок вместе с объединёнными ячейками. Это установлено в наш контейнер (occular 0.4.1), но в
пайплайне KAG не используется. Перед тем как подключать, надо увидеть своими глазами, что он даёт на
наших файлах: сколько таблиц нашёл, какие размеры сетки, что в ячейках.

Запуск внутри api-контейнера (там есть occular и opencv):
    docker exec kag-api python /app/data/occular_tables_probe.py "/app/data/uploads/<файл>.png"
"""
import json
import sys
import time

import cv2


def describe(obj, name: str, limit: int = 3) -> None:
    """Показать структуру объекта — чтобы не угадывать поля, а увидеть их."""
    print(f"  {name}: тип {type(obj).__name__}", flush=True)
    if isinstance(obj, dict):
        print(f"    поля: {list(obj.keys())}", flush=True)
        for k, v in list(obj.items())[:limit]:
            short = str(v)[:120] + ("…" if len(str(v)) > 120 else "")
            print(f"      {k} = {short}", flush=True)
    elif isinstance(obj, list):
        print(f"    элементов: {len(obj)}", flush=True)
        if obj:
            describe(obj[0], f"{name}[0]", limit)


def main() -> int:
    path = sys.argv[1]
    img = cv2.imread(path)
    if img is None:
        print(f"  не удалось открыть файл: {path}", flush=True)
        return 1
    print(f"  файл: {path} | размер {img.shape[1]}x{img.shape[0]} px", flush=True)

    import occular

    print(f"  версия occular: {getattr(occular, '__version__', '?')}", flush=True)
    # Готовы ли веса табличной модели офлайн (для закрытого контура это критично)
    try:
        from occular import model_files
        for attr in dir(model_files):
            if "table" in attr.lower():
                print(f"    модуль весов: {attr} = {getattr(model_files, attr)}", flush=True)
        print(f"    кэш весов: {[p for p in dir(model_files) if p.isupper()][:6]}", flush=True)
    except Exception as e:  # noqa: BLE001
        print(f"    модуль весов недоступен: {type(e).__name__}: {str(e)[:80]}", flush=True)

    from occular import TableRecognizer

    tr = TableRecognizer()
    t0 = time.time()
    tables = tr(img)
    dt = time.time() - t0
    print(f"  TableRecognizer: таблиц найдено {len(tables)} | время {dt:.1f} с", flush=True)
    if tables:
        describe(tables[0], "первая таблица")
        for i, t in enumerate(tables, 1):
            bbox = t.get("bbox")
            rows, cols, cells = t.get("rows"), t.get("cols"), t.get("cells")
            print(f"    таблица {i}: bbox {bbox} | строк {len(rows or [])} | колонок {len(cols or [])} "
                  f"| ячеек {len(cells or [])}", flush=True)
            if cells:
                describe(cells[0], f"    ячейка таблицы {i}")
                texts = []
                for c in cells[:12]:
                    if isinstance(c, dict):
                        texts.append(str(c.get("text") or c.get("value") or "").strip()[:40])
                    else:
                        texts.append(str(c)[:40])
                print(f"      первые ячейки: {texts}", flush=True)

    # Для сравнения: что даёт обычное распознавание текста (без структуры)
    try:
        from occular import ocr
        t1 = time.time()
        res = ocr(path)      # API принимает путь, а не массив
        print(f"  обычное OCR: {time.time() - t1:.1f} с", flush=True)
        if isinstance(res, str):
            text = res
        elif isinstance(res, dict):
            text = res.get("text") or json.dumps(res, ensure_ascii=False)[:200]
        else:
            text = str(res)[:400]
        print(f"    символов: {len(text)} | первые 300: {text[:300]!r}", flush=True)
    except Exception as e:  # noqa: BLE001
        print(f"  обычное OCR: не удалось ({type(e).__name__}: {str(e)[:120]})", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
