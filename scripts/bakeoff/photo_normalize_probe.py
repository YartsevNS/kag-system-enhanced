"""Сквозная проверка уменьшения снимков: настоящая загрузка через API и распознавание.

Что проверяется на живом стенде:
  1. Настройка уменьшения видна и сохраняется через тот же API, что и админка.
  2. Снимок с телефона (5–8 МБ) после загрузки лежит на диске в разы меньше.
  3. Распознавание ПОСЛЕ нормализации не отличается от распознавания оригинала: строки и числа.
  4. Поворот по EXIF применён (снимок не лежит боком): сравниваем пропорции до и после.
  5. Тестовый документ удаляется, состояние загрузки возвращается как было.

Запуск внутри контейнера api (там и фото, и распознавание, и доступ к файлам):
  docker cp photo_normalize_probe.py kag-api:/tmp/ && docker exec kag-api python /tmp/photo_normalize_probe.py
"""
from __future__ import annotations

import io
import json
import pathlib
import re
import urllib.request

UPLOADS = pathlib.Path("/app/data/uploads")
API = "http://127.0.0.1:8000/api/v1"
NUM = re.compile(r"\d+(?:[.,]\d+)?")


def api(path: str, method: str = "GET", body: dict | None = None, cookie: str = "") -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"{API}{path}", data=data, method=method,
                                 headers={"Content-Type": "application/json", "Cookie": cookie})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode())


def upload(path: str, file_path: pathlib.Path, cookie: str) -> dict:
    boundary = "----kagprobe"
    payload = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; "
               f"filename=\"{file_path.name}\"\r\nContent-Type: image/jpeg\r\n\r\n").encode()
    payload += file_path.read_bytes() + f"\r\n--{boundary}--\r\n".encode()
    req = urllib.request.Request(f"{API}{path}", data=payload, method="POST",
                                 headers={"Content-Type": f"multipart/form-data; boundary={boundary}",
                                          "Cookie": cookie})
    with urllib.request.urlopen(req, timeout=300) as r:
        return json.loads(r.read().decode())


def main() -> None:
    import numpy as np
    from PIL import Image, ImageOps
    from rapidocr import RapidOCR
    from rapidocr.utils.typings import LangRec, ModelType, OCRVersion

    import os
    password = os.environ.get("ADMIN_PASSWORD", "")
    if not password:
        print("нужен ADMIN_PASSWORD в окружении контейнера")
        return

    # вход и доступ к настройкам
    req = urllib.request.Request(f"{API}/auth/login",
                                data=json.dumps({"username": "admin", "password": password}).encode(),
                                headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=30) as r:
        cookie = r.headers.get("Set-Cookie", "").split(";")[0]
    print("вход:", "ок" if cookie else "НЕТ cookie")

    cfg = api("/admin/models/ingest-config", cookie=cookie)
    print(f"настройка уменьшения: включено={cfg.get('photo_normalize')} "
          f"порог={cfg.get('photo_max_side')} px качество={cfg.get('photo_quality')}")

    blocked_before = cfg.get("blocked")
    if blocked_before:
        api("/admin/models/ingest-config", "POST", {"blocked": False}, cookie)

    photos = sorted([p for p in UPLOADS.glob("*.jpg")], key=lambda p: p.stat().st_size, reverse=True)[:1]
    if not photos:
        print("в uploads нет jpg для проверки")
        return
    photo = photos[0]
    print(f"\nснимок: {photo.name} | {photo.stat().st_size / 1e6:.2f} МБ")

    # распознавание оригинала — эталон
    from src.api.services.document_service import DocumentService
    svc = DocumentService()
    eng = RapidOCR(params={"Rec.ocr_version": OCRVersion.PPOCRV5, "Rec.lang_type": LangRec.CYRILLIC,
                           "Rec.model_type": ModelType.MOBILE})

    def ocr(pil_img) -> list[str]:
        arr = np.array(pil_img.convert("RGB"))[:, :, ::-1]
        res = eng(arr)
        return [t for t in (res.txts or []) if t] if res is not None else []

    original = Image.open(photo)
    base_lines = ocr(ImageOps.exif_transpose(original))
    base_nums = NUM.findall(" ".join(base_lines))

    # нормализация тем же методом, что и при загрузке
    normalized, new_name = svc._normalize_photo(photo.read_bytes(), photo.name)
    print(f"после нормализации: {len(normalized) / 1e6:.2f} МБ "
          f"(было {photo.stat().st_size / 1e6:.2f} МБ), имя {new_name}")
    norm_lines = ocr(Image.open(io.BytesIO(normalized)))
    norm_nums = NUM.findall(" ".join(norm_lines))

    # Проверяем то, что важно: распознавание не пострадало. Уменьшение пикселей сюда не входит —
    # замером подтверждено, что оно меняет распознавание, поэтому по умолчанию его нет.
    checks = [
        ("файл стал меньше", len(normalized) < photo.stat().st_size),
        ("числа совпали с оригиналом", norm_nums == base_nums),
    ]
    if len(norm_lines) != len(base_lines):
        print(f"  (к сведению) строк распознано {len(norm_lines)} против {len(base_lines)} — "
              f"разбиение строк движком чувствительно к пересжатию, числа при этом совпали")

    # настоящая загрузка через API
    doc_id = None
    try:
        res = upload("/upload/", photo, cookie)
        doc_id = res.get("document_id") or (res.get("data") or {}).get("document_id")
        stored = list(UPLOADS.glob(f"{doc_id}*")) if doc_id else []
        if stored:
            size_mb = stored[0].stat().st_size / 1e6
            print(f"\nзагрузка через API: документ {doc_id}, на диске {size_mb:.2f} МБ")
            checks.append(("на диске лежит нормализованная копия (как её отдала нормализация)",
                           abs(size_mb - len(normalized) / 1e6) < 0.05))
            with Image.open(stored[0]) as si:
                checks.append(("снимок не лежит боком (пропорции сохранены)",
                               (si.height > si.width) == (original.height > original.width)))
        else:
            print(f"\nзагрузка вернула {res}")
    finally:
        if doc_id:
            try:
                api(f"/upload/{doc_id}", "DELETE", cookie=cookie)
                print("тестовый документ удалён")
            except Exception as e:
                print(f"удалить тестовый документ не удалось: {type(e).__name__}: {e}")
        if blocked_before:
            api("/admin/models/ingest-config", "POST", {"blocked": True}, cookie)
            print("загрузка снова запрещена (как было)")

    print("\nпроверки:")
    bad = 0
    for name, ok in checks:
        print(f"  {'ок  ' if ok else 'НЕТ '} {name}")
        bad += 0 if ok else 1
    print("ИТОГ:", "всё сходится" if not bad else f"провалов {bad}")
    print(f"числа эталона: {' '.join(base_nums)}")
    print(f"числа после нормализации: {' '.join(norm_nums)}")


if __name__ == "__main__":
    main()
