from src.api.services.config_store import config_store
all_docs = config_store.get_all("documents") or {}
print("Docs in config_store:", len(all_docs))
for did, doc in list(all_docs.items())[:5]:
    t = type(doc).__name__
    if isinstance(doc, str):
        print("  %s: (str) %s" % (did[:12], doc[:50]))
    else:
        print("  %s: %s (%s)" % (did[:12], doc.get("filename","?"), doc.get("status","?")))

from src.api.services.document_service import document_service
print("Docs in memory:", len(document_service._documents))
