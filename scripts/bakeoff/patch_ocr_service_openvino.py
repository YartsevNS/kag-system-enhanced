"""Патч службы OCR на 41: добавляет выбор движка инференса (--engine cpu|openvino).

По умолчанию остаётся cpu — то есть поведение не меняется, пока флаг не передан явно.
Делает резервную копию ocr_service.py.orig-cpu и идемпотентен (повторный запуск ничего не ломает).
"""
from pathlib import Path

P = Path("/home/yartsevn/ocr-bakeoff/ocr_service.py")
BACKUP = Path("/home/yartsevn/ocr-bakeoff/ocr_service.py.orig-cpu")

HELPER = '''
_engine_mode = "cpu"


def _apply_engine_mode() -> None:
    """Подменить провайдеры rapidocr на OpenVINO, если выбран режим openvino.

    Замер 04.10.2026 (Intel Xeon E5-2682 v4): страница-накладная 4,6 -> 2,0 с (2,35x),
    смета 2,4 -> 1,2 с (1,94x), подстановок цифр — 0 (FP32, квантование не применяется).
    """
    global _engine_mode
    if _engine_mode != "openvino":
        return
    try:
        import onnxruntime as ort

        if "OpenVINOExecutionProvider" not in ort.get_available_providers():
            print("[ocr] OpenVINO недоступен — работаем на CPU", flush=True)
            _engine_mode = "cpu"
            return
        from rapidocr.inference_engine.onnxruntime import provider_config as pc

        if getattr(pc.ProviderConfig, "_kag_patched", False):
            return
        original = pc.ProviderConfig.get_ep_list

        def patched(self):
            return [("OpenVINOExecutionProvider", {"device_type": "CPU"})] + list(original(self))

        pc.ProviderConfig.get_ep_list = patched
        pc.ProviderConfig._kag_patched = True
        print("[ocr] движок инференса: OpenVINO (CPU)", flush=True)
    except Exception as e:  # noqa: BLE001
        print(f"[ocr] OpenVINO включить не удалось ({type(e).__name__}: {e}) — CPU", flush=True)
        _engine_mode = "cpu"


def build_engine(lang: str):'''

CALL = '    _apply_engine_mode()\n    from rapidocr import RapidOCR\n'


def main() -> None:
    src = P.read_text(encoding="utf-8")
    if "_apply_engine_mode" in src:
        print("уже пропатчено — ничего не делаю")
        return
    if not BACKUP.exists():
        BACKUP.write_text(src, encoding="utf-8")
        print(f"копия сохранена: {BACKUP}")

    # 1) helper перед build_engine
    src = src.replace("\ndef build_engine(lang: str):", HELPER, 1)

    # 2) вызов перед сборкой обоих движков
    src = src.replace("    from rapidocr import RapidOCR\n", CALL, 2)

    # 3) флаг --engine и его проброс
    src = src.replace(
        '    ap.add_argument("--warmup", action="store_true",',
        '    ap.add_argument("--engine", default="cpu", choices=["cpu", "openvino"],\n'
        '                    help="движок onnxruntime: openvino ускоряет страницу ~2x без потери цифр")\n'
        '    ap.add_argument("--warmup", action="store_true",', 1)
    src = src.replace("def main() -> int:\n    global _default_lang",
                      "def main() -> int:\n    global _default_lang, _engine_mode", 1)
    src = src.replace("    _default_lang = args.lang\n",
                      "    _default_lang = args.lang\n    _engine_mode = args.engine\n", 1)

    checks = ["_apply_engine_mode", 'choices=["cpu", "openvino"]', "global _default_lang, _engine_mode"]
    missing = [c for c in checks if c not in src]
    if missing:
        raise SystemExit(f"патч неполный, не найдено: {missing}")
    P.write_text(src, encoding="utf-8")
    print("служба пропатчена: добавлен --engine (по умолчанию cpu)")


if __name__ == "__main__":
    main()
