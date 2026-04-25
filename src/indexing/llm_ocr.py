"""
LLM-based OCR Engine для KAG

Использует Ollama vision модели для распознавания текста.
Поддерживает русский язык и сложные документы.

Модели:
- deepseek-ocr:latest - специализирована для OCR
- qwen3-vl:latest - хорошее качество
- granite3.2-vision:latest - для документов
- minicpm-v - компактная

Установка:
pip install ollama

Загрузка моделей:
ollama pull deepseek-ocr
ollama pull qwen3-vl
"""

import base64
from typing import Optional, Dict, Any, List
import json
import requests
from loguru import logger
from pathlib import Path


class OCRLLMEngine:
    """
    LLM-based OCR Engine использующий Ollama vision модели.
    
    Преимущества перед Tesseract:
    - Лучшее распознавание сложных макетов
    - Понимание контекста
    - Распознавание таблиц и структуры
    - Русский язык без дополнительной настройки
    """

    def __init__(
        self,
        base_url: str = "http://192.168.50.41:11434",
        model: str = "deepseek-ocr:latest",
        language: str = "russian",
        timeout: int = 120
    ):
        """
        Инициализация LLM OCR движка.
        
        Args:
            base_url: URL Ollama сервера
            model: Модель для OCR (deepseek-ocr, qwen3-vl, granite3.2-vision)
            language: Язык распознавания (russian, english)
            timeout: Таймаут в секундах
        """
        self._base_url = base_url
        self._model = model
        self._language = language
        self._timeout = timeout
        self._available = False
        
        self._check_model()
    
    def _check_model(self):
        """Проверить доступность модели"""
        try:
            response = requests.get(
                f"{self._base_url}/api/tags",
                timeout=10
            )
            if response.status_code == 200:
                models = response.json().get("models", [])
                model_names = [m.get("name", "") for m in models]
                
                if self._model in model_names:
                    self._available = True
                    logger.info(f"LLM OCR доступен: {self._model}")
                else:
                    # Ищем альтернативную vision модель
                    for alt in ["deepseek-ocr:latest", "qwen3-vl:latest", "granite3.2-vision:latest"]:
                        if alt in model_names:
                            self._model = alt
                            self._available = True
                            logger.info(f"Использую альтернативную модель: {self._model}")
                            break
                    else:
                        logger.warning(f"Vision модель не найдена")
            else:
                logger.warning(f"Ollama недоступна: {response.status_code}")
        except Exception as e:
            logger.warning(f"Ошибка проверки Ollama: {e}")
    
    @property
    def is_available(self) -> bool:
        """Проверить доступность"""
        return self._available
    
    def _encode_image(self, image_path: str) -> str:
        """Кодировать изображение в base64"""
        with open(image_path, "rb") as f:
            return base64.b64encode(f.read()).decode("utf-8")
    
    def _create_prompt(self, language: str) -> str:
        """Создать промпт для OCR"""
        prompts = {
            "russian": """Извлеки весь текст с изображения. 
Сохрани оригинальную структуру и форматирование.
Включи все заголовки, таблицы и списки.
Если есть дата или номер документа - обязательно извлеки.""",
            
            "english": """Extract all text from the image.
Keep original structure and formatting.
Include all headers, tables and lists.
If there is a date or document number - extract it.""",
            
            "auto": """Извлеки весь текст с изображения.
Сохрани оригинальную структуру и форматирование.
Включи все заголовки, таблицы и списки."""
        }
        return prompts.get(language, prompts["auto"])
    
    def extract_from_image(
        self,
        image_path: str,
        language: Optional[str] = None
    ) -> str:
        """
        Распознать текст из изображения используя LLM.
        
        Args:
            image_path: Путь к изображению
            language: Язык (russian, english, auto)
        
        Returns:
            Распознанный текст
        """
        if not self._available:
            logger.warning("LLM OCR недоступен")
            return ""
        
        lang = language or self._language
        
        try:
            # Кодируем изображение
            image_b64 = self._encode_image(image_path)
            
            # Формируем запрос
            prompt = self._create_prompt(lang)
            
            payload = {
                "model": self._model,
                "prompt": prompt,
                "images": [image_b64],
                "stream": False,
                "options": {
                    "temperature": 0.1,
                    "num_ctx": 4096
                }
            }
            
            response = requests.post(
                f"{self._base_url}/api/generate",
                json=payload,
                timeout=self._timeout
            )
            
            if response.status_code == 200:
                result = response.json()
                text = result.get("response", "").strip()
                
                logger.info(
                    f"LLM OCR завершён: {Path(image_path).name}, "
                    f"символов: {len(text)}"
                )
                return text
            else:
                logger.error(f"OCR ошибка: {response.status_code}")
                return ""
                
        except Exception as e:
            logger.error(f"OCR ошибка {image_path}: {e}")
            return ""
    
    def extract_from_pdf(
        self,
        pdf_path: str,
        dpi: int = 150,
        language: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Распознать текст из PDF страниц используя LLM OCR.
        
        Args:
            pdf_path: Путь к PDF файлу
            dpi: Разрешение для рендеринга страниц
            language: Язык
        
        Returns:
            {
                "pages": [{"page": N, "text": "..."}],
                "total_pages": N
            }
        """
        if not self._available:
            logger.warning("LLM OCR недоступен")
            return {"pages": [], "total_pages": 0}
        
        try:
            from pdf2image import convert_from_path
            
            # Рендерим PDF в изображения
            logger.info(f"Рендеринг PDF: {pdf_path}, dpi={dpi}")
            images = convert_from_path(pdf_path, dpi=dpi)
            
            import tempfile
            
            pages = []
            for page_num, image in enumerate(images, 1):
                # Сохраняем во временный файл
                with tempfile.NamedTemporaryFile(
                    suffix=".png", delete=False
                ) as tmp:
                    image.save(tmp.name, "PNG")
                    tmp_path = tmp.name
                
                # OCR страницы
                text = self.extract_from_image(tmp_path, language)
                
                pages.append({
                    "page": page_num,
                    "text": text,
                    "char_count": len(text)
                })
                
                logger.info(f"Страница {page_num}/{len(images)}: {len(text)} символов")
            
            return {
                "pages": pages,
                "total_pages": len(pages)
            }
            
        except Exception as e:
            logger.error(f"OCR PDF ошибка: {e}")
            return {"pages": [], "total_pages": 0}
    
    def batch_extract(
        self,
        image_paths: List[str],
        language: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """
        Пакетная обработка нескольких изображений.
        
        Args:
            image_paths: Список путей к изображениям
            language: Язык
        
        Returns:
            Список результатов
        """
        results = []
        
        for img_path in image_paths:
            text = self.extract_from_image(img_path, language)
            results.append({
                "path": img_path,
                "text": text,
                "success": bool(text)
            })
        
        return results


# Глобальный экземпляр (_lazy инициализация)
_llm_ocr_engine: Optional[OCRLLMEngine] = None


def get_llm_ocr_engine(
    base_url: str = "http://192.168.50.41:11434",
    model: str = "deepseek-ocr:latest",
    language: str = "russian"
) -> OCRLLMEngine:
    """Получить экземпляр LLM OCR движка"""
    global _llm_ocr_engine
    
    if _llm_ocr_engine is None:
        _llm_ocr_engine = OCRLLMEngine(
            base_url=base_url,
            model=model,
            language=language
        )
    
    return _llm_ocr_engine


# Для обратной совместимости с существующим кодом
llm_ocr_engine = get_llm_ocr_engine()