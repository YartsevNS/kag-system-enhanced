"""Разбор B5 без временных логов: создать версию, проверить поле, позвать построение узла вручную.

Зачем: прибор-проверка не показывает связь редакций, и надо понять, где обрыв. Здесь всё по шагам,
с печатью на каждом: (1) новая версия создаётся и в записи есть previous_document_id?
(2) есть ли у прежнего документа узел в графе? (3) создаётся ли связь, если позвать
create_document_node напрямую? После проверки тестовая версия удаляется.
"""
import asyncio
import os
import pathlib

from neo4j import GraphDatabase

from src.api.services.document_repository import get_doc_repo


def q(session, cypher, **kw):
    return session.run(cypher, **kw).single()


async def main():
    from src.api.services.document_service import document_service

    repo = get_doc_repo()
    docs = repo.get_all() or {}
    target = next(((k, v) for k, v in docs.items()
                   if (v.get("chunks_count") or 0) > 3 and (v.get("file_size") or 0) < 200000
                   and not v.get("previous_document_id")), None)
    if not target:
        print("нет подходящего документа")
        return 1
    old_id, old = target
    path = pathlib.Path("/app/data/uploads") / f"{old_id}_{old.get('filename')}"
    print(f"прежний: {old_id[:12]} | {str(old.get('filename'))[:38]} | v{old.get('version')}")
    print("у прежнего поле previous_document_id:", repr(old.get("previous_document_id")))

    rec = await document_service.upload_document(
        filename=str(old.get("filename")), file_content=path.read_bytes(),
        uploaded_by=None, force_new=True, source_metadata={"probe": "edition-diag-2"})
    new_id = rec.document_id
    stored = (repo.get_dict(new_id) or {})
    print(f"новая: {new_id[:12]} | v{stored.get('version')} | "
          f"previous_hash={str(stored.get('previous_hash'))[:12]} | "
          f"previous_document_id={str(stored.get('previous_document_id'))[:12]!r}")

    uri = os.environ.get("NEO4J_URI") or "bolt://neo4j:7687"
    drv = GraphDatabase.driver(uri, auth=(os.environ.get("NEO4J_USER", "neo4j"), os.environ.get("NEO4J_PASSWORD", "")))
    with drv.session() as s:
        old_node = q(s, "MATCH (d:Document {id: $id}) RETURN count(d) AS n", id=old_id)["n"]
        new_node = q(s, "MATCH (d:Document {id: $id}) RETURN count(d) AS n", id=new_id)["n"]
        print(f"узлы в графе: прежний {old_node}, новая версия {new_node}")

        # зовём построение узла так же, как это делает конвейер
        from src.indexing.knowledge_graph import kg_service
        kg_service.create_document_node(new_id, str(old.get("filename")))

        s2 = drv.session()
        new_node2 = q(s2, "MATCH (d:Document {id: $id}) RETURN count(d) AS n", id=new_id)["n"]
        edge = q(s2, "MATCH (n:Document {id: $a})-[r:NEW_EDITION_OF]->(o:Document) "
                     "RETURN o.id AS old, r.version AS v", a=new_id)
        print(f"после прямого вызова: узел новой версии {new_node2}, связь: "
              f"{('есть → ' + str(edge['old'])[:12] + ' v' + str(edge['v'])) if edge else 'НЕТ'}")
        # состояние полей узла
        keys = q(s2, "MATCH (d:Document {id: $id}) RETURN keys(d) AS k", id=new_id)["k"]
        print("ключи узла новой версии:", keys)
        s2.close()

    # уборка
    try:
        from src.indexing.embeddings_service import service_for_document
        await service_for_document(new_id).delete_document(new_id)
        with drv.session() as s:
            s.run("MATCH (n:Document {id: $id}) DETACH DELETE n", id=new_id)
        repo.delete(new_id)
        f = pathlib.Path("/app/data/uploads") / f"{new_id}_{old.get('filename')}"
        if f.exists():
            f.unlink()
        print("тестовая версия убрана")
    except Exception as e:  # noqa: BLE001
        print("уборка неполная:", str(e)[:120])
    drv.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
