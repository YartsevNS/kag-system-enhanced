# Админ-API: бэкап, SSH, баланс провайдеров

Заметки по эксплуатации админских эндпоинтов (`/api/v1/admin/models/*`), включая
разбор пунктов внешнего аудита от 2026-09-12/13 (проверено кодом и на стенде 18).

## Бэкап настроек: категории берутся из БД

`GET /api/v1/admin/models/backup` — категории читаются динамически
(`SELECT DISTINCT category FROM system_configs`), как в `/backup-documents`.

Было: список `BACKUP_NAMESPACES` захардкожен и отстал. В бэкап не попадали
`providers` (провайдеры LLM!), `search` (режим поиска), `system` (блокировки
загрузки и обработки), `setup`, `upload_config`, `entity_cache` — при переносе на
другой сервер конфигурация провайдеров терялась.

Сейчас: в бэкапе 9 категорий (проверено) —

```
function_map, kg_config, process_logs, providers, search, setup,
system, upload_config, web_monitor
```

Кэши по умолчанию исключаются (`entity_cache` — эмбеддинги сущностей,
восстанавливать бессмысленно), но включаются параметром `?include_caches=true`.
В ответе есть `categories` и `skipped_caches` — видно, что именно уехало.

Восстановление (`POST /backup-restore`, multipart, поле `file`) формат не меняло:
круг «скачал → залил» проверен на стенде — `restored: 9, errors: []`, значения
`providers`/`search`/`system` после восстановления не изменились.

## SSH: пароли не в командной строке

Было (`restart-ollama` и `ssh_manager.test_connection`): пароль подставлялся
аргументом sshpass (флаг `-p`) → виден в `ps` на хосте; sudo-пароль уходил в
командной строке на удалённой стороне; вызов шёл через `shell=True`.

Стало:

- SSH-пароль — через переменную окружения: `sshpass -e`, `SSHPASS` в `env`
  дочернего процесса (в argv пароля нет);
- sudo-пароль — на stdin (`sudo -S -p ''`), поэтому в `ps` удалённой стороны его
  тоже нет;
- вызовы только списком аргументов, `shell=True` не используется;
- имя systemd-сервиса санитизируется (`_safe_service_name`: только
  `[A-Za-z0-9_.@-]`, иначе `ollama`) — защита от инъекции через настройку.

Тест: `tests/test_admin_audit_fixes.py` — пароль не в argv, `SSHPASS` в env,
sudo-пароль в stdin, инъекции в имени сервиса отсекаются, `shell=True` и
`sshpass -p` в исходниках отсутствуют.

## Баланс провайдеров

Три дублирующих эндпоинта сведены к одной функции `_provider_balance()`:
`/ext-llm/balance` и `/graph/balance` (старые конфигурации) и
`/providers/{id}/balance` (актуальный, его зовёт админка).

DeepSeek: ответ `{is_available, balance_infos: [{currency, total_balance,
granted_balance, topped_up_balance}]}`. Раньше в двух местах из трёх читалось
поле `balance`, которого нет → в интерфейсе было «0.0 токенов» при живом ключе.
Теперь баланс берётся из `balance_infos`, при отрицательной сумме статус
`balance_ok=false` и приписка «(баланс исчерпан)» (у DeepSeek `is_available`
остаётся `true` и при исчерпанном балансе — проверено на живом ключе: −1.18 CNY).

Провайдеры, которые баланс не отдают (gigachat, openai): `balance_ok=null`,
`balance_known=false`, в админке нейтральное «—», а не красная ошибка и не нули.
Ошибки HTTP различаются: 401/403 → «API ключ недействителен», иначе `HTTP <код>`.

## Что из аудита подтвердилось, но осталось в бэклоге

- `admin_models.py` — 2542 строки, дубли имён `get_upload_config`/
  `save_upload_config` (форматы загрузки и блокировка ингеста), 9 тел `dict`
  вместо Pydantic, 0 `response_model`, хардкод путей (`/app/src`,
  `/home/yartsevn/kag-system`, `/app/kag.env`) и имени контейнера `kag-keycloak`,
  глобалы `_ext_llm_config`/`_graph_model_config`, `asyncio.to_thread` применён
  непоследовательно (8 обёрнутых против 16 прямых синхронных вызовов).
- `/deploy` (запись файлов в `/app/src`, `git pull`, рестарт) — RCE для
  администратора по замыслу; авторизация есть (middleware защищает
  `/api/v1/admin`, роли admin/kag-admin).
- Отклонено как неверное в аудите: «NameError в get_llm_config» (модульный импорт
  `config_store` есть, к моменту запроса модуль импортирован), «нет проверки прав
  на админ-роуты», «`_graph_model_config` используется до определения».
