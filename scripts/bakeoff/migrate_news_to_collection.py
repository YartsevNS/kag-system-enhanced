"""Перенос векторов новостных документов из основной коллекции в отдельную (kag_news).

Зачем: новости сейчас лежат в одной коллекции с нормативными документами и разбавляют выдачу.
Переносим БЕЗ повторного распознавания и без повторного эмбеддинга: читаем точки с векторами
из основной коллекции и записываем те же точки в коллекцию новостей.

Порядок безопасности: сначала записать в новую коллекцию и ПРОВЕРИТЬ число точек, и только потом
удалять из старой. Иначе при сбое документ исчезает из обеих.

Запуск в контейнере api (идентификаторы документов передаются аргументами):
  docker exec kag-api python /app/data/migrate_news_to_collection.py <doc_id> [<doc_id> ...]
"""
import os
import sys

from qdrant_client import QdrantClient
from qdrant_client.http import models as qm
from qdrant_client.http.models import PointStruct

from src.config import get_settings
from src.indexing.embeddings_service import news_embeddings_service

settings = get_settings()
host = os.environ.get("QDRANT_HOST", "qdrant").replace("https://", "").replace("http://", "")
client = QdrantClient(url=f"http://{host}:{settings.QDRANT_PORT}", api_key=os.environ.get("QDRANT_API_KEY") or None)

MAIN = settings.QDRANT_COLLECTION
NEWS = settings.QDRANT_NEWS_COLLECTION


def _ensure_news_collection() -> None:
    """Коллекция новостей должна существовать — создаём тем же сервисом (та же схема: dense+sparse)."""
    try:
        client.get_collection(NEWS)
        print(f"коллекция {NEWS} уже есть")
    except Exception:
        print(f"создаю коллекцию {NEWS}")
        import asyncio
        asyncio.run(news_embeddings_service().initialize())


def _scroll_all(collection: str, doc_id: str) -> list:
    """Все точки документа с векторами (страницами, чтобы не упереться в лимит)."""
    points, offset = [], None
    flt = qm.Filter(must=[qm.FieldCondition(key="document_id", match=qm.MatchValue(value=doc_id))])
    while True:
        batch, offset = client.scroll(collection_name=collection, scroll_filter=flt, limit=500,
                                      with_vectors=True, with_payload=True, offset=offset)
        points.extend(batch)
        if offset is None or not batch:
            break
    return points


def _count(collection: str, doc_id: str) -> int:
    flt = qm.Filter(must=[qm.FieldCondition(key="document_id", match=qm.MatchValue(value=doc_id))])
    return client.count(collection_name=collection, count_filter=flt, exact=True).count


def migrate(doc_id: str) -> tuple[int, int, int]:
    """Вернуть (перенесено, осталось в основной, стало в новостной)."""
    points = _scroll_all(MAIN, doc_id)
    if not points:
        return 0, 0, _count(NEWS, doc_id)
    # 1) пишем в новостную коллекцию
    # scroll отдаёт записи (Record), а upsert ждёт PointStruct — конвертируем явно,
    # иначе ValidationError и (что важнее) молчаливая потеря переноса.
    new_points = [PointStruct(id=p.id, vector=p.vector, payload=p.payload) for p in points]
    client.upsert(collection_name=NEWS, points=new_points, wait=True)
    moved = _count(NEWS, doc_id)
    if moved < len(points):
        raise RuntimeError(f"{doc_id}: записали {moved} из {len(points)} — НЕ удаляю из основной")
    # 2) только после проверки — удаляем из основной
    ids = [p.id for p in points]
    for i in range(0, len(ids), 500):
        client.delete(collection_name=MAIN, points_selector=qm.PointIdsList(points=ids[i:i + 500]), wait=True)
    return len(points), _count(MAIN, doc_id), moved


def main() -> int:
    doc_ids = sys.argv[1:]
    if not doc_ids:
        print("нужны идентификаторы документов")
        return 2
    _ensure_news_collection()
    print(f"переношу {len(doc_ids)} документов: {MAIN} → {NEWS}")
    total = 0
    problems = []
    for did in doc_ids:
        try:
            moved, left, now = migrate(did)
            total += moved
            flag = "ok " if left == 0 else "ОСТАЛОСЬ!"
            print(f"  {flag} {did[:12]}  перенесено {moved}, в основной осталось {left}, в новостной {now}")
            if left:
                problems.append(did)
        except Exception as e:  # noqa: BLE001
            problems.append(did)
            print(f"  ФЕЙЛ {did[:12]}: {type(e).__name__}: {str(e)[:140]}")
    print(f"\nитого перенесено точек: {total}")
    print(f"в основной коллекции осталось точек: {client.get_collection(MAIN).points_count}")
    print(f"в коллекции новостей точек: {client.get_collection(NEWS).points_count}")
    if problems:
        print(f"проблемные документы ({len(problems)}): {problems}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
