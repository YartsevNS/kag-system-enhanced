#!/usr/bin/env bash
# Пересборка ONNX-моделей детекторов из ОФИЦИАЛЬНЫХ весов PaddlePaddle (Apache-2.0).
#
# Зачем скрипт: в образ вшиты два детектора, и они не «скачались откуда-то», а собраны нами из официальных
# весов. Чтобы артефакт не был магическим, вот точный рецепт (проверен 05.10.2026 на ферме 41).
#
# PaddlePaddle нужен ТОЛЬКО здесь, на сборочной машине: в рантайм-образ он не попадает.
#
# Что получится:
#   pp_doclayout_v3_int8.onnx       — детектор ОБЛАСТИ таблицы (второй шанс, если сетка по линиям не нашла)
#   rt_detr_wireless_cell_int8.onnx — детектор ЯЧЕЕК (безлинейные/прозаические таблицы)
#
# Затем файлы кладутся в build-контекст: <репо>/assets/models/ (каталог в .gitignore, в git им не место).
set -euo pipefail

WORK=${WORK:-/home/yartsevn/paddle-build}
mkdir -p "$WORK"
cd "$WORK"

echo "=== 1) сборочный venv (paddlepaddle только тут) ==="
[ -d .venv ] || python3 -m venv .venv
.venv/bin/pip install -q --upgrade pip setuptools
# setuptools нужен: paddle импортирует его из cpp_extension
.venv/bin/pip install -q paddlepaddle==3.0.0 paddle2onnx huggingface_hub

echo "=== 2) официальные веса: детекторы ЯЧЕЕК (wired и wireless) ==="
.venv/bin/python - <<'PY'
from huggingface_hub import snapshot_download
for name, dst in (("RT-DETR-L_wireless_table_cell_det", "wireless"),
                  ("RT-DETR-L_wired_table_cell_det", "wired")):
    print(dst, "->", snapshot_download(f"PaddlePaddle/{name}", local_dir=f"{dst}"))
PY

echo "=== 3) конвертация в ONNX (opset 16) ==="
for pair in "wireless cell_wireless" "wired cell_wired"; do
  set -- $pair
  .venv/bin/paddle2onnx --model_dir "$WORK/$1" \
    --model_filename inference.json --params_filename inference.pdiparams \
    --save_file "$WORK/$2.onnx" --opset_version 16 --enable_onnx_checker True
done

echo "=== 4) контракт: снимаем с ФАЙЛА (не из документации) ==="
.venv/bin/python - <<'PY'
import hashlib
import onnxruntime as ort
for name in ("cell_wireless", "cell_wired"):
    path = f"{name}.onnx"
    sess = ort.InferenceSession(path, providers=["CPUExecutionProvider"])
    print(name, "| sha256", hashlib.sha256(open(path, "rb").read()).hexdigest())
    print("   входы:", [(i.name, i.shape) for i in sess.get_inputs()])
    print("   выходы:", [(o.name, o.shape) for o in sess.get_outputs()])
PY
# Ожидаемый контракт (проверено):
#   входы  im_shape [N,2], image [N,3,640,640], scale_factor [N,2]
#   выходы fetch_name_0 [300*N, 6] = [cls_id, score, x0, y0, x1, y1], fetch_name_1 — счётчик (всегда 300)
#   ВАЖНО: подавать im_shape=[[640,640]] и scale_factor=[[1,1]] — модель отдаёт боксы в системе 640x640.
#   С «правильными» Paddle-входами (im_shape=[h,w], scale=640/h) боксы выходят за пределы кропа.
#   Детектор ОБЛАСТИ таблицы (PP-DocLayoutV3) — другой контракт: pixel_values [1,3,800,800],
#   выходы logits [1,300,25] + pred_boxes [1,300,4], класс таблицы 21; берётся из HF-зеркала
#   thesanogoeffect/PP-DocLayoutV3-ONNX, веса-первоисточник PaddlePaddle/PP-DocLayoutV3_safetensors.

echo "=== 5) INT8: точность держится, размер в 3,7 раза меньше (на Broadwell медленнее) ==="
# Замер 05.10.2026: детектор ячеек FP32 123,4 МБ / 0,44 с → INT8 32,3 МБ / 0,81 с,
# совпадение боксов 15 из 15 (медиана IoU 0,997) на конспекте и 287 из 300 (IoU 0,989) на накладной.
# Детектор области: IoU 0,994 и 0,997, ложных срабатываний нет. То есть INT8 — выбор ради размера.
.venv/bin/python - <<'PY'
from pathlib import Path
from onnxruntime.quantization import QuantType, quantize_dynamic
for src, dst in (("cell_wireless.onnx", "cell_wireless_int8.onnx"),):
    if not Path(dst).exists():
        quantize_dynamic(model_input=src, model_output=dst, per_channel=True, weight_type=QuantType.QUInt8)
    print(dst, f"{Path(dst).stat().st_size/1024/1024:.1f} МБ")
PY

echo
echo "Готово. Положите файлы в build-контекст репозитория:"
echo "  mkdir -p <репо>/assets/models"
echo "  cp $WORK/cell_wireless_int8.onnx <репо>/assets/models/rt_detr_wireless_cell_int8.onnx"
echo "  cp <детектор-области>/pp_doclayout_v3_int8.onnx <репо>/assets/models/"
echo "И проверьте замером на своих страницах (scripts/bakeoff/cell_detector_test.py)."
