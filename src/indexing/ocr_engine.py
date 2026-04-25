"""
OCR Engine - унифицированный интерфейс для OCR

Объединяет Tesseract и LLM-based OCR в единый API.
Автоматически выбирает лучший доступный метод.

Приоритеты:
1. Tesseract - быстрый, для простых документов
2. LLM OCR - для сложных макетов, таблиц, русского текста

Использование:
    from src.indexing.ocr_engine import ocr_engine
    
    # Извлечение текста из изображения
    text = ocr_engine.extract_text_from_image("photo.jpg")
    
    # Извлечение из PDF
    result = ocr_engine.extract_text_from_pdf("doc.pdf")
    for page in result["pages"]:
        print(page["text"])
"""

import io
import logging
from pathlib import Path
from typing import Dict, Any, List, Optional

logger = logging.getLogger(__name__)


class OCREngine:
    """
    Унифицированный OCR движок.
    
    Поддерживает:
    - Tesseract (основной) - для простых документов
    - LLM OCR (fallback) - для сложных документов
    """
    
    def __init__(
        self,
        tesseract_lang: str = "rus+eng",
        llm_model: str = "deepseek-ocr:latest",
        llm_base_url: str = "http://192.168.50.41:11434",
        enable_llm_fallback: bool = True
    ):
        """
        Инициализация OCR движка.
        
        Args:
            tesseract_lang: Языки для Tesseract (rus+eng)
            llm_model: Модель для LLM OCR
            llm_base_url: URL Ollama сервера
            enable_llm_fallback: Использовать LLM как fallback
        """
        self._tesseract_lang = tesseract_lang
        self._llm_model = llm_model
        self._llm_base_url = llm_base_url
        self._enable_llm_fallback = enable_llm_fallback
        
        self._tesseract_available = False
        self._llm_available = False
        
        self._check_engines()
    
    def _check_engines(self):
        """Проверить доступность движков"""
        # Tesseract
        try:
            import pytesseract
            pytesseract.get_tesseract_version()
            self._tesseract_available = True
            logger.info(f"Tesseract OCR доступен: lang={self._tesseract_lang}")
        except Exception as e:
            logger.warning(f"Tesseract недоступен: {e}")
        
        # LLM OCR
        try:
            from src.indexing.llm_ocr import get_llm_ocr_engine
            llm = get_llm_ocr_engine(
                base_url=self._llm_base_url,
                model=self._llm_model
            )
            self._llm_available = llm.is_available
            if self._llm_available:
                logger.info(f"LLM OCR доступен: {self._llm_model}")
        except Exception as e:
            logger.warning(f"LLM OCR недоступен: {e}")
    
    @property
    def is_available(self) -> bool:
        """Проверить доступность любого OCR"""
        return self._tesseract_available or self._llm_available
    
    @property
    def tesseract_available(self) -> bool:
        return self._tesseract_available
    
    @property
    def llm_available(self) -> bool:
        return self._llm_available
    
    def _preprocess_image(self, image_path: str) -> Optional[str]:
        """
        Предварительная обработка изображения.
        
        - Исправляет поворот (deskew)
        - Улучшает контраст
        
        Returns:
            Путь к обработанному временному файлу или None
        """
        try:
            from PIL import Image, ImageEnhance, ImageFilter
            import tempfile
            import numpy as np
            
            img = Image.open(image_path)
            
            # Проверяем, нужно ли обрабатывать
            needs_processing = False
            
            # Поворот (deskew) - простая проверка
            try:
                import pytesseract
                data = pytesseract.image_to_data(img, output_type=pytesseract.Output.DICT)
                angles = [float(a) for a in data.get("angle", []) if a != "0"]
                if angles:
                    avg_angle = sum(angles) / len(angles)
                    if abs(avg_angle) > 0.5:
                        img = img.rotate(-avg_angle, fillcolor='white')
                        needs_processing = True
            except:
                pass
            
            # Улучшение контраста для сканов
            if img.mode in ("L", "RGB"):
                # Проверяем,是不是 скан (низкий контраст)
                if img.mode == "L":
                    hist = img.histogram()
                    if len(hist) > 50:
                        # Проверяем распределение
                        non_zero = sum(1 for h in hist if h > 0)
                        if non_zero < 20:
                            # Улучшаем контраст
                            enhancer = ImageEnhance.Contrast(img)
                            img = enhancer.enhance(1.5)
                            needs_processing = True
            
            if needs_processing:
                # Сохраняем во временный файл
                with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
                    img.save(tmp.name, "PNG")
                    return tmp.name
            
            return None
            
        except Exception as e:
            logger.debug(f"Preprocess error: {e}")
            return None
    
    def extract_text_from_image(
        self,
        image_path: str,
        use_llm: bool = False
    ) -> str:
        """
        Извлечь текст из изображения.
        
        Args:
            image_path: Путь к изображению
            use_llm: Принудительно использовать LLM
        
        Returns:
            Распознанный текст
        """
        text = ""
        
        # Предварительная обработка
        processed_path = self._preprocess_image(image_path)
        path_to_use = processed_path or image_path
        
        try:
            # Сначала пробуем Tesseract
            if self._tesseract_available and not use_llm:
                try:
                    import pytesseract
                    from PIL import Image
                    
                    img = Image.open(path_to_use)
                    text = pytesseract.image_to_string(
                        img,
                        lang=self._tesseract_lang
                    )
                    
                    if text and len(text.strip()) > 20:
                        logger.info(
                            f"Tesseract OCR: {Path(image_path).name}, "
                            f"{len(text)} символов"
                        )
                        return text.strip()
                except Exception as e:
                    logger.debug(f"Tesseract error: {e}")
            
            # Fallback на LLM если нужно
            if self._enable_llm_fallback and self._llm_available:
                try:
                    from src.indexing.llm_ocr import get_llm_ocr_engine
                    llm = get_llm_ocr_engine()
                    text = llm.extract_from_image(path_to_use)
                    
                    if text:
                        logger.info(
                            f"LLM OCR: {Path(image_path).name}, "
                            f"{len(text)} символов"
                        )
                except Exception as e:
                    logger.warning(f"LLM OCR error: {e}")
            
        finally:
            # Удаляем временный файл
            if processed_path and Path(processed_path).exists():
                try:
                    Path(processed_path).unlink()
                except:
                    pass
        
        return text.strip() if text else ""
    
    def extract_text_from_pdf(
        self,
        pdf_path: str,
        dpi: int = 150
    ) -> Dict[str, Any]:
        """
        Извлечь текст из PDF.
        
        Args:
            pdf_path: Путь к PDF
            dpi: Разрешение для рендеринга
        
        Returns:
            {
                "pages": [{"page": N, "text": "..."}],
                "total_pages": N,
                "ocr_used": "tesseract"|"llm"|"none"
            }
        """
        result = {
            "pages": [],
            "total_pages": 0,
            "ocr_used": "none"
        }
        
        try:
            import PyPDF2
            
            # Сначала пробуем просто извлечь текст
            with open(pdf_path, 'rb') as f:
                reader = PyPDF2.PdfReader(f)
                result["total_pages"] = len(reader.pages)
                
                for page_num, page in enumerate(reader.pages):
                    text = page.extract_text()
                    
                    # Если текст короткий - используем OCR
                    if not text or len(text.strip()) < 50:
                        logger.info(
                            f"Страница {page_num + 1}: текст={len(text) if text else 0}, "
                            f"применяю OCR..."
                        )
                        
                        # Рендерим страницу
                        try:
                            from pdf2image import convert_from_path
                            images = convert_from_path(
                                pdf_path,
                                dpi=dpi,
                                first_page=page_num + 1,
                                last_page=page_num + 1
                            )
                            
                            if images:
                                import tempfile
                                with tempfile.NamedTemporaryFile(
                                    suffix=".png", delete=False
                                ) as tmp:
                                    images[0].save(tmp.name, "PNG")
                                    tmp_path = tmp.name
                                    
                                    # Пробуем Tesseract
                                    if self._tesseract_available:
                                        text = self.extract_text_from_image(tmp_path)
                                        if text and len(text.strip()) > 20:
                                            result["ocr_used"] = "tesseract"
                                    
                                    # Если Tesseract не помог - LLM
                                    if (
                                        not text or len(text.strip()) < 20
                                    ) and self._llm_available:
                                        try:
                                            from src.indexing.llm_ocr import get_llm_ocr_engine
                                            llm = get_llm_ocr_engine()
                                            text = llm.extract_from_image(tmp_path)
                                            if text:
                                                result["ocr_used"] = "llm"
                                        except Exception as e:
                                            logger.debug(f"LLM error: {e}")
                                    
                                    # Удаляем временный файл
                                    try:
                                        Path(tmp_path).unlink()
                                    except:
                                        pass
                                    
                        except Exception as e:
                            logger.warning(f"PDF render error: {e}")
                    
                    result["pages"].append({
                        "page": page_num + 1,
                        "text": text.strip() if text else ""
                    })
            
            logger.info(
                f"PDF processed: {Path(pdf_path).name}, "
                f"страниц: {result['total_pages']}, "
                f"OCR: {result['ocr_used']}"
            )
            
        except Exception as e:
            logger.error(f"PDF extract error: {e}")
        
        return result
    
    def batch_extract(
        self,
        file_paths: List[str]
    ) -> List[Dict[str, Any]]:
        """
        Пакетная обработка файлов.
        
        Args:
            file_paths: Список путей к файлам
        
        Returns:
            Список результатов
        """
        results = []
        
        for file_path in file_paths:
            path = Path(file_path)
            ext = path.suffix.lower()
            
            if ext in (".jpg", ".jpeg", ".png", ".tiff", ".bmp"):
                text = self.extract_text_from_image(file_path)
            elif ext == ".pdf":
                result = self.extract_text_from_pdf(file_path)
                text = "\n\n---PAGE---\n\n".join(
                    p["text"] for p in result["pages"]
                )
            else:
                text = ""
            
            results.append({
                "file": path.name,
                "text": text,
                "success": bool(text)
            })
        
        return results


# Глобальный экземпляр
_ocr_engine: Optional[OCREngine] = None


def get_ocr_engine() -> OCREngine:
    """Получить экземпляр OCR движка"""
    global _ocr_engine
    
    if _ocr_engine is None:
        _ocr_engine = OCREngine()
    
    return _ocr_engine


# Обратная совместимость
ocr_engine = get_ocr_engine()