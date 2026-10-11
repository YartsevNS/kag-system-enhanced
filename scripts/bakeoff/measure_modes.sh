#!/usr/bin/env bash
# Замер одного и того же документа в ДВУХ режимах проверки цитат: мягком и строгом.
#
# Мягкий (require_evidence=false): пишутся все связи модели, у части цитаты может не быть.
# Строгий (true): пишутся только связи с цитатой, дословно найденной в тексте фрагмента.
# Для каждого режима граф документа пересобирается ШТАТНОЙ задачей (тот же код, что за кнопкой),
# затем считается одна и та же метрика — доля связей с подтверждённой цитатой по ГРАФУ.
#
# Запуск на стенде:  bash measure_modes.sh <документ> [<документ> ...]
set -u

DOCS="$*"
if [ -z "$DOCS" ]; then
  echo "укажи хотя бы один документ"
  exit 1
fi

for D in $DOCS; do
  for MODE in false true; do
    LABEL=$([ "$MODE" = "true" ] && echo "СТРОГИЙ" || echo "МЯГКИЙ")
    echo "=============================================================="
    echo "режим: $LABEL (require_evidence=$MODE) | документ: $D"
    echo "=============================================================="
    docker exec kag-api python /tmp/set_require_evidence.py "$MODE" 2>/dev/null | tail -1
    # ЧИСТКА перед прогоном: смысловые связи документа висят на сущностях, а не на узлах документа,
    # поэтому штатная чистка их не снимает — без этого шага замер показывает смесь прогонов
    # (числа росли 551 → 1765 → 1894 → 1976 на одном документе).
    docker exec kag-api python /tmp/clear_doc_relations.py "$D" 2>/dev/null | tail -1
    docker exec kag-system_worker-maintenance_1 python /tmp/rebuild_doc_graph.py "$D" 2>/dev/null \
      | grep -E "задача завершена|chunks_processed" | tail -1
    docker exec kag-api python /tmp/verify_doc_evidence.py "$D" 0 2>/dev/null \
      | grep -E "фрагментов в графе|связей всего|с непустой|ЦИТАТА|без цитаты"
    echo
  done
done
