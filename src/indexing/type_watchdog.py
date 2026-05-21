"""
Type Detection Watchdog — сторож определения типов документов.

Проверяет наличие document_type у каждого документа.
Если тип не определён — берёт первые 2 чанка, отправляет LLM и определяет тип.
Результат сохраняется в config_store (metadata документа) и Neo4j.
"""

import asyncio
from loguru import logger

class TypeWatchdog:
    """Сторож определения типов документов."""
    
    def __init__(self):
        self._task: asyncio.Task | None = None
        self._processed = 0
        self._total = 0
    
    def start(self):
        if self._task and not self._task.done():
            logger.info("TypeWatchdog уже запущен")
            return
        from src.api.services.config_store import config_store
        config_store.set("kg_config", "type_watch_status", {"state": "running"})
        self._task = asyncio.create_task(self._run())
        logger.info("🏷️ TypeWatchdog запущен — определяю типы документов")
    
    async def _run(self):
        from src.api.services.config_store import config_store
        from src.indexing.embeddings_service import embeddings_service
        
        try:
            await embeddings_service.initialize()
        except Exception as e:
            logger.warning(f"TypeWatchdog: Qdrant недоступен: {e}")
            config_store.set("kg_config", "type_watch_status", {"state": "error", "error": str(e)})
            return
        
        docs = config_store.get_all("documents") or {}
        self._total = sum(1 for d in docs.values() if isinstance(d, dict) and d.get('status') == 'completed')
        self._processed = 0
        
        config_store.set("kg_config", "type_watch_status", {"state": "running"})
        
        for did, doc in docs.items():
            # Проверка сигнала остановки
            if config_store.get("kg_config", "rebuild_stop"):
                config_store.set("kg_config", "type_watch_status", "stopped")
                break
            
            if not isinstance(doc, dict) or doc.get('status') != 'completed':
                continue
            
            existing_type = doc.get('document_type')
            if existing_type and existing_type not in ('unknown', None, ''):
                self._processed += 1
                continue
            
            fn = doc.get('filename', '?')[:60]
            
            try:
                # Берём первые 2 чанка
                chunks = await embeddings_service.get_document_chunks(did)
                if not chunks:
                    continue
                
                sample_texts = []
                for ch in chunks[:2]:
                    ct = ch.get('content', '')
                    if ct:
                        sample_texts.append(ct[:500])
                
                if not sample_texts:
                    continue
                
                # Определяем тип через LLM
                detected_type = await self._detect_type(sample_texts, fn)
                
                if detected_type:
                    # Сохраняем в config_store
                    doc['document_type'] = detected_type
                    config_store.set("documents", did, doc)
                    
                    # Обновляем Neo4j если есть документ
                    try:
                        from src.indexing.knowledge_graph import kg_service
                        with kg_service.driver.session() as s:
                            s.run(
                                "MATCH (d:Document {id: $did}) SET d.doc_type = $dtype",
                                did=did, dtype=detected_type
                            )
                    except Exception:
                        pass
                    
                    logger.info(f"🏷️ {fn[:30]} -> {detected_type}")
                else:
                    doc['document_type'] = 'unknown'
                    config_store.set("documents", did, doc)
                
                self._processed += 1
                
                # Обновляем прогресс каждые 10 документов
                if self._processed % 10 == 0:
                    config_store.set("kg_config", "type_watch_progress", {
                        "processed": self._processed,
                        "total": self._total
                    })
                
            except Exception as e:
                logger.debug(f"TypeWatchdog: ошибка {fn[:30]}: {e}")
                continue
        
        config_store.set("kg_config", "type_watch_status", {"state": "completed"})
        config_store.set("kg_config", "type_watch_progress", {
            "processed": self._processed,
            "total": self._total
        })
        logger.info(f"🏷️ TypeWatchdog завершён: {self._processed}/{self._total}")
    
    async def _detect_type(self, sample_texts: list, filename: str) -> str | None:
        """Определить тип документа по первым чанкам через LLM."""
        from src.api.services.config_store import config_store
        from src.indexing.entity_extractor import entity_extractor
        
        # Загружаем список типов из БД (авто-пополняемый)
        type_list = config_store.get("kg_config", "doc_types") or {}
        if isinstance(type_list, dict):
            known_types = type_list.get("types", [])
        else:
            known_types = []
        
        # Дефолтный список если пусто
        if not known_types:
            known_types = ['contract','report','invoice','letter','form','certificate',
                          'legal','medical','financial','technical','order','policy',
                          'standard','news','other']
            config_store.set("kg_config", "doc_types", {"types": known_types})
        
        type_list_str = ", ".join(known_types)
        
        cfg = entity_extractor._get_graph_config()
        model = cfg.get("model", "phi4-mini:latest")
        llm_url = cfg.get("url", "http://192.168.50.41:11434")
        provider = cfg.get("provider", "ollama")
        api_key = cfg.get("api_key", "")
        
        prompt = f"""Определи ТОЧНЫЙ тип документа по его содержимому. Верни только одно слово из списка.
Если документ не подходит ни под один тип из списка — предложи НОВЫЙ тип (одно слово на английском).

СПИСОК ИЗВЕСТНЫХ ТИПОВ:
{type_list_str}

Имя файла: {filename}

Содержимое (первые фрагменты):
---FRAGMENT 1---
{sample_texts[0][:600]}
---FRAGMENT 2---
{sample_texts[1][:600] if len(sample_texts) > 1 else '—'}

Верни СТРОГО одно слово (без кавычек, без пояснений):"""
        
        try:
            result = await entity_extractor._call_llm(
                prompt, model, llm_url, 
                chunk_id="type_detect", pass_name="type",
                api_key=api_key, provider=provider
            )
            entities = result.get("entities", [])
            if entities:
                raw = entities[0].get("name", "").strip().lower()
                # Очищаем от мусора
                raw = raw.split()[0] if raw else ""
                if len(raw) < 2 or len(raw) > 30:
                    return 'other'
                
                # Если тип новый — добавляем в список
                if raw not in known_types and raw != 'other':
                    known_types.append(raw)
                    config_store.set("kg_config", "doc_types", {"types": known_types})
                    logger.info(f"🏷️ Новый тип документа: {raw} (добавлен в список)")
                
                return raw if raw in known_types else 'other'
        except Exception as e:
            logger.debug(f"TypeWatchdog: LLM ошибка: {e}")
        
        return None


type_watchdog = TypeWatchdog()
