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
        candidates = []
        for did, doc in docs.items():
            if not isinstance(doc, dict) or doc.get('status') != 'completed':
                continue
            existing = doc.get('document_type')
            if existing and existing not in ('unknown', None, ''):
                continue
            candidates.append((did, doc))

        self._total = len(candidates)
        self._processed = 0

        if not candidates:
            config_store.set("kg_config", "type_watch_status", {"state": "completed"})
            return

        logger.info(f"🏷️ TypeWatchdog: {self._total} документов без типа")
        config_store.set("kg_config", "type_watch_status", {"state": "running"})

        # База — известные типы
        type_list = config_store.get("kg_config", "doc_types") or {}
        if isinstance(type_list, dict):
            known_types = type_list.get("types", [])
        else:
            known_types = []
        if not known_types:
            known_types = [
                {"key": "contract", "label": "Договор"},
                {"key": "report", "label": "Отчёт"},
                {"key": "invoice", "label": "Счёт"},
                {"key": "letter", "label": "Письмо"},
                {"key": "form", "label": "Форма"},
                {"key": "certificate", "label": "Удостоверение"},
                {"key": "legal", "label": "Юридический"},
                {"key": "medical", "label": "Медицинский"},
                {"key": "financial", "label": "Финансовый"},
                {"key": "technical", "label": "Технический"},
                {"key": "standard", "label": "Стандарт (ГОСТ)"},
                {"key": "policy", "label": "Политика/Регламент"},
                {"key": "order", "label": "Приказ"},
                {"key": "news", "label": "Новость"},
                {"key": "other", "label": "Прочее"},
            ]
            config_store.set("kg_config", "doc_types", {"types": known_types})

        BATCH_SIZE = 5

        # Последовательная обработка батчей — без пауз, один за другим
        for i in range(0, len(candidates), BATCH_SIZE):
            if config_store.get("kg_config", "rebuild_stop"):
                break
            batch = candidates[i:i + BATCH_SIZE]
            await self._process_batch(batch, known_types, config_store, embeddings_service)

        config_store.set("kg_config", "type_watch_status", {"state": "completed"})
        config_store.set("kg_config", "type_watch_progress", {"processed": self._processed, "total": self._total})
        logger.info(f"🏷️ TypeWatchdog завершён: {self._processed}/{self._total}")

    async def _process_batch(self, batch, known_types, config_store, embeddings_service):
        """Обработать один батч документов."""
        # Собираем тексты и id
        items = []
        for did, doc in batch:
            try:
                chunks = await embeddings_service.get_document_chunks(did)
                texts = []
                for ch in (chunks or [])[:2]:
                    ct = ch.get('content', '')
                    if ct:
                        texts.append(ct[:500])
                if texts:
                    items.append({"id": did, "filename": doc.get('filename', '?')[:60], "texts": texts})
            except Exception:
                continue

        if not items:
            self._processed += len(batch)
            return

        # Определяем типы через LLM (одним вызовом на батч)
        try:
            detected = await self._detect_types_batch(items, known_types)
        except Exception as e:
            logger.debug(f"TypeWatchdog batch error: {e}")
            self._processed += len(items)
            return

        # Сохраняем результаты
        for item in items:
            did = item["id"]
            dtype = detected.get(did, "unknown")
            final_type = "other"
            for t in known_types:
                if dtype.lower() in (t["label"].lower(), t["key"].lower()):
                    final_type = t["key"]
                    break

            if final_type == "other" and dtype and len(dtype) < 40:
                new_key = dtype.lower().replace(' ', '_')[:20]
                known_types.append({"key": new_key, "label": dtype})
                config_store.set("kg_config", "doc_types", {"types": known_types})
                final_type = new_key
                logger.info(f"🏷️ Новый тип: {dtype}")

            doc_data = config_store.get("documents", did) or {}
            if isinstance(doc_data, dict):
                doc_data["document_type"] = final_type
                config_store.set("documents", did, doc_data)

            # Qdrant
            try:
                await embeddings_service.update_document_type_payload(did, final_type)
            except Exception:
                pass

            # Neo4j
            try:
                from src.indexing.knowledge_graph import kg_service
                with kg_service.driver.session() as s:
                    s.run("MATCH (d:Document {id: $did}) SET d.doc_type = $dtype", did=did, dtype=final_type)
            except Exception:
                pass

            logger.info(f"🏷️ {item['filename'][:30]} -> {final_type}")

        self._processed += len(items)
        if self._processed % 10 == 0 or self._processed == self._total:
            config_store.set("kg_config", "type_watch_progress", {"processed": self._processed, "total": self._total})

    async def _detect_types_batch(self, items: list, known_types: list) -> dict:
        """Определить типы для пачки документов одним LLM-вызовом (батч: до 5)."""
        type_labels = ", ".join(t["label"] for t in known_types)

        prompt_lines = [f"Определи типы {len(items)} документов из списка: {type_labels}"]
        prompt_lines.append("Верни JSON список: [{\"id\":\"doc_id\",\"type\":\"тип\"}]")
        for item in items:
            prompt_lines.append(f"\n---{item['id'][:8]} ({item['filename']})---")
            for i, t in enumerate(item["texts"]):
                prompt_lines.append(f"[{i+1}] {t}")
        prompt = "\n".join(prompt_lines)

        cfg = self._get_config()
        model = cfg.get("model", "phi4-mini:latest")
        llm_url = cfg.get("url", "http://192.168.50.41:11434")
        provider = cfg.get("provider", "ollama")
        api_key = cfg.get("api_key", "")

        from src.indexing.entity_extractor import entity_extractor
        result = await entity_extractor._call_llm(
            prompt, model, llm_url,
            chunk_id="type_batch", pass_name="type",
            api_key=api_key, provider=provider
        )

        entities = result.get("entities", [])
        detected = {}
        for e in entities:
            eid = e.get("id", "")
            et = e.get("name", "").strip()
            if eid and et:
                detected[eid] = et

        # Fallback: парсим JSON из ответа
        if not detected:
            raw = result.get("raw", "")
            import json, re
            m = re.search(r'\[.*?\]', raw, re.DOTALL)
            if m:
                try:
                    parsed = json.loads(m.group())
                    for p in parsed:
                        if isinstance(p, dict) and "id" in p:
                            detected[p["id"]] = p.get("type", "other")
                except Exception:
                    pass

        return detected

    def _get_config(self):
        from src.indexing.entity_extractor import entity_extractor
        return entity_extractor._get_graph_config()


type_watchdog = TypeWatchdog()
