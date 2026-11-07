"""P1.8: факты онтологии проверяются по ACL чанка, а не по попаданию в выдачу поиска."""
import io

EMB = "src/indexing/embeddings_service.py"
FACTS = "src/indexing/typedb_facts.py"
CHAT = "src/api/services/chat_service.py"


def test_visible_chunk_ids_exists():
    text = io.open(EMB, encoding="utf-8").read()
    assert "async def visible_chunk_ids(" in text
    # ACL: публичное ИЛИ allow-группа/пользователь, deny — приоритетнее
    block = text.split("async def visible_chunk_ids(", 1)[1].split("async def", 1)[0]
    for token in ('visibility', 'allow_group_ids', 'allow_user_ids',
                  'deny_group_ids', 'deny_user_ids'):
        assert token in block, token
    assert "if not is_admin:" in block


def test_visible_chunk_ids_checks_both_collections():
    text = io.open(EMB, encoding="utf-8").read()
    assert "def _collections(self)" in text
    assert '"kag_news"' in text


def test_all_facts_skips_chunk_filter_but_keeps_acl_by_caller():
    text = io.open(FACTS, encoding="utf-8").read()
    assert "def all_facts(" in text
    assert "acl_by_caller=True" in text
    block = text.split("def query_facts(", 1)[1].split("def format_facts_block", 1)[0]
    assert "if not acl_by_caller and chunk_id not in visible_chunk_ids:" in block


def test_facts_query_does_not_require_source_origin():
    """source_origin есть не у всех документов компании; требование его рвало выборку."""
    text = io.open(FACTS, encoding="utf-8").read()
    block = text.split("def query_facts(", 1)[1].split("def format_facts_block", 1)[0]
    assert "has source-chunk-id $ch;" in block
    assert "has source_origin $so" not in block


def test_chat_uses_acl_not_search_results():
    text = io.open(CHAT, encoding="utf-8").read()
    block = text.split("2b2. Факты TypeDB", 1)[1].split("# 2c.", 1)[0]
    assert "all_facts as _td_all_facts" in block
    assert "visible_chunk_ids(" in block
    assert "visible_chunk_ids = {" not in block
