"""Проверка связи редакций: загрузка новой версии документа создаёт ребро NEW_EDITION_OF.

Что делает: берёт существующий документ, загружает ЕГО ЖЕ содержимое как новую версию
(force_new=True) и проверяет, что в графе появилась связь (новая)-[:NEW_EDITION_OF]->(прежняя)
с версией и схемой. Затем удаляет тестовую новую версию, чтобы не мусорить в корпусе.

Запуск: docker exec kag-api python /app/data/edition_link_probe.py
"""
import asyncio
import os
import pathlib

from neo4j import GraphDatabase


async def main():
    from src.api.services.document_service import document_service
    from src.api.services.document_repository import get_doc_repo

    # берём небольшой существующий документ
    repo = get_doc_repo()
    docs = repo.get_all() or {}
    target = None
    for did, d in docs.items():
        if (d.get("chunks_count") or 0) > 3 and (d.get("file_size") or 0) < 200000:
            target = (did, d)
            break
    if not target:
        print("не нашёл подходящий документ")
        return 1
    old_id, old = target
    print(f"прежний документ: {old_id[:12]} | {str(old.get('filename'))[:40]} | v{old.get('version')}")

    path = pathlib.Path("/app/data/uploads") / f"{old_id}_{old.get('filename')}"
    if not path.exists():
        print("файл прежнего документа не найден:", path)
        return 1
    content = path.read_bytes()

    rec = await document_service.upload_document(
        filename=str(old.get("filename")), file_content=content,
        uploaded_by=None, force_new=True, source_metadata={"probe": "edition-link"},
    )
    new_id = rec.document_id
    print(f"новая версия: {new_id[:12]} | v{rec.version} | previous_hash={str(rec.previous_hash)[:12]}")

    uri = os.environ.get("NEO4J_URI") or "bolt://neo4j:7687"
    d = GraphDatabase.driver(uri, auth=(os.environ.get("NEO4J_USER", "neo4j"), os.environ.get("NEO4J_PASSWORD", "")))
    with d.session() as s:
        r = s.run("MATCH (n:Document {id: $new})-[rel:NEW_EDITION_OF]->(o:Document) "
                  "RETURN o.id AS old, rel.version AS v, rel.schema_version AS sv", new=new_id).single()
        if r:
            print(f"связь есть: {new_id[:12]} -[NEW_EDITION_OF v{r['v']} схема {r['sv']}]-> {str(r['old'])[:12]}")
            print("совпадает с прежним документом:", str(r["old"]) == old_id)
        else:
            print("СВЯЗИ НЕТ")
        # уборка: удаляем тестовую новую версию (запись, точки, файл, узел графа)
        try:
            from src.indexing.embeddings_service import service_for_document
            await service_for_document(new_id).delete_document(new_id)
            s.run("MATCH (n:Document {id: $new}) DETACH DELETE n", new=new_id)
            repo.delete(new_id)
            path2 = pathlib.Path("/app/data/uploads") / f"{new_id}_{old.get('filename')}"
            if path2.exists():
                path2.unlink()
            print("тестовая версия удалена (запись, точки, узел, файл)")
        except Exception as e:  # noqa: BLE001
            print("уборка неполная:", str(e)[:140])
    d.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
