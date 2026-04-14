"""
OCR-движок для KAG

Использует Tesseract OCR (https://github.com/tesseract-ocr/tesseract)
для распознавания текста на изображениях и PDF-страницах.

Поддержка русского языка через пакет tesseract-ocr-rus.

Зависимости:
- pytesseract — Python-обёртка для Tesseract
- Pillow — обработка изображений
- pdf2image — рендеринг PDF страниц в изображения
"""

from typing import Optional, List, Dict, Any
from pathlib import Path
from PIL import Image
import pytesseract
from loguru import logger


class OCREngine:
    """
    Движок оптического распознавания символов.

    Поддерживает:
    - OCR изображений (PNG, JPG, TIFF, BMP)
    - OCR PDF-документов (через рендеринг страниц в изображения)
    - Распознавание русского и английского текста
    """

    def __init__(
        self,
        lang: str = "rus+eng",
        psm: int = 3,
        dpi: int = 300
    ):
        """
        Инициализация OCR-движка.

        Args:
            lang: Языки распознавания (по умолчанию русский+английский)
            psm: Page Segmentation Mode (3 = автоматически, по умолчанию)
            dpi: Разрешение для рендеринга PDF страниц
        """
        self._lang = lang
        self._psm = psm
        self._dpi = dpi
        self._tesseract_available = False

        # Проверяем доступность Tesseract
        try:
            pytesseract.get_tesseract_version()
            self._tesseract_available = True
            logger.info(f"Tesseract OCR доступен, языки: {lang}")
        except Exception as e:
            logger.warning(f"Tesseract OCR недоступен: {e}")

    @property
    def is_available(self) -> bool:
        """Проверить доступность Tesseract"""
        return self._tesseract_available

    def extract_text_from_image(self, image_path: str) -> str:
        """
        Извлечь текст из изображения.

        Args:
            image_path: Путь к файлу изображения

        Returns:
            Распознанный текст
        """
        if not self._tesseract_available:
            logger.warning("Tesseract недоступен, пропускаю OCR")
            return ""

        try:
            image = Image.open(image_path)
            text = pytesseract.image_to_string(
                image,
                lang=self._lang,
                config=f"--psm {self._psm}"
            ).strip()

            logger.info(
                f"OCR изображения завершён: {Path(image_path).name}, "
                f"символов: {len(text)}"
            )
            return text

        except Exception as e:
            logger.error(f"Ошибка OCR изображения {image_path}: {e}")
            return ""

    def extract_text_from_pdf(self, pdf_path: str) -> Dict[str, Any]:
        """
        Извлечь текст из PDF через OCR.

        Рендерит каждую страницу PDF в изображение и распознаёт текст.

        Args:
            pdf_path: Путь к PDF-файлу

        Returns:
            Словарь: {"pages": [{"page": N, "text": "..."}], "total_pages": N}
        """
        if not self._tesseract_available:
            logger.warning("Tesseract недоступен, пропускаю OCR PDF")
            return {"pages": [], "total_pages": 0}

        try:
            from pdf2image import convert_from_path

            logger.info(f"OCR PDF: {pdf_path}, dpi={self._dpi}")

            # Рендерим страницы в изображения
            images = convert_from_path(pdf_path, dpi=self._dpi)

            pages = []
            for page_num, image in enumerate(images, 1):
                text = pytesseract.image_to_string(
                    image,
                    lang=self._lang,
                    config=f"--psm {self._psm}"
                ).strip()

                pages.append({
                    "page": page_num,
                    "text": text,
                    "char_count": len(text)
                })

                logger.debug(
                    f"Страница {page_num}/{len(images)}: "
                    f"символов={len(text)}"
                )

            logger.info(
                f"OCR PDF завершён: {Path(pdf_path).name}, "
                f"страниц: {len(pages)}"
            )

            return {
                "pages": pages,
                "total_pages": len(pages)
            }

        except Exception as e:
            logger.error(f"Ошибка OCR PDF {pdf_path}: {e}")
            return {"pages": [], "total_pages": 0}


# Глобальный экземпляр
ocr_engine = OCREngine()
