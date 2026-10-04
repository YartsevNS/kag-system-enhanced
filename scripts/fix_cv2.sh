#!/bin/sh
# Лечилка cv2: любой pip install этих табличных библиотек притягивает opencv-python
# (не headless), который пишет в тот же каталог cv2/ и ломает наш opencv-python-headless
# (ImportError: libGL.so.1). Лечение: снять opencv-python и переустановить headless.
# Использование: fix_cv2.sh /путь/к/venv
set -e
V="$1"
[ -d "$V/bin" ] || { echo "нет venv: $V"; exit 1; }
"$V/bin/pip" uninstall -y -q opencv-python 2>/dev/null || true
"$V/bin/pip" install -q --force-reinstall --no-deps opencv-python-headless==4.12.0.88
"$V/bin/python" -c "import cv2; assert hasattr(cv2,'cvtColor'); print('cv2 ок:', cv2.__version__)"
