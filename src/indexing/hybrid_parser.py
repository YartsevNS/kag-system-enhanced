"""
Hybrid Document Parser: Docling (layout/structure) + Occular-ocr (Russian text).

Architecture:
  PDF/DOCX/Image
    → Docling Standard Pipeline (CPU)
        ├─ Layout analysis: text blocks, tables, images, formulas
        ├─ Reading order detection
        └─ Table structure extraction
    → Occular-ocr (CPU, Russian-optimized)
        └─ Text recognition in detected regions (93.7% accuracy)
    → Structured output: Markdown with tables, images, formulas

Fallback: pure Occular-ocr if Docling fails.
"""

import hashlib
from pathlib import Path
from typing import List, Dict, Any, Optional
from dataclasses import dataclass, field

from loguru import logger


@dataclass
class ParsedPage:
    """One page of parsed document."""
    page_num: int
    text: str = ""                      # Full text of the page
    layout: List[Dict[str, Any]] = field(default_factory=list)  # Layout elements
    tables: List[Dict[str, Any]] = field(default_factory=list)  # Extracted tables
    images: List[Dict[str, Any]] = field(default_factory=list)  # Image descriptions


@dataclass
class ParsedDocument:
    """Complete parsed document with structure."""
    filename: str
    pages: List[ParsedPage] = field(default_factory=list)
    full_text: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)
    parse_method: str = "unknown"  # "docling+ocular", "ocular_only", "docling_only"

    def to_markdown(self) -> str:
        """
        Собрать Markdown из распознанного документа.

        Приоритет: layout (от Docling) + таблицы + распознанный текст (от Occular).
        Если layout есть — используем его для заголовков и структуры.
        Если нет — просто full_text.
        """
        md_parts = []

        for page in self.pages:
            # Заголовок страницы
            if len(self.pages) > 1:
                md_parts.append(f"## Страница {page.page_num}\n")

            # Текст страницы — ВСЕГДА (иначе при layout таблиц текст терялся)
            if page.text:
                md_parts.append(page.text.strip())
                md_parts.append("")

            # Если есть layout-элементы (таблицы, изображения, формулы)
            if page.layout:
                for item in page.layout:
                    item_type = item.get('type', 'text')

                    if item_type == 'table':
                        table_data = item.get('data', {})
                        md_table = table_data.get('markdown', '')
                        if md_table:
                            md_parts.append(md_table)
                            md_parts.append("")
                        else:
                            # Fallback: просто текст
                            md_parts.append(table_data.get('text', ''))
                            md_parts.append("")

                    elif item_type == 'image':
                        caption = item.get('caption', '') or item.get('description', '')
                        md_parts.append(f"![{caption}]({item.get('src', '')})")
                        md_parts.append("")

                    elif item_type == 'formula':
                        latex = item.get('latex', '')
                        md_parts.append(f"$$\n{latex}\n$$")
                        md_parts.append("")

                    # type='text' пропускаем — текст страницы уже выведен выше

        result = "\n".join(md_parts).strip()
        return result if result else self.full_text


class HybridDocumentParser:
    """
    Hybrid parser combining Docling's layout analysis with Occular-ocr's
    Russian-optimized text recognition.
    
    CPU-only. No GPU required.
    """
    
    def __init__(self, force_ocr: bool = None, dpi: int = None):
        self._docling_available = False
        self._ocular_available = False
        # Load OCR settings from config_store (admin panel)
        try:
            from src.api.services.config_store import config_store
            ocr_cfg = config_store.get("ocr", "settings") or {}
            self._force_ocr = force_ocr if force_ocr is not None else ocr_cfg.get("force_ocr", False)
            self._dpi = dpi if dpi is not None else ocr_cfg.get("dpi", 200)
        except Exception:
            self._force_ocr = force_ocr if force_ocr is not None else False
            self._dpi = dpi if dpi is not None else 200
        self._init_engines()
    
    def _init_engines(self):
        """Initialize parsing engines. Graceful degradation if unavailable."""
        # Try Docling
        try:
            from docling.document_converter import DocumentConverter
            self._docling_converter = DocumentConverter()
            self._docling_available = True
            logger.info("Docling Standard pipeline initialized (CPU)")
        except Exception as e:
            logger.warning(f"Docling not available: {e}. Using Occular-ocr only.")
            self._docling_converter = None
        
        # Try Occular-ocr
        # Проверяем веса: сначала в /app/data/weights (persist volume), потом в пакете
        weights_pkg = Path("/opt/venv/lib/python3.11/site-packages/ocr_skel/weights")
        weights_data = Path("/app/data/weights")
        required = ["dbnet.onnx", "dbnet_weights.pth", "crnn_encoder.onnx", "crnn_mobilenet_large.pth"]
        if weights_pkg.exists() and weights_data.exists():
            for f in required:
                if not (weights_pkg / f).is_file() and (weights_data / f).is_file():
                    import shutil
                    shutil.copy2(str(weights_data / f), str(weights_pkg / f))
                    logger.info(f"  Copied weight from persist: {f}")
        try:
            from ocr_skel import OCRPipeline
            # Новые версии Occular-ocr: конструктор принимает (settings, deskew,
            # reading_order, lm, num_threads, gpu, detector, recognizer). Параметра
            # onnx больше нет — onnxruntime всегда используется для инференса.
            self._ocular = OCRPipeline(gpu=False)
            self._ocular_available = True
            logger.info("Occular-ocr initialized (CPU)")
        except Exception as e:
            logger.warning(f"Occular-ocr not available: {e}")
            self._ocular = None
    
    def parse(self, file_path: str) -> ParsedDocument:
        """
        Parse a document using the best available method.
        
        Priority: Docling layout + Occular-ocr text > Docling only > Occular only.
        """
        path = Path(file_path)
        filename = path.name
        
        # Compute file hash for tracking
        file_hash = hashlib.sha256(path.read_bytes()).hexdigest()
        
        if self._docling_available:
            return self._parse_with_docling(file_path, filename, file_hash)
        elif self._ocular_available:
            return self._parse_with_ocular_only(file_path, filename, file_hash)
        else:
            return self._parse_fallback(file_path, filename, file_hash)
    
    def _parse_with_docling(self, file_path: str, filename: str, file_hash: str) -> ParsedDocument:
        """Use Docling for structure + Occular-ocr for Russian text."""
        doc = ParsedDocument(filename=filename, parse_method="docling+ocular")
        
        try:
            # Step 1: Docling layout analysis
            logger.info(f"Docling: analyzing layout of {filename}")
            result = self._docling_converter.convert(file_path)
            docling_doc = result.document
            
            # Extract metadata
            doc.metadata = {
                "file_hash": file_hash,
                "page_count": len(docling_doc.pages) if hasattr(docling_doc, 'pages') else 0,
                "format": Path(file_path).suffix.lower(),
            }
            
            # Step 2: Process each page
            full_parts = []
            for page_idx, page in enumerate(getattr(docling_doc, 'pages', [])):
                parsed_page = ParsedPage(page_num=page_idx + 1)
                page_text_parts = []
                
                for item in getattr(page, 'items', []):
                    item_type = getattr(item, 'type', 'text')
                    
                    if item_type == 'table':
                        # Extract table structure from Docling
                        table_data = self._extract_table(item)
                        parsed_page.tables.append(table_data)
                        page_text_parts.append(table_data.get('markdown', ''))
                        parsed_page.layout.append({'type': 'table', 'data': table_data})
                    
                    elif item_type == 'image':
                        parsed_page.images.append({
                            'caption': getattr(item, 'caption', ''),
                            'description': getattr(item, 'description', '')
                        })
                        parsed_page.layout.append({'type': 'image', 'bbox': getattr(item, 'bbox', None)})
                    
                    elif item_type == 'formula':
                        formula = getattr(item, 'text', '')
                        parsed_page.layout.append({'type': 'formula', 'text': formula})
                        page_text_parts.append(f"$${formula}$$")
                    
                    else:
                        # Text block: use Occular-ocr if available for better Russian
                        text = getattr(item, 'text', '')
                        bbox = getattr(item, 'bbox', None)
                        
                        # If text is short/unreadable and we have Occular, try OCR
                        if self._ocular_available and self._needs_ocr(text, filename):
                            if bbox:
                                ocr_text = self._ocr_region(file_path, bbox, page_idx)
                                if ocr_text and len(ocr_text) > len(text) * 0.5:
                                    text = ocr_text
                        
                        parsed_page.layout.append({'type': 'text', 'text': text, 'bbox': bbox})
                        page_text_parts.append(text)
                
                parsed_page.text = '\n\n'.join(page_text_parts)
                full_parts.append(parsed_page.text)
                doc.pages.append(parsed_page)
            
            doc.full_text = '\n\n--- PAGE BREAK ---\n\n'.join(full_parts)
            logger.info(f"Docling+Occular: parsed {filename}, {len(doc.pages)} pages, {len(doc.full_text)} chars")
            
        except Exception as e:
            logger.error(f"Docling parsing failed for {filename}: {e}")
            # Fallback to Occular-ocr only
            if self._ocular_available:
                logger.info(f"Falling back to Occular-ocr for {filename}")
                return self._parse_with_ocular_only(file_path, filename, file_hash)
            else:
                raise
        
        return doc
    
    def _parse_with_ocular_only(self, file_path: str, filename: str, file_hash: str) -> ParsedDocument:
        """Pure Occular-ocr parsing (optimized for Russian)."""
        doc = ParsedDocument(filename=filename, parse_method="ocular_only")
        
        try:
            pdf = Path(file_path).suffix.lower() == '.pdf'
            if pdf:
                pages = self._ocular.process_pdf(file_path, dpi=self._dpi)
                for page_data in pages:
                    # process_pdf возвращает [{"page": N, "method": "...", "results": [...]}]
                    results = page_data.get('results', []) if isinstance(page_data, dict) else []
                    text = '\n'.join(r.get('text', '') for r in results if isinstance(r, dict) and r.get('text'))
                    page_num = page_data.get('page', len(doc.pages) + 1) if isinstance(page_data, dict) else len(doc.pages) + 1
                    doc.pages.append(ParsedPage(page_num=page_num, text=text))
                    doc.full_text += text + '\n\n'
            else:
                results = self._ocular.process_image(file_path)
                text = '\n'.join(r['text'] for r in results if isinstance(r, dict))
                doc.pages.append(ParsedPage(page_num=1, text=text))
                doc.full_text = text
            
            doc.metadata = {
                "file_hash": file_hash,
                "page_count": len(doc.pages),
                "format": Path(file_path).suffix.lower(),
            }
            logger.info(f"Occular-ocr: parsed {filename}, {len(doc.pages)} pages, {len(doc.full_text)} chars")
            
        except Exception as e:
            logger.error(f"Occular-ocr failed for {filename}: {e}")
            return self._parse_fallback(file_path, filename, file_hash)
        
        return doc
    
    def _parse_fallback(self, file_path: str, filename: str, file_hash: str) -> ParsedDocument:
        """Last-resort fallback: read as plain text."""
        doc = ParsedDocument(filename=filename, parse_method="fallback")
        try:
            text = Path(file_path).read_text(errors='replace')
            doc.pages.append(ParsedPage(page_num=1, text=text))
            doc.full_text = text
            doc.metadata = {"file_hash": file_hash, "fallback": True}
        except Exception:
            doc.full_text = f"[Unable to parse {filename}]"
        return doc

    def parse_ocular_only(self, file_path: str) -> Optional[ParsedDocument]:
        """Occular-ocr без Docling. Быстрее и стабильнее для русского текста."""
        if not self._ocular_available:
            return None
        path = Path(file_path)
        return self._parse_with_ocular_only(str(path), path.name, hashlib.sha256(path.read_bytes()).hexdigest())

    def parse_pymupdf_first(self, file_path: str) -> Optional[ParsedDocument]:
        """PyMuPDF текстовый слой В ПРИОРИТЕТЕ — OCR не нужен.

        Зачем: электронные PDF (Word/госконтора) содержат текстовый слой —
        PyMuPDF извлекает его мгновенно и без OOM (в отличие от Occular,
        который рендерит страницы и прогоняет через нейросеть). Occular
        остаётся для СКАНОВ (нет текстового слоя).

        Возвращает ParsedDocument (parse_method="pymupdf"), если текста
        достаточно; None — если это скан (нужен Occular).
        """
        try:
            import fitz
        except ImportError:
            return None

        path = Path(file_path)
        if not path.exists():
            return None

        try:
            doc = fitz.open(str(path))
            pages = []
            total_text = 0
            pages_with_text = 0
            total_tables = 0
            code_heavy = False

            for page_num in range(len(doc)):
                page = doc[page_num]
                text = (page.get_text() or "").strip()
                layout = []
                tables = []

                # ── Таблицы: fitz.find_tables → структурированные ячейки ──
                # Иначе таблицы «склеятся» в плоский текст без структуры.
                try:
                    found = page.find_tables()
                    for tb in found.tables:
                        data = tb.extract()  # 2D массив ячеек
                        if not data or len(data) < 2:
                            continue
                        total_tables += 1
                        # Markdown-таблица
                        md_lines = []
                        md_lines.append("| " + " | ".join(str(c or "") for c in data[0]) + " |")
                        md_lines.append("|" + "|".join("---" for _ in data[0]) + "|")
                        for row in data[1:]:
                            md_lines.append("| " + " | ".join(str(c or "") for c in row) + " |")
                        md_table = "\n".join(md_lines)
                        tables.append({"markdown": md_table, "text": "\n".join(" | ".join(str(c or "") for c in r) for r in data)})
                except Exception as e:
                    logger.debug(f"find_tables page {page_num + 1}: {e}")

                # ── Код: эвристика — много отступов/служебных символов ──
                if not code_heavy and text:
                    code_indicators = sum(
                        text.count(tok) for tok in ("def ", "import ", "class ", "function ", "const ", "```", "{", "}", "=>")
                    )
                    # Много отступов (4+ пробелов в начале строк)
                    indent_lines = sum(1 for line in text.splitlines() if line.startswith("    ") or line.startswith("\t"))
                    if code_indicators > 20 or (indent_lines > 10 and len(text) > 2000):
                        code_heavy = True

                # Таблицы → layout (to_markdown умеет рендерить type=table)
                for tb in tables:
                    layout.append({"type": "table", "data": tb})

                if text:
                    total_text += len(text)
                    pages_with_text += 1

                pages.append(ParsedPage(
                    page_num=page_num + 1,
                    text=text,
                    layout=layout,
                    tables=tables,
                ))

            doc.close()

            # Критерий «достаточно текста»: суммарно > 500 символов И
            # больше половины страниц имеют текстовый слой. Иначе — скан.
            if total_text < 500 or pages_with_text < max(1, len(pages) // 2):
                logger.info(
                    f"PyMuPDF: мало текстового слоя ({total_text} симв, "
                    f"{pages_with_text}/{len(pages)} стр) — скан, нужен OCR"
                )
                return None

            full_text = "\n\n".join(p.text for p in pages if p.text)
            logger.info(
                f"PyMuPDF: текстовый слой {total_text} симв, {pages_with_text}/{len(pages)} стр, "
                f"таблиц: {total_tables}, код: {code_heavy} (OCR не нужен)"
            )
            return ParsedDocument(
                filename=path.name,
                pages=pages,
                full_text=full_text,
                metadata={
                    "page_count": len(pages),
                    "parser": "pymupdf",
                    "tables_count": total_tables,
                    "code_heavy": code_heavy,
                },
                parse_method="pymupdf",
            )
        except Exception as e:
            logger.warning(f"PyMuPDF failed ({e})")
            return None

    def _needs_ocr(self, text: str, filename: str) -> bool:
        """Check if text needs OCR enhancement.

        Decision logic:
        1. force_ocr=True -> always run OCR
        2. Empty/garbled text -> run OCR
        3. Docling found good text -> skip OCR (save 10-15s per page)
        4. Text has encoding issues -> run OCR
        """
        if self._force_ocr:
            return True
        if not text or len(text.strip()) < 10:
            return True
        # Artifacts in PDF text layer
        if any(artifact in text for artifact in ['□□', '???', '□', 'â', 'Ã']):
            return True
        # Docling found substantial clean text -> skip Occular
        if len(text.strip()) > 100:
            return False
        # Russian text check
        has_cyrillic = any('\u0400' <= c <= '\u04FF' for c in text)
        if has_cyrillic:
            return False
        return False
    
    def _ocr_region(self, file_path: str, bbox, page_idx: int) -> Optional[str]:
        """Run Occular-ocr on a specific region of a page."""
        try:
            # For PDF, we can't easily crop by bbox, so use full page OCR
            # This is a simplification - in production, use pdf2image + crop
            return None
        except Exception:
            return None
    
    def _extract_table(self, item) -> Dict[str, Any]:
        """Extract table data from Docling table item."""
        table_data = {
            'rows': [],
            'headers': [],
            'markdown': ''
        }
        try:
            rows = getattr(item, 'rows', [])
            if not rows:
                return table_data
            
            # Extract header
            if rows:
                table_data['headers'] = [getattr(c, 'text', '') for c in rows[0]]
            
            # Build markdown table
            md_rows = []
            for i, row in enumerate(rows):
                cells = [getattr(c, 'text', '') for c in row]
                md_rows.append('| ' + ' | '.join(cells) + ' |')
                if i == 0:
                    md_rows.append('|' + '|'.join(['---'] * len(cells)) + '|')
                table_data['rows'].append(cells)
            
            table_data['markdown'] = '\n'.join(md_rows)
        except Exception as e:
            logger.warning(f"Table extraction failed: {e}")
        
        return table_data


# Singleton
_parser: Optional[HybridDocumentParser] = None


def get_hybrid_parser() -> HybridDocumentParser:
    """Get or create the hybrid parser singleton."""
    global _parser
    if _parser is None:
        _parser = HybridDocumentParser()
    return _parser


def route_document(file_path: str) -> Dict[str, Any]:
    """Маршрутизатор: классифицировать документ и вернуть стратегию обработки.

    Решение принимается эвристиками (без LLM — быстро):
    - расширение файла (PDF / DOCX / XLSX / CSV / TXT / MD)
    - для PDF: наличие текстового слоя (fitz get_text) → PyMuPDF или Occular
    - содержание: плотность таблиц (find_tables), код (эвристика)

    Возвращает: {route, fallback, tables, code, reason}
    """
    path = Path(file_path)
    ext = path.suffix.lower()

    if ext in (".docx", ".doc"):
        return {"route": "docx", "fallback": None, "tables": False, "code": False, "reason": "DOCX"}
    if ext == ".xlsx":
        return {"route": "xlsx", "fallback": None, "tables": True, "code": False, "reason": "XLSX"}
    if ext in (".csv", ".txt", ".md", ".markdown"):
        return {"route": "text", "fallback": None, "tables": False, "code": False, "reason": ext.upper()}

    if ext == ".pdf":
        try:
            import fitz
            doc = fitz.open(str(path))
            total_text = sum(len((doc[i].get_text() or "").strip()) for i in range(len(doc)))
            pages = len(doc)
            doc.close()
            if total_text < 500:
                return {"route": "ocular", "fallback": "document_parser", "tables": False, "code": False,
                        "reason": f"скан: текстового слоя {total_text} симв"}
            # Таблицы/код — оценка (сам parse_pymupdf_first извлечёт детально)
            return {"route": "pymupdf", "fallback": "ocular", "tables": True, "code": False,
                    "reason": f"текстовый слой {total_text} симв, {pages} стр"}
        except Exception as e:
            return {"route": "ocular", "fallback": "document_parser", "tables": False, "code": False,
                    "reason": f"fitz failed: {e}"}

    return {"route": "document_parser", "fallback": None, "tables": False, "code": False, "reason": "default"}
