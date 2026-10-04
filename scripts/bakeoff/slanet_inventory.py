"""Инвентаризация моделей структуры таблиц (RapidTable SLANet) + проверка селектора numeric/prose.

1) Находит .onnx структуры таблиц и словари токенов, считает размеры и SHA256 (для отчёта и сборки).
2) Прогоняет эвристику «числовая / прозаическая таблица» на двух наших документах с известной истиной.
"""
import hashlib
import json
import re
import sys
from pathlib import Path

VENVS = [Path("/home/yartsevn/table-bakeoff/.venv"), Path("/home/yartsevn/ocr-bakeoff/.venv")]
NUM_ONLY = re.compile(r"^[-+]?\d*\.?\d+$")
DATE = re.compile(r"^\d{2}\.\d{2}\.\d{4}$")


def sha256(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def inventory() -> None:
    print("=== 1) модели структуры таблиц и словари === ")
    seen = set()
    for venv in VENVS:
        sp = next((p for p in [venv / "lib/python3.12/site-packages", venv / "lib/python3.11/site-packages"]
                   if p.exists()), None)
        if not sp:
            continue
        print(f"-- {venv}")
        try:
            import subprocess
            ver = subprocess.run([str(venv / "bin/pip"), "show", "rapid-table"],
                                 capture_output=True, text=True, timeout=60).stdout
            print("   " + next((l for l in ver.splitlines() if l.startswith("Version")), "rapid-table версия? "))
        except Exception as e:  # noqa: BLE001
            print("   версию определить не вышло:", type(e).__name__)
        for pat in ("**/*.onnx", "**/*dict*.txt", "**/*keys*.txt", "**/*.charset"):
            for p in sorted(sp.glob(pat)):
                if p in seen or "rapidocr" in str(p):
                    continue
                name = p.name.lower()
                if not any(k in name for k in ("slanet", "table", "unitable", "dict", "keys", "token")):
                    continue
                seen.add(p)
                print(f"   {p.relative_to(sp)} | {p.stat().st_size/1024/1024:.2f} МБ | sha256 {sha256(p)[:16]}…")


def numeric_ratio(texts: list[str]) -> float:
    blocks = [t.strip() for t in texts if t and t.strip()]
    if not blocks:
        return 0.0
    hits = 0
    for b in blocks:
        t = b.replace(" ", "").replace(",", ".")
        if NUM_ONLY.match(t) or DATE.match(t):
            hits += 1
    return hits / len(blocks)


def page_texts(path: Path) -> list[str]:
    """Строки страницы нашим штатным OCR (cyrillic, как в конвейере)."""
    import fitz
    import cv2
    import numpy as np
    from rapidocr import RapidOCR
    from rapidocr.utils.typings import LangRec, ModelType, OCRVersion

    eng = RapidOCR(params={"Rec.ocr_version": OCRVersion.PPOCRV5, "Rec.lang_type": LangRec.CYRILLIC,
                           "Rec.model_type": ModelType.MOBILE})
    if path.suffix.lower() == ".pdf":
        png = fitz.open(str(path))[0].get_pixmap(dpi=200).tobytes("png")
        img = cv2.imdecode(np.frombuffer(png, np.uint8), cv2.IMREAD_COLOR)
    else:
        img = cv2.imread(str(path))
    out = eng(img)
    return [t for t in (out.txts or []) if t and t.strip()]


def main() -> None:
    inventory()
    print("\n=== 2) селектор numeric/prose на наших документах ===")
    cases = [
        ("смета «Договор 09.11.2020» (ожидание: numeric)", Path("/home/yartsevn/table-bakeoff/img/smeta.pdf")),
        ("13611481-3 конспект (ожидание: prose, истина 4x3)",
         Path("/home/yartsevn/table-bakeoff/img/13611481-3.pdf")),
    ]
    for label, path in cases:
        if not path.exists():
            print(f"   {label:46} ФАЙЛА НЕТ: {path}")
            continue
        texts = page_texts(path)
        r = numeric_ratio(texts)
        verdict = "numeric" if r >= 0.40 else "prose"
        print(f"   {label:46} блоков {len(texts):4} | доля числовых {r:.2f} | вердикт {verdict}")


if __name__ == "__main__":
    main()
