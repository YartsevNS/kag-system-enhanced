"""Диагностика связи редакций: где обрывается цепочка."""
import os

from neo4j import GraphDatabase

from src.api.services.document_repository import get_doc_repo

repo = get_doc_repo()
uri = os.environ.get("NEO4J_URI") or "bolt://neo4j:7687"
drv = GraphDatabase.driver(uri, auth=(os.environ.get("NEO4J_USER", "neo4j"), os.environ.get("NEO4J_PASSWORD", "")))

all_docs = repo.get_all() or {}
with_prev = [(k, v.get("previous_document_id"), v.get("version"), v.get("filename"))
             for k, v in all_docs.items() if v.get("previous_document_id")]
print("записей с предыдущей редакцией:", len(with_prev))
for k, p, v, f in with_prev[:3]:
    print(f"   {k[:12]} v{v} ← {str(p)[:12]} | {str(f)[:40]}")

with drv.session() as s:
    total_nodes = s.run("MATCH (d:Document) RETURN count(d) AS n").single()["n"]
    edges = s.run("MATCH ()-[r:NEW_EDITION_OF]->() RETURN count(r) AS n").single()["n"]
    print(f"узлов Document в графе: {total_nodes} | связей NEW_EDITION_OF: {edges}")
    if with_prev:
        k, p, v, f = with_prev[0]
        found = s.run("MATCH (d:Document {id: $a}), (o:Document {id: $b}) RETURN count(*) AS n",
                      a=k, b=p).single()["n"]
        print(f"оба узла для пары {k[:8]}→{str(p)[:8]} есть: {found == 1}")
        only_new = s.run("MATCH (d:Document {id: $a}) RETURN count(d) AS n", a=k).single()["n"]
        only_old = s.run("MATCH (o:Document {id: $b}) RETURN count(o) AS n", b=p).single()["n"]
        print(f"узел новой редакции: {only_new}, узел прежней: {only_old}")
drv.close()
