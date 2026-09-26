"""Включить реранкер на сервисном бэкенде и проверить его живьём (запускается НА СТЕНДЕ).

Что делает: логинится админом, показывает состояние привязки реранкера до и после правки,
включает backend=service (модель на сервере моделей), дергает /service-health и задаёт один
реальный вопрос через чат, чтобы увидеть уже перезаряженный ответ.

Запуск: docker exec kag-api python /app/data/enable_reranker_service.py
"""
import json
import os
import urllib.request

API = os.environ.get("KAG_API", "http://localhost:8000")
ENDPOINT = os.environ.get("RERANK_ENDPOINT", "http://192.168.50.41:8010")
MODEL = os.environ.get("RERANK_MODEL", "DiTy/cross-encoder-russian-msmarco")
QUESTION = "Москва, Кутузова, д. 11, кор. 4, кв. 047 какие начисления были в 24 году, найти можешь?"


def req(method: str, path: str, token: str = "", payload: dict | None = None) -> dict:
    data = json.dumps(payload).encode() if payload is not None else None
    r = urllib.request.Request(API + path, data=data, method=method,
                               headers={"Content-Type": "application/json",
                                        **({"Authorization": "Bearer " + token} if token else {})})
    with urllib.request.urlopen(r, timeout=300) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def main() -> int:
    pw = ""
    for p in ("/tmp/.kag_pass", "/app/data/.kag_pass"):
        try:
            pw = open(p).read().strip(); break
        except Exception:
            continue
    token = req("POST", "/api/v1/auth/login",
                payload={"username": "admin", "password": pw or os.environ.get("ADMIN_PASSWORD", "")}
                ).get("access_token", "")
    print(f"вход: {'ок' if token else 'НЕ УДАЛСЯ'}")

    before = req("GET", "/api/v1/admin/models/reranker-config", token)
    print(f"ДО: enabled={before.get('enabled')} backend={before.get('backend')} "
          f"model={before.get('model')} top_k={before.get('top_k')}")

    after = req("POST", "/api/v1/admin/models/reranker-config", token, {
        "enabled": True, "backend": "service", "model": MODEL,
        "endpoint": ENDPOINT, "timeout_ms": 3000, "top_k": 5,
        "min_fragments": 4, "keep_dense_on_error": True,
    })
    print(f"ПОСЛЕ: enabled={after.get('enabled')} backend={after.get('backend')} "
          f"model={after.get('model')} endpoint={after.get('endpoint')} "
          f"timeout_ms={after.get('timeout_ms')} top_k={after.get('top_k')}")

    health = req("GET", "/api/v1/admin/models/reranker-config/service-health", token)
    print(f"ПРОВЕРКА СВЯЗИ: ok={health.get('ok')} endpoint={health.get('endpoint')} "
          f"модель сервиса={health.get('model')} ошибка={health.get('error')}")

    print("вопрос в чат с включённым реранкером…")
    ans = req("POST", "/api/v1/chat/", token,
              {"messages": [{"role": "user", "content": QUESTION}], "context_limit": 10})
    src = ans.get("sources") or []
    print(f"  ответ {len(ans.get('response') or '')} симв. | источников {len(src)} | "
          f"модель {ans.get('model')}")
    for s in src[:5]:
        print(f"    - {s.get('filename')} (score {s.get('score')})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
