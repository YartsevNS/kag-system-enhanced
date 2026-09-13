"""
Type Detection Watchdog — сторож определения типов документов.

Проверяет наличие document_type у каждого документа.
Если тип не определён — берёт первые 2 чанка, отправляет LLM и определяет тип.
Результат сохраняется в config_store (metadata документа) и Neo4j.
"""

import asyncio
from loguru import logger


def _set_doc_type_in_graph(document_id: str, doc_type: str) -> None:
    """Проставить тип документа в графе (синхронно, зовётся через to_thread)."""
    from src.indexing.knowledge_graph import kg_service
    with kg_service.driver.session() as s:
        s.run(
            "MATCH (d:Document {id: $did}) SET d.doc_type = $dtype",
            did=document_id, dtype=doc_type,
        )


def is_registrable_doc_type(dtype) -> bool:
    """Стоит ли регистрировать тип, который вернула LLM.

    «unknown», «other», «неизвестно», «-» — это ОТКАЗ модели, а не новый тип
    документа: раньше такой ответ попадал в список типов (в логе «Новый тип:
    unknown») и мусорил в настройках.
    """
    if not dtype or not isinstance(dtype, str):
        return False
    text = dtype.strip()
    if not text or len(text) >= 40:
        return False
    return text.lower() not in ("unknown", "other", "неизвестно", "-", "n/a")


class TypeWatchdog:
    """Сторож определения типов документов."""

    def __init__(self):
        self._task: asyncio.Task | None = None
        self._processed = 0
        self._total = 0

    async def start(self):
        """Запустить сторож.

        async, потому что: (1) запись статуса в БД уводим в поток, (2) create_task
        требует работающего event loop — в отдельном потоке он бы упал.
        """
        if self._task and not self._task.done():
            logger.info("TypeWatchdog уже запущен")
            return
        from src.api.services.config_store import config_store
        await asyncio.to_thread(
            config_store.set, "kg_config", "type_watch_status", {"state": "running"}
        )
        self._task = asyncio.create_task(self._run())
        logger.info("🏷️ TypeWatchdog запущен — определяю типы документов")

    async def _run(self):
        from src.api.services.config_store import config_store
        from src.api.services.document_repository import get_doc_repo
        from src.indexing.embeddings_service import embeddings_service

        try:
            await embeddings_service.initialize()
        except Exception as e:
            logger.warning(f"TypeWatchdog: Qdrant недоступен: {e}")
            await asyncio.to_thread(
                config_store.set, "kg_config", "type_watch_status",
                {"state": "error", "error": str(e)},
            )
            return

        docs = await asyncio.to_thread(get_doc_repo().get_all) or {}
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
            await asyncio.to_thread(
                config_store.set, "kg_config", "type_watch_status", {"state": "completed"}
            )
            return

        logger.info(f"🏷️ TypeWatchdog: {self._total} документов без типа")
        await asyncio.to_thread(
            config_store.set, "kg_config", "type_watch_status", {"state": "running"}
        )

        # База — известные типы
        type_list = await asyncio.to_thread(config_store.get, "kg_config", "doc_types") or {}
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

        BATCH_SIZE = 3

        # Последовательная обработка батчей — без пауз, один за другим
        for i in range(0, len(candidates), BATCH_SIZE):
            if await asyncio.to_thread(config_store.get, "kg_config", "rebuild_stop"):
                break
            batch = candidates[i:i + BATCH_SIZE]
            await self._process_batch(batch, known_types, config_store, embeddings_service)

        await asyncio.to_thread(
            config_store.set, "kg_config", "type_watch_status", {"state": "completed"}
        )
        await asyncio.to_thread(
            config_store.set, "kg_config", "type_watch_progress",
            {"processed": self._processed, "total": self._total},
        )
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
                        texts.append(ct[:600])
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
            # В prompt id обрезается до 8 символов (item['id'][:8]), поэтому
            # LLM возвращает короткий id — матчим по префиксу, а не точному ключу.
            dtype = "unknown"
            for k, v in detected.items():
                if k and did.startswith(k):
                    dtype = v
                    break
            if dtype == "unknown":
                dtype = detected.get(did, "unknown")
            final_type = "other"
            for t in known_types:
                if dtype.lower() in (t["label"].lower(), t["key"].lower()):
                    final_type = t["key"]
                    break

            if final_type == "other" and is_registrable_doc_type(dtype):
                new_key = dtype.lower().replace(' ', '_')[:20]
                known_types.append({"key": new_key, "label": dtype})
                await asyncio.to_thread(
                    config_store.set, "kg_config", "doc_types", {"types": known_types}
                )
                final_type = new_key
                logger.info(f"🏷️ Новый тип: {dtype}")

            from src.api.services.document_repository import get_doc_repo
            doc_data = await asyncio.to_thread(get_doc_repo().get_dict, did) or {}
            if isinstance(doc_data, dict):
                doc_data["document_type"] = final_type
                await asyncio.to_thread(get_doc_repo().upsert, did, doc_data)

            # Qdrant
            try:
                await embeddings_service.update_document_type_payload(did, final_type)
            except Exception:
                pass

            # Neo4j — синхронный драйвер: вызов в потоке (сторож работает
            # в процессе API, блокировать event loop нельзя).
            try:
                await asyncio.to_thread(_set_doc_type_in_graph, did, final_type)
            except Exception:
                pass

            logger.info(f"🏷️ {item['filename'][:30]} -> {final_type}")

        self._processed += len(items)
        if self._processed % 10 == 0 or self._processed == self._total:
            await asyncio.to_thread(
                config_store.set, "kg_config", "type_watch_progress",
                {"processed": self._processed, "total": self._total},
            )

    async def _detect_types_batch(self, items: list, known_types: list) -> dict:
        """Определить типы для пачки документов одним LLM-вызовом (батч: до 5)."""
        type_labels = ", ".join(t["label"] for t in known_types)

        # Компактный промпт. Подробные маркеры типов вынесены в system_prompt
        # настроек («Анализ документов»), т.к. полный prompts/type.txt (8К)
        # + тексты батча делали промпт слишком длинным для flash-модели —
        # она возвращала пустой ответ.
        prompt_lines = [f"Определи тип каждого документа из списка: {type_labels}."]

        prompt_lines.append(f"Документов в батче: {len(items)}.")
        prompt_lines.append('Верни СТРОГО JSON список (без markdown):')
        prompt_lines.append('[{"id":"<короткий id>","type":"<ключ типа латиницей>"}, ...]')
        prompt_lines.append('Ключ типа — латиницей (invoice, contract, report, letter, form, identity, medical, legal, financial, technical, standard, policy, order, news).')
        prompt_lines.append('Если тип не ясен — "other".')
        for item in items:
            prompt_lines.append(f"\n---{item['id'][:8]} ({item['filename']})---")
            for i, t in enumerate(item["texts"]):
                prompt_lines.append(f"[{i+1}] {t}")
        prompt = "\n".join(prompt_lines)

        cfg = self._get_config()
        model = cfg.get("model", "phi4-mini:latest")
        from src.config import get_settings
        llm_url = cfg.get("url", get_settings().OLLAMA_BASE_URL)
        provider = cfg.get("provider", "ollama")
        api_key = cfg.get("api_key", "")

        from src.indexing.entity_extractor import entity_extractor
        result = await entity_extractor._call_llm(
            prompt, model, llm_url,
            chunk_id="type_batch", pass_name="type",
            api_key=api_key, provider=provider,
            system_prompt=cfg.get("system_prompt", "").replace("{type_labels}", type_labels)
        )

        entities = result.get("entities", [])
        detected = {}
        for e in entities:
            eid = e.get("id", "")
            et = e.get("name", "").strip()
            if eid and et:
                detected[eid] = et

        # Fallback: парсим JSON-список из сырого ответа
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

    def _load_type_rules(self) -> str:
        """Загрузить подробный промпт типизации из prompts/type.txt."""
        for path in ("/app/prompts/type.txt", "prompts/type.txt"):
            try:
                from pathlib import Path
                p = Path(path)
                if p.exists():
                    return p.read_text(encoding="utf-8")
            except Exception as e:
                logger.debug(f"Не удалось загрузить {path}: {e}")
        return ""

    def _get_config(self):
        from src.api.services.provider_service import provider_service
        # Типизация — это «Анализ документов» (function_map:doc_analysis),
        # а не graph (тот — для извлечения сущностей). system_prompt берётся
        # из поля «промпт» в настройках админки.
        cfg = provider_service.get_function_llm_config("doc_analysis")
        if cfg and cfg.get("model"):
            return cfg
        from src.indexing.entity_extractor import entity_extractor
        return entity_extractor._get_graph_config()


type_watchdog = TypeWatchdog()
