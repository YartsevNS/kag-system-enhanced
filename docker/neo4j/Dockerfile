# Собственный образ Neo4j/DozerDB для kag-system-enhanced.
#
# Зачем:
# 1. Не зависеть от graphstack на Docker Hub при деплое (свой образ в своём аккаунте).
# 2. Вшить APOC в /plugins — jar уже внутри образа, при старте НИЧЕГО не качается
#    с GitHub (NEO4J_PLUGINS не нужен; раньше из-за блокировки
#    release-assets.githubusercontent.com из РФ контейнер зависал unhealthy).
#
# База: DozerDB = Neo4j CE 5.26.27 + GPL-патчи (multi-db, NODE KEY constraints,
# OpenGDS) — фичи реализованы jar'ами в lib/, поэтому взяли его целиком,
# а не официальный neo4j (тот не даёт multi-db на Community).
#
# Сборка:
#   docker build -t kre44et/dozerdb:5.26.27.0 -f docker/neo4j/Dockerfile docker/neo4j
# Публикация:
#   docker push kre44et/dozerdb:5.26.27.0
#
# APOC-версия должна совпадать с Neo4j по первым двум числам: Neo4j 5.26.x -> APOC 5.26.x.
# apoc-5.26.4-core.jar скачан с github.com/neo4j/apoc/releases (SHA256 4d37076c...).

FROM graphstack/dozerdb:5.26.27.0

# Вшиваем APOC core (нужен для apoc.coll.union в post_process_entities).
# Кладём в /plugins — стандартная директория Neo4j, подхватывается при старте.
COPY apoc-5.26.4-core.jar /plugins/

# Разрешаем процедуры APOC (по умолчанию Neo4j пускает только безопасное ядро).
# Внимание: в env-имени Neo4j точка -> одиночное _, литеральное _ -> двойное __.
# dbms.security.procedures.unrestricted -> NEO4J_dbms_security_procedures_unrestricted
ENV NEO4J_dbms_security_procedures_unrestricted=apoc.*
