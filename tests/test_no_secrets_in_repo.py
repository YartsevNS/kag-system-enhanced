"""В репозитории не должно быть секретов.

Правило проекта: пароли и ключи приходят ТОЛЬКО из окружения (.env, генерируется
`deploy.sh`). В коде им места нет — иначе система поднимается на значении,
известном любому, у кого есть репозиторий.

Тест структурный: он не «ищет конкретные утёкшие строки» (их перечисление само
было бы секретом), а проверяет правила, которые ловят любой новый такой случай:

1. в `src/config.py` нет непустых литеральных умолчаний у настроек-секретов;
2. в `docker-compose.yml` нет умолчаний вида `${VAR:-<литерал>}` для секретов
   (должно быть `${VAR:?сообщение}`) и нет `NEO4J_AUTH=neo4j/<литерал>`;
3. в `PROJECT.md` и `docs/` нет строк «пароль: <значение>» с литералом;
4. `.env` не под контролем git (только `.env.example`).

Проверка запускается в общем прогоне: если кто-то вернёт пароль в код — тест
упадёт с указанием файла и строки.
"""
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

SECRET_SETTING = re.compile(
    r"^\s*([A-Z_]*(?:PASSWORD|PASSWD|SECRET|API_KEY|TOKEN)[A-Z_]*)\s*:\s*(?:str|Optional\[str\])\s*=\s*(['\"])(.+?)\2",
    re.M,
)
# «admin» в KEYCLOAK_ADMIN — это имя пользователя, не секрет; пустая строка = «не задано»
ALLOWED_LITERALS = {"", "admin"}


def test_config_has_no_literal_secret_defaults():
    text = (ROOT / "src/config.py").read_text(encoding="utf-8")
    bad = [
        f"src/config.py: {name} = {value!r}"
        for name, _, value in SECRET_SETTING.findall(text)
        if value not in ALLOWED_LITERALS
    ]
    assert not bad, (
        "секрет-настройка имеет литеральное умолчание (пароль в коде!):\n  " + "\n  ".join(bad)
    )


def test_compose_requires_secrets():
    text = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    bad = []
    for i, line in enumerate(text.splitlines(), 1):
        # AUTH_ENABLED — флаг функции (SSO), не секрет; AUTH проверяем отдельно ниже
        m = re.search(r"\$\{([A-Z_]*(?:PASSWORD|SECRET|API_KEY|TOKEN)[A-Z_]*):-([^}]*)\}", line)
        if m and m.group(2).strip():
            bad.append(f"docker-compose.yml:{i}: {m.group(0)}")
        if re.search(r"NEO4J_AUTH=neo4j/\S", line) and "${" not in line:
            bad.append(f"docker-compose.yml:{i}: {line.strip()}")
    assert not bad, (
        "секрет задан значением по умолчанию — при отсутствии .env система поднимется "
        "на известном пароле (нужно ${VAR:?сообщение}):\n  " + "\n  ".join(bad)
    )


def test_no_password_literals_in_docs():
    files = [ROOT / "PROJECT.md"] + sorted((ROOT / "docs").rglob("*.md"))
    pat = re.compile(r"(?i)(пароль|password|secret|api[_-]?key)\s*[:=]\s*[`'\"]?([A-Za-zА-Яа-я0-9!@#$%^&*_\-]{6,})")
    # разрешены ссылки на переменные и примеры-заглушки
    allowed = re.compile(
        r"(?i)^(из|в|не|нет|\$|\{|см|<|\*\*\*|менеджер|переменн|env|your|example|placeholder|"
        r"change_me|CHANGE_ME|azure|dashscope|openai|sk-|test|fake)"
    )
    # строки-определения моделей и аннотации типов секретами не являются
    code_like = re.compile(r"(Column\(|Field\(|:\s*str|:\s*Optional|=\s*None)")
    bad = []
    for path in files:
        if not path.exists():
            continue
        for i, line in enumerate(path.read_text(encoding="utf-8", errors="ignore").splitlines(), 1):
            m = pat.search(line)
            if m and not allowed.match(m.group(2)) and not code_like.search(line):
                rel = path.relative_to(ROOT)
                bad.append(f"{rel}:{i}: {line.strip()[:90]}")
    assert not bad, (
        "в документации литеральный секрет (заменить ссылкой на переменную окружения):\n  "
        + "\n  ".join(bad)
    )


def test_env_not_tracked():
    tracked = subprocess.run(
        ["git", "ls-files"], cwd=ROOT, capture_output=True, text=True
    ).stdout.splitlines()
    env_files = [f for f in tracked if Path(f).name == ".env" or f.endswith("/.env")]
    assert not env_files, f".env под контролем git: {env_files}"
