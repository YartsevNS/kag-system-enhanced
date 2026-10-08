"""Сколько весят метаданные снимка и что даёт их снятие БЕЗ пересжатия пикселей.

Пересжатие JPEG меняет распознавание (замерено), а удаление служебных блоков из JPEG — нет:
пиксели остаются бит в бит. Здесь меряем, сколько такие блоки занимают у настоящих снимков:
EXIF с миниатюрой, GPS, данные камеры, комментарии.

Запуск внутри контейнера api:
  docker cp photo_meta_check.py kag-api:/tmp/ && docker exec kag-api python /tmp/photo_meta_check.py
"""
from __future__ import annotations

import pathlib

UPLOADS = pathlib.Path("/app/data/uploads")


def strip_jpeg_metadata(data: bytes) -> bytes:
    """Убрать служебные блоки JPEG (APPn, COM), не трогая пиксели.

    JPEG — это цепочка блоков: SOI, затем блоки с маркером FFxx и длиной, затем сжатые данные
    и EOI. Пиксели лежат в SOF/SOS-части, поэтому удаление APP1 (EXIF), APP2 (ICC), APP13 (IPTC)
    и COM (комментарии) не меняет изображение: распознавание получит те же пиксели.

    Осторожно: APP0 (JFIF) и Adobe-маркеры нужны некоторым просмотрщикам — их оставляем.
    """
    if not data.startswith(b"\xff\xd8"):
        return data
    out = bytearray(data[:2])            # SOI
    i = 2
    kept_app0 = False
    while i < len(data) - 1:
        if data[i] != 0xFF:
            break                        # дошли до сжатых данных — копируем остаток как есть
        marker = data[i + 1]
        if marker == 0xD9:               # EOI
            break
        if marker in (0xDA,):            # SOS — дальше сжатые данные, копируем всё до конца
            out += data[i:]
            return bytes(out)
        if marker in range(0xD0, 0xD8) or marker == 0x01:
            out += data[i:i + 2]
            i += 2
            continue
        length = int.from_bytes(data[i + 2:i + 4], "big")
        segment = data[i:i + 2 + length]
        keep = True
        if marker == 0xE0:               # APP0/JFIF — оставляем только первый
            keep = not kept_app0
            kept_app0 = kept_app0 or keep
        elif 0xE1 <= marker <= 0xEF or marker == 0xFE:   # APP1..APP15 (EXIF, ICC, IPTC) и комментарии
            keep = False
        if keep:
            out += segment
        i += 2 + length
    out += data[-2:] if data.endswith(b"\xff\xd9") else b""
    return bytes(out)


def main() -> None:
    imgs = [p for p in UPLOADS.glob("*") if p.suffix.lower() in (".jpg", ".jpeg")]
    if not imgs:
        print("в uploads нет jpeg")
        return
    imgs.sort(key=lambda p: p.stat().st_size, reverse=True)
    print(f"{'файл':52} {'всего':>9} {'после снятия метаданных':>24} {'выигрыш':>9}")
    total_before = total_after = 0
    for p in imgs[:8]:
        data = p.read_bytes()
        stripped = strip_jpeg_metadata(data)
        gain = (1 - len(stripped) / len(data)) * 100
        total_before += len(data)
        total_after += len(stripped)
        print(f"{p.name[:50]:52} {len(data) / 1e6:8.2f} МБ {len(stripped) / 1e6:20.2f} МБ {gain:8.1f}%")
    if total_before:
        print(f"\nитого по показанным: {total_before / 1e6:.2f} МБ -> {total_after / 1e6:.2f} МБ "
              f"({(1 - total_after / total_before) * 100:.1f}% без потери пикселей)")


if __name__ == "__main__":
    main()
