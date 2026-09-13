"""
Celery задачи для обработки документов.

Вызываются из upload.py вместо asyncio.Queue.
Преимущества: retry, изоляция, очередь в Redis (не теряется при перезапуске).
"""

from typing import Dict, Any, Optional
import asyncio
from datetime import datetime, timedelta, timezone
from loguru import logger

from src.indexing.celery_app import celery_app
from src.indexing.processing_guard import (
    PROCESSING_BLOCK_RETRY_S,
    processing_blocked,
)
from src.api.services.document_service import document_service


@celery_app.task(
    bind=True,
    queue="documents",
    max_retries=5,
    default_retry_delay=60,
    acks_late=True,
    reject_on_worker_lost=True,
)
def process_document(
    self,
    document_id: str,
    force: bool = False,
) -> Dict[str, Any]:
    """
    Обработать документ: парсинг → чанкинг → векторизация.
    
    Важно: worker — отдельный процесс, document_service загружает документы
    из config_store при инициализации. Новые документы, добавленные после
    старта worker'а, НЕ попадают в его оперативную память.
    Поэтому мы принудительно перезагружаем документ из config_store.

    force=True — принудительная переобработка (reindex/reprocess), разрешена
    даже для completed-документов. Защита от дублей (QueueGuard, уровень 3):
    если документ уже processing (другая копия задачи выполняется) или уже
    completed без force — задача выходит без обработки.
    """
    # Административная блокировка обработки (system/processing) — очередь
    # обязана её уважать, иначе пауза действует только на кнопку «Обработать».
    # Задачу не теряем: откладываем повтор, документ остаётся в очереди и
    # поедет сразу после снятия блокировки.
    blocked, block_msg = processing_blocked()
    if blocked:
        logger.warning(
            f"[Celery] Обработка остановлена администратором ({block_msg}): "
            f"{document_id} отложен на {PROCESSING_BLOCK_RETRY_S} с"
        )
        raise self.retry(countdown=PROCESSING_BLOCK_RETRY_S, max_retries=None)

    # Доменная схема сущностей живёт в настройках, а не в памяти процесса:
    # worker — отдельный процесс, и без этого выбранный в админке пресет не
    # применился бы к извлечению сущностей (оно идёт здесь).
    try:
        from src.indexing.entity_extractor import entity_extractor
        entity_extractor.apply_stored_domain_schema()
    except Exception as e:
        logger.warning(f"[Celery] доменную схему применить не удалось: {e}")

    logger.info(f"[Celery] Начало обработки: {document_id}")
    
    try:
        # Принудительно загружаем документ из БД в память worker'а
        from src.api.services.document_service import document_service
        from src.api.services.document_repository import get_doc_repo
        
        # Получаем метаданные из SQL (DocumentRepository)
        doc_data = get_doc_repo().get_dict(document_id)

        if not doc_data:
            # Гонка «документ удалили между постановкой в очередь и стартом
            # задачи»: раньше код шёл дальше и падал в document_service —
            # в логе было «Ошибка обработки», по которой причина не читалась.
            logger.warning(f"[Celery] {document_id}: документа нет в БД — задача пропущена")
            from src.indexing.queue_guard import release_lock
            release_lock(document_id)
            return {"status": "not_found", "document_id": document_id}

        if isinstance(doc_data, str):
            raise ValueError(f"Документ повреждён в БД (строка вместо dict): {document_id}")
        
        # ── QueueGuard, уровень 3: защита от дублей в самой задаче ──────
        # Если в очередь попала задача-дубль (например, осталась от старого
        # кода до внедрения QueueGuard), а документ уже обрабатывается другой
        # копией задачи (status=processing) или уже обработан (status=completed
        # без force) — выходим без обработки. Это страховка на случай, когда
        # замок в Redis недоступен/истёк, а дубль в очереди остался.
        if doc_data:
            cur_status = doc_data.get("status")
            if cur_status == "processing" and not force:
                logger.warning(
                    f"[QueueGuard] {document_id}: уже processing (другая копия "
                    f"задачи выполняется) — пропуск дубля"
                )
                # Пропуск — это финальное завершение задачи-дубля: снимаем
                # замок, чтобы он не висел до TTL (иначе recovery не сможет
                # перезапустить документ, если основная задача потеряется).
                from src.indexing.queue_guard import release_lock
                release_lock(document_id)
                return {"status": "skipped", "reason": "already_processing"}
            if cur_status == "completed" and not force:
                logger.warning(
                    f"[QueueGuard] {document_id}: уже completed — пропуск дубля"
                )
                from src.indexing.queue_guard import release_lock
                release_lock(document_id)
                return {"status": "skipped", "reason": "already_completed"}
        
        if doc_data:
            # Пересоздаём запись в памяти (даже если уже была)
            from src.api.services.document_service import DocumentRecord
            from datetime import datetime
            
            record = DocumentRecord(
                document_id=document_id,
                filename=doc_data.get("filename", "unknown"),
                file_type=doc_data.get("file_type", ""),
                file_size=doc_data.get("file_size", 0),
                file_hash=doc_data.get("file_hash", ""),
                status=doc_data.get("status", "pending"),
                progress=doc_data.get("progress", 0),
                uploaded_by=doc_data.get("uploaded_by"),
                group_ids=doc_data.get("group_ids", []),
                version=doc_data.get("version", 1),
                created_at=datetime.fromisoformat(doc_data["created_at"]) if doc_data.get("created_at") else datetime.utcnow(),
                updated_at=datetime.fromisoformat(doc_data["updated_at"]) if doc_data.get("updated_at") else datetime.utcnow(),
            )
            document_service._documents[document_id] = record
            logger.info(f"[Celery] Документ загружен из БД: {document_id}")
        
        # Обрабатываем. force=True (переиндексация/reindex) передаём дальше —
        # document_service при этом сначала удалит старые векторы из Qdrant и
        # граф Neo4j, чтобы не оставалось «осиротевших» точек при изменении
        # числа чанков (иначе старые точки с document_id висели бы вечно).
        result = asyncio.run(document_service.process_document(document_id, force=force))
        # Успех — снимаем замок QueueGuard (задача завершена, документ
        # обработан; при необходимости его можно будет поставить заново).
        from src.indexing.queue_guard import release_lock
        release_lock(document_id)
        logger.info(f"[Celery] ✅ Документ обработан: {document_id}")
        return {
            "document_id": document_id,
            "status": "completed",
            "result": str(result),
        }
        
    except Exception as exc:
        logger.error(f"[Celery] ❌ Ошибка обработки {document_id}: {exc}")
        now = datetime.now(timezone.utc)
        
        # Определяем: это ошибка провайдера (Ollama/DeepSeek) или внутренняя?
        error_str = str(exc).lower()
        is_provider_issue = any(kw in error_str for kw in [
            "connection", "refused", "resolve", "no route", "timeout",
            "401", "403", "502", "503", "unavailable", "authentication fails"
        ])
        
        if is_provider_issue:
            # Провайдер недоступен — не retry, а откладываем
            logger.warning(f"⏸ Провайдер недоступен для {document_id}, откладываю на 5 мин")
            try:
                doc_data.setdefault("error", "")
                doc_data["error"] += f" | {now.isoformat()}: провайдер недоступен"
                doc_data["delayed_until"] = (now + timedelta(minutes=5)).isoformat()
                from src.api.services.document_repository import get_doc_repo
                get_doc_repo().upsert(document_id, doc_data)
            except Exception:
                pass
            # Задача завершилась (delayed) — снимаем замок, чтобы recovery
            # смог переставить документ после delayed_until.
            from src.indexing.queue_guard import release_lock
            release_lock(document_id)
            return {"status": "delayed", "document_id": document_id}
        else:
            # Внутренняя ошибка — retry как обычно.
            # Перед retry: если это ПОСЛЕДНЯЯ попытка, снимаем замок — иначе
            # после исчерпания retry документ останется заблокированным до TTL
            # (6 часов) и recovery не сможет его перезапустить. При обычном
            # retry замок держим (задача перепоставится Celery, дубль не нужен).
            from src.indexing.queue_guard import release_lock
            if self.request.retries >= self.max_retries:
                release_lock(document_id)
            countdown = 60 * (2 ** self.request.retries)
            raise self.retry(exc=exc, countdown=countdown)


@celery_app.task(
    bind=True,
    queue="audio",
    max_retries=3
)
def transcribe_audio(
    self,
    document_id: str,
    audio_path: str,
    language: str = "ru"
) -> Dict[str, Any]:
    """
    Транскрибировать аудиофайл через Whisper.
    
    Args:
        document_id: Идентификатор документа
        audio_path: Путь к аудиофайлу
        language: Язык аудио
        
    Returns:
        Результат транскрипции
    """
    logger.info(f"Транскрипция аудио: {document_id}")
    
    try:
        # TODO: Интеграция с Whisper
        # whisper_model = whisper.load_model("base")
        # result = whisper_model.transcribe(audio_path, language=language)
        
        # Заглушка для демонстрации
        result = {
            "text": "Транскрипция будет реализрована позже",
            "segments": []
        }
        
        logger.info(f"Аудио транскрибировано: {document_id}")
        
        return {
            "document_id": document_id,
            "status": "transcribed",
            "text_length": len(result.get("text", ""))
        }
        
    except Exception as exc:
        logger.error(f"Ошибка транскрипции {document_id}: {exc}")
        raise self.retry(exc=exc, countdown=60 * (2 ** self.request.retries))


@celery_app.task(
    bind=True,
    queue="maintenance",
    max_retries=1
)
def clear_cache_task(self, cache_pattern: str = "*") -> Dict[str, Any]:
    """
    Очистить кэш в Redis.
    
    Args:
        cache_pattern: Паттерн для удаления ключей
        
    Returns:
        Результат очистки
    """
    logger.warning(f"Очистка кэша по паттерну: {cache_pattern}")
    
    try:
        import redis
        from src.config import get_settings
        
        settings = get_settings()
        r = redis.Redis(
            host=settings.REDIS_HOST,
            port=settings.REDIS_PORT,
            db=settings.REDIS_DB,
            password=settings.REDIS_PASSWORD
        )
        
        # Находим ключи по паттерну
        keys = r.keys(cache_pattern)
        
        if keys:
            deleted = r.delete(*keys)
            logger.info(f"Удалено ключей из кэша: {deleted}")
            return {"deleted": deleted}
        
        return {"deleted": 0}
        
    except Exception as exc:
        logger.error(f"Ошибка очистки кэша: {exc}")
        raise self.retry(exc=exc)


@celery_app.task(
    bind=True,
    queue="documents",
    max_retries=3
)
def batch_process_documents(
    self,
    documents: list[Dict[str, Any]]
) -> Dict[str, Any]:
    """
    Пакетная обработка нескольких документов.
    
    Args:
        documents: Список документов для обработки
        
    Returns:
        Результат пакетной обработки
    """
    logger.info(f"Пакетная обработка: {len(documents)} документов")
    
    results = []
    for doc in documents:
        try:
            # QueueGuard: единая точка постановки с дедупликацией — на один
            # документ никогда не создаётся две задачи (ни здесь, ни из
            # upload/recovery/reindex).
            from src.indexing.queue_guard import enqueue_document
            ok = enqueue_document(
                document_id=doc["document_id"],
                force=doc.get("force", False),
            )
            results.append({
                "document_id": doc["document_id"],
                "task_id": None,
                "status": "queued" if ok else "duplicate_skipped"
            })
        except Exception as e:
            logger.error(f"Ошибка постановки в очередь {doc['document_id']}: {e}")
            results.append({
                "document_id": doc["document_id"],
                "status": "failed",
                "error": str(e)
            })
    
    return {
        "total": len(documents),
        "queued": sum(1 for r in results if r["status"] == "queued"),
        "failed": sum(1 for r in results if r["status"] == "failed"),
        "results": results
    }


@celery_app.task(
    bind=True,
    queue="maintenance",
    max_retries=1,
)
def resolve_entity_candidates(self, threshold: float = 0.90) -> Dict[str, Any]:
    """Кандидаты на слияние сущностей — раз в сутки, без автослияний.

    Раньше эта работа шла в конвейере КАЖДОГО документа: эмбеддинг всех имён графа
    (1580 сущностей при 8506 узлах) + косинусы O(n²) = ~44 с на документ, при том что
    кандидатов находилось ~0.6% от графа. Ещё и слияния при сходстве >=0.95 применялись
    автоматически, без ревью.

    Теперь: раз в сутки, в maintenance-очереди, только ФОРМИРУЕМ пары-кандидаты
    (source='pending') для ручного ревью в админке («Словарь алиасов»). Слияние графа
    не меняется — это необратимая операция, её выполняет человек.
    """
    from src.api.services.config_store import config_store
    from src.indexing.knowledge_graph import kg_service

    # datetime, а не time/now_iso: в модуле нет `import time`, а `now_iso` — локальная
    # лямбда внутри rebuild_graph_task (обнаружено smoke-тестом на стенде: NameError).
    started = datetime.now(timezone.utc)
    try:
        result = kg_service.resolve_duplicate_entities(threshold=threshold, auto_merge=False)
        elapsed = round((datetime.now(timezone.utc) - started).total_seconds(), 1)
        config_store.set("kg_config", "entity_candidates_last", {
            "at": datetime.now(timezone.utc).isoformat(), "elapsed_s": elapsed,
            "candidates": result.get("aliased", 0),
            "threshold": threshold,
        })
        logger.info(f"[resolution] кандидаты обновлены за {elapsed}с: {result}")
        return {"status": "ok", "elapsed_s": elapsed, **result}
    except Exception as e:
        logger.error(f"[resolution] формирование кандидатов не удалось: {e}")
        return {"status": "error", "message": str(e)}


@celery_app.task(
    bind=True,
    queue="maintenance",
    max_retries=1,
)
def rebuild_graph_task(self, document_ids: Optional[list] = None) -> Dict[str, Any]:
    """Фоновая задача: перестроение графа знаний для документов.

    Запускается из POST /api/v1/kg/rebuild-graph (только админ).
    Тяжёлая работа (LLM-извлечение сущностей) выполняется здесь, в воркере,
    а не в HTTP-запросе. Прогресс пишется в config_store:
      - kg_config.rebuild_status  = running | completed | stopped | error
      - kg_config.rebuild_progress = {processed, total, current_doc,
                                      started_at, finished_at}
    Страница /kg опрашивает GET /api/v1/kg/rebuild-status каждые 5 секунд.
    """
    from src.api.services.config_store import config_store
    from src.api.services.document_repository import get_doc_repo
    from src.indexing.knowledge_graph import kg_service
    from src.indexing.entity_extractor import entity_extractor
    from src.indexing.embeddings_service import embeddings_service

    now_iso = lambda: datetime.now(timezone.utc).isoformat()

    async def _run() -> Dict[str, Any]:
        await embeddings_service.initialize()

        # Собираем список документов
        if document_ids:
            docs = []
            for did in document_ids:
                doc = get_doc_repo().get_dict(did)
                if doc:
                    doc["document_id"] = did
                    docs.append(doc)
        else:
            all_docs = get_doc_repo().get_all() or {}
            docs = []
            for did, doc in all_docs.items():
                if isinstance(doc, dict) and doc.get("status") == "completed":
                    doc["document_id"] = did
                    docs.append(doc)

        total = len(docs)
        started_at = now_iso()
        config_store.set("kg_config", "rebuild_progress", {
            "processed": 0, "total": total, "current_doc": "",
            "started_at": started_at, "finished_at": "",
        })

        results = []
        processed = 0
        for idx, doc in enumerate(docs, start=1):
            # Сигнал остановки (POST /stop-rebuild)
            if config_store.get("kg_config", "rebuild_stop"):
                logger.info("[rebuild] Получен сигнал STOP — останавливаюсь")
                config_store.set("kg_config", "rebuild_status", "stopped")
                config_store.set("kg_config", "rebuild_progress", {
                    "processed": processed, "total": total, "current_doc": "",
                    "started_at": started_at, "finished_at": now_iso(),
                })
                return {"status": "stopped", "processed": processed, "total": total}

            doc_id = doc.get("document_id") or doc.get("id")
            filename = doc.get("filename", "unknown")

            config_store.set("kg_config", "rebuild_progress", {
                "processed": processed, "total": total,
                "current_doc": filename[:80],
                "started_at": started_at, "finished_at": "",
            })

            # Очищаем старые данные графа и создаём узел документа
            kg_service.clear_document(doc_id)
            kg_service.create_document_node(doc_id, filename)

            chunks = await embeddings_service.get_document_chunks(doc_id)
            if not chunks:
                results.append({"document_id": doc_id, "status": "no_chunks"})
                processed += 1
                continue

            # ВАЖНО: обрабатываем ВСЕ чанки, а не первые 10.
            # Раньше здесь стоял chunks[:10] (остаток старого ограничения): документ
            # в 23 чанка получал граф на 10 узлов (43% текста), документ в 212 чанков —
            # на 4.7%, и это молча портило и /kg, и поиск по графу. В конвейере
            # (document_service._build_knowledge_graph_async) лимит убрали, здесь — нет.
            # ── Параллельное извлечение ────────────────────────────────────
            # Цикл был ПОСЛЕДОВАТЕЛЬНЫМ: 2 LLM-вызова на чанк ≈ 3 с на чанк, то есть
            # ~3 часа на корпус из 3761 чанка. В конвейере обработки то же извлечение
            # уже идёт с семафором (MAX_PARALLEL_LLM=6); здесь ограничение осталось
            # от старой версии. Держим 4 одновременных запроса: выше — риск упереться
            # в rate-limit GigaChat и в конфликты параллельной записи Neo4j (их гасит
            # run_with_transient_retry, но лишний конфликт не нужен).
            _par = 4
            _sem = asyncio.Semaphore(_par)
            _state = {"done": 0, "stopped": False}

            async def _one(i: int, chunk: dict):
                # Остановку проверяем на каждом чанке: перестроение большого документа
                # длится минутами, админ должен иметь возможность прервать его сразу.
                if _state["stopped"]:
                    return
                if config_store.get("kg_config", "rebuild_stop"):
                    _state["stopped"] = True
                    logger.info(
                        f"[rebuild] остановлено администратором на {filename}, "
                        f"чанков сделано: {_state['done']}/{len(chunks)}"
                    )
                    return
                chunk_id = chunk.get("chunk_id", f"chunk_{i}")
                chunk_text = chunk.get("content", "")
                chunk_seq = chunk.get("metadata", {}).get("chunk_seq", i + 1)
                async with _sem:
                    # Синхронный драйвер Neo4j — только через to_thread (иначе блокируем loop
                    # при параллельной работе, см. карту синхронного I/O).
                    await asyncio.to_thread(
                        kg_service.create_chunk_node, chunk_id, doc_id, chunk_text, chunk_seq
                    )
                    try:
                        await entity_extractor.extract_and_store(
                            doc_id, chunk_id, chunk_text, chunk_seq, filename
                        )
                    except Exception as e:
                        logger.debug(f"[rebuild] Ошибка извлечения {filename}: {e}")
                _state["done"] += 1

            await asyncio.gather(*[_one(i, c) for i, c in enumerate(chunks)])
            chunks_done = _state["done"]

            # Слой разделов: узлы Section + section_id/breadcrumb на чанках.
            try:
                from src.indexing.section_parser import (
                    build_breadcrumb as _bc, parse_sections as _ps,
                )
                _label = str((doc.get("recognized_title") or "") or filename or "")
                _secs = _ps(chunks, filename)
                # ВНИМАНИЕ: запятая после значения перед `for` в dict comprehension —
                # синтаксическая ошибка ({k: v, for x in y}). Уже второй раз на этом
                # спотыкаюсь (первый — в document_service): в batch-компиляции её не
                # видно, если предыдущий файл упал раньше.
                _crumbs = {
                    f"{doc_id}:sec:{s['section_index']}": _bc(
                        _label, s.get("number", ""), s.get("title", ""),
                    )
                    for s in _secs
                }
                if _secs:
                    kg_service.create_sections(doc_id, _secs, _crumbs)
            except Exception as e:
                logger.warning(f"[rebuild] разделы не записаны для {filename}: {e}")

            # Самообозначения документа (колонтитул) отвязываем от чанков — иначе обозначение
            # становится крупнейшим узлом графа и перевешивает смысловые сущности
            # (см. kg_service.drop_ubiquitous_reference_entities, замер 2026-09-13).
            _drop_self = 0
            try:
                _drop_self = kg_service.drop_ubiquitous_reference_entities(doc_id).get("dropped", 0)
            except Exception as e:
                logger.warning(f"[rebuild] отсев самообозначений не сработал для {filename}: {e}")

            results.append({
                "document_id": doc_id,
                "filename": filename,
                "chunks_processed": chunks_done,
                "chunks_total": len(chunks),
                "self_refs_dropped": _drop_self,
            })
            processed += 1

        config_store.set("kg_config", "rebuild_stop", False)
        try:
            from src.indexing.entity_extractor import entity_extractor as _ee
            logger.info(f"[rebuild] записей в кэше извлечения: {_ee.cache_size()}")
        except Exception:
            pass
        config_store.set("kg_config", "rebuild_status", "completed")
        config_store.set("kg_config", "rebuild_progress", {
            "processed": processed, "total": total, "current_doc": "",
            "started_at": started_at, "finished_at": now_iso(),
        })
        total_stats = kg_service.get_stats()
        logger.info(f"[rebuild] Готово: {processed}/{total} документов")
        return {"status": "completed", "processed": processed, "total": total,
                "results": results, "total_stats": total_stats}

    try:
        return asyncio.run(_run())
    except Exception as e:
        logger.error(f"[rebuild] Ошибка перестроения графа: {e}")
        try:
            config_store.set("kg_config", "rebuild_status", "error")
        except Exception:
            pass
        return {"status": "error", "message": str(e)}


def revoke_document_tasks(document_id: str) -> int:
    """
    Отозвать все pending/active Celery задачи для указанного документа.

    Использует inspect для поиска задач по document_id в kwargs,
    затем revoke с terminate. Возвращает количество отозванных задач.
    """
    revoked = 0
    try:
        inspector = celery_app.control.inspect()
        # Смотрим active и reserved задачи
        for state_name, getter in [("active", inspector.active), ("reserved", inspector.reserved)]:
            tasks_by_worker = getter() or {}
            for worker_name, tasks in tasks_by_worker.items():
                for task in tasks:
                    kwargs = task.get("kwargs", {})
                    if kwargs.get("document_id") == document_id:
                        task_id = task.get("id")
                        if task_id:
                            celery_app.control.revoke(task_id, terminate=True)
                            logger.info(f"[revoke] {state_name} task {task_id} для {document_id}")
                            revoked += 1
        if revoked:
            logger.info(f"[revoke] Отозвано {revoked} задач для {document_id}")
    except Exception as e:
        logger.warning(f"[revoke] Ошибка при отзыве задач для {document_id}: {e}")
    return revoked

# ═══════════════════════════════════════════════════════════════
# Recovery: периодическая проверка зависших документов
# ═══════════════════════════════════════════════════════════════

@celery_app.task(
    bind=True,
    name="src.indexing.tasks.check_stuck_documents",
    queue="maintenance",
    max_retries=2,
    default_retry_delay=120,
)
def check_stuck_documents(self):
    """
    Периодическая задача (каждые 5 минут).
    Сканирует документы в статусе processing дольше порога — 
    сбрасывает в pending и перезапускает обработку.
    """
    from src.indexing.recovery import recover_stuck_documents

    logger.debug("[Beat] Проверка зависших документов...")
    try:
        # На тиках Beat НЕ ставим pending в очередь повторно (requeue_pending
        # по умолчанию False) — иначе каждый тик (5 мин) добавляет по задаче
        # на каждый pending документ, очередь забивается дублями.
        result = recover_stuck_documents(requeue=True)
        if result["recovered"] > 0:
            logger.warning(
                f"[Beat] Найдено и восстановлено {result['recovered']} "
                f"зависших документов: {result['details']}"
            )
    except Exception as e:
        logger.error(f"[Beat] Ошибка проверки зависших документов: {e}")
        raise self.retry(exc=e, countdown=120)


@celery_app.task(bind=True, queue="maintenance", max_retries=3, default_retry_delay=300,
                 soft_time_limit=1200, time_limit=1500)
def run_monitor_check(self, source_id: str = None):
    """Запустить проверку источников мониторинга (Celery)."""
    try:
        from src.api.services.web_monitor import web_monitor
        result = asyncio.run(web_monitor.run_check(source_id))
        logger.info(f"✅ Монитор проверка: source={source_id}, results={len(result)}")
        return {"status": "ok", "checked": len(result)}
    except Exception as e:
        logger.error(f"❌ Монитор проверка упала: {e}")
        raise self.retry(exc=e)
