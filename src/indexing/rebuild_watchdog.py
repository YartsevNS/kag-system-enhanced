"""
Rebuild Watchdog — сторож перестроения графа знаний.

Запускается как фоновая asyncio-задача внутри uvicorn (переживает SSH/контейнер-рестарты).
Мониторит прогресс, обнаруживает зависания, автоматически перезапускает.

API (через config_store):
- kg_config.REBUILD_ACTIVE = True/False — флаг активности
- kg_config.REBUILD_STATUS = {running, stalled, completed, stopped}
- kg_config.REBUILD_STATS  = {entities, relations, last_entity_count, last_update}
- kg_config.REBUILD_STOP   = True — сигнал остановки
"""

import asyncio
import time
from loguru import logger

class RebuildWatchdog:
    """Сторож перестроения графа."""
    
    def __init__(self):
        self._task: asyncio.Task | None = None
        self._stall_counter = 0
        self._last_entity_count = 0
        self._run_count = 0  # Сколько раз запускали (для отладки)
    
    # ============================================================
    # Публичный API
    # ============================================================
    
    def start(self):
        """Запустить сторожа (если ещё не запущен)."""
        if self._task and not self._task.done():
            logger.info("Watchdog уже запущен")
            return
        
        self._task = asyncio.create_task(self._watch_loop())
        logger.info("🔍 Watchdog запущен — слежу за перестроением графа")
    
    async def stop(self):
        """Остановить сторожа и перестроение."""
        from src.api.services.config_store import config_store
        config_store.set("kg_config", "rebuild_stop", True)
        config_store.set("kg_config", "rebuild_status", "stopped")
        
        if self._task and not self._task.done():
            self._task.cancel()
        logger.info("⏹ Watchdog остановлен")
    
    # ============================================================
    # Внутренняя логика
    # ============================================================
    
    async def _watch_loop(self):
        """Основной цикл наблюдения."""
        from src.api.services.config_store import config_store
        from src.indexing.knowledge_graph import kg_service
        
        # Сбрасываем флаги
        config_store.set("kg_config", "rebuild_stop", False)
        config_store.set("kg_config", "rebuild_status", "running")
        self._stall_counter = 0
        self._last_entity_count = 0
        self._run_count += 1
        
        while True:
            try:
                await asyncio.sleep(30)  # Проверка каждые 30 секунд
                
                # Проверка флага остановки
                stop_flag = config_store.get("kg_config", "rebuild_stop")
                if stop_flag:
                    logger.info("Watchdog: получен сигнал STOP")
                    config_store.set("kg_config", "rebuild_status", "stopped")
                    break
                
                # Получаем текущую статистику
                try:
                    stats = kg_service.get_stats()
                    entities = stats.get("entities", 0)
                    relations = stats.get("relations", 0)
                    
                    # Сохраняем в config_store для мониторинга через API
                    config_store.set("kg_config", "rebuild_stats", {
                        "entities": entities,
                        "relations": relations,
                        "last_entity_count": self._last_entity_count,
                        "last_update": time.time(),
                        "watchdog_run": self._run_count
                    })
                    
                    # Обнаружение зависания: сущности не растут 5 циклов подряд (2.5 мин)
                    if entities == self._last_entity_count and entities > 0:
                        self._stall_counter += 1
                        if self._stall_counter >= 5:
                            logger.warning(f"Watchdog: перестроение зависло! {entities} сущностей, без изменений {self._stall_counter} циклов")
                            config_store.set("kg_config", "rebuild_status", "stalled")
                            await self._restart_rebuild()
                            self._stall_counter = 0
                    else:
                        self._stall_counter = 0
                    
                    self._last_entity_count = entities
                    
                    # Если все документы обработаны — завершаем
                    total_docs = len(config_store.get_all("documents") or {})
                    if entities > 0 and stats.get("documents", 0) >= total_docs * 0.9:
                        logger.info(f"Watchdog: перестроение завершено — {entities} сущностей, {relations} связей")
                        config_store.set("kg_config", "rebuild_status", "completed")
                        break
                        
                except Exception as e:
                    logger.warning(f"Watchdog: ошибка получения статистики: {e}")
                    continue
                    
            except asyncio.CancelledError:
                logger.info("Watchdog: задача отменена")
                break
            except Exception as e:
                logger.error(f"Watchdog: ошибка в цикле: {e}")
                await asyncio.sleep(60)
    
    async def _restart_rebuild(self):
        """Перезапустить перестроение при зависании."""
        from src.api.services.config_store import config_store
        from src.indexing.entity_extractor import entity_extractor
        from src.indexing.embeddings_service import embeddings_service
        from src.indexing.knowledge_graph import kg_service
        
        logger.info("Watchdog: перезапускаю перестроение...")
        config_store.set("kg_config", "rebuild_status", "restarting")
        
        try:
            await embeddings_service.initialize()
            docs = config_store.get_all("documents") or {}
            
            for did, doc in docs.items():
                # Проверка флага остановки
                if config_store.get("kg_config", "rebuild_stop"):
                    break
                
                if not isinstance(doc, dict) or doc.get("status") != "completed":
                    continue
                
                fn = doc.get("filename", "?")[:50]
                kg_service.create_document_node(did, fn)
                
                chunks = await embeddings_service.get_document_chunks(did)
                if not chunks:
                    continue
                
                for i, ch in enumerate(chunks[:1]):
                    cid = ch.get("chunk_id", f"{did}_chunk_{i}")
                    ctext = ch.get("content", "")
                    cseq = ch.get("metadata", {}).get("chunk_seq", i + 1)
                    kg_service.create_chunk_node(cid, did, ctext, cseq)
                    try:
                        await entity_extractor.extract_and_store(did, cid, ctext, cseq, fn)
                    except Exception as e:
                        logger.debug(f"Watchdog: ошибка {fn}: {e}")
            
            config_store.set("kg_config", "rebuild_status", "running")
            self._stall_counter = 0
            logger.info("Watchdog: перестроение перезапущено")
            
        except Exception as e:
            logger.error(f"Watchdog: ошибка перезапуска: {e}")
            config_store.set("kg_config", "rebuild_status", "error")


# Глобальный экземпляр
rebuild_watchdog = RebuildWatchdog()
