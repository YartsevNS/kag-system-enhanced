"""Минимальный измеритель ответов: отдельный контейнер, по одному вопросу, с паузами.

Почему сделано именно так (урок 19.09.2026: хост 18 роняли мои операции):
  * запускается в ОТДЕЛЬНОМ лёгком контейнере на сети стенда — внутри kag-api скрипты
    не выполняются, чтобы не тянуть туда весь стек приложения (torch/onnx и прочее);
  * один запрос в моменте, пауза между вопросами по умолчанию 15 с — никаких серий;
  * таймаут клиента 60 с: если провайдер задумался, клиент отваливается и ждёт, а не
    держит соединение (серверный таймаут 120 с остаётся серверу);
  * результат пишется после каждого вопроса, повторный запуск продолжает с места обрыва;
  * судейство здесь НЕ делается — ответы сохраняются, оценка считается отдельно.

Пароль не передаётся в аргументах и переменных (он виден в ps/docker inspect): файл
/tmp/.kag_pass готовится на хосте и монтируется только на чтение.

Запуск (с хоста 18):
    mkdir -p /tmp/kag_out
    printf "%s" "$ADMIN_PASSWORD" > /tmp/.kag_pass && chmod 600 /tmp/.kag_pass
    docker run --rm --network kag_internal --entrypoint python \
      -v /tmp:/work:ro -v /tmp/kag_out:/out \
      -e KAG_OUT=/out/kag_answers.json \
      kre44et/kag-base:2026.09.07 /work/collect_answers_lite.py
    rm -f /tmp/.kag_pass

Образ берётся уже имеющийся (kag-base), чтобы ничего не скачивать; скрипт использует
только стандартную библиотеку и внутрь kag-api не заходит.
"""
import ast
import json
import os
import sys
import time
import urllib.error
import urllib.request

BASE = os.environ.get("KAG_API_BASE", "http://api:8000/api/v1")
OUT = os.environ.get("KAG_OUT", "/work/kag_answers.json")
PAUSE = float(os.environ.get("KAG_PAUSE", "15"))
CLIENT_TIMEOUT = float(os.environ.get("KAG_TIMEOUT", "60"))
QUESTIONS_SRC = os.environ.get("KAG_QUESTIONS", "/work/collect_questions.py")


def load_questions():
    src = open(QUESTIONS_SRC, encoding="utf-8").read()
    for node in ast.parse(src).body:
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "QUESTIONS" for t in node.targets):
            return ast.literal_eval(node.value)
    raise SystemExit("не нашёл QUESTIONS")


def post(path, payload, token=None, timeout=CLIENT_TIMEOUT):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    req = urllib.request.Request(BASE + path, data=json.dumps(payload).encode(), headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def password():
    if os.environ.get("KAG_ADMIN_PASSWORD"):
        return os.environ["KAG_ADMIN_PASSWORD"]
    # файл монтируется в контейнер (обычно /work/.kag_pass); /tmp — на случай запуска
    # прямо на хосте стенда
    for path in ("/work/.kag_pass", "/tmp/.kag_pass"):
        if os.path.exists(path):
            with open(path, encoding="utf-8") as fh:
                return fh.read().strip()
    raise SystemExit("нет ни KAG_ADMIN_PASSWORD, ни файла .kag_pass")


def main() -> int:
    questions = load_questions()
    done = {}
    if os.path.exists(OUT):
        try:
            done = {r["id"]: r for r in json.load(open(OUT, encoding="utf-8"))}
        except Exception:
            done = {}
    print(f"вопросов: {len(questions)} | уже собрано: {len(done)} | пауза {PAUSE} с | "
          f"таймаут клиента {CLIENT_TIMEOUT} с", flush=True)

    token = post("/auth/login", {"username": "admin", "password": password()})["access_token"]
    print("вход выполнен", flush=True)

    for i, q in enumerate(questions, 1):
        if q["id"] in done:
            continue
        t0 = time.time()
        answer, err = "", None
        try:
            data = post("/chat/", {"messages": [{"role": "user", "content": q["query"]}],
                                   "stream": False, "temperature": 0.0}, token=token)
            answer = data.get("response") or data.get("answer") or ""
            # Скорость и расход: metadata в ответе бывает и словарём, и строкой repr —
            # разбираем оба варианта, чтобы сравнивать замеры по токенам и времени.
            md = data.get("metadata")
            usage = {}
            if isinstance(md, dict):
                usage = md.get("usage") or {}
            elif isinstance(md, str):
                try:
                    usage = json.loads(md.replace("'", '"')).get("usage") or {}
                except Exception:
                    import re as _re
                    pt = _re.search(r"prompt_tokens'?:\s*(\d+)", md)
                    ct = _re.search(r"completion_tokens'?:\s*(\d+)", md)
                    usage = {"prompt_tokens": int(pt.group(1)) if pt else None,
                             "completion_tokens": int(ct.group(1)) if ct else None}
            extra = {"usage": usage,
                     "prompt_tokens": usage.get("prompt_tokens"),
                     "completion_tokens": usage.get("completion_tokens"),
                     "sources": len(data.get("sources") or [])}
        except Exception as e:
            err = f"{type(e).__name__}: {str(e)[:70]}"
            extra = {"usage": {}, "prompt_tokens": None, "completion_tokens": None, "sources": 0}
        done[q["id"]] = {"id": q["id"], "query": q["query"], "answer": answer,
                         "answer_len": len(answer), "error": err,
                         "refused": ("не найдена" in answer.lower() or "не найдено" in answer.lower()),
                         "seconds": round(time.time() - t0, 1), **extra}
        try:
            json.dump(list(done.values()), open(OUT, "w", encoding="utf-8"),
                      ensure_ascii=False, indent=1)
        except Exception as e:
            print(f"  (не смог записать результат: {e})", flush=True)
        print(f"  {i:>2}/{len(questions)} id={q['id']:<3} {len(answer):>5} симв. "
              f"за {time.time() - t0:>5.1f} с | отказ: {'да' if done[q['id']]['refused'] else 'нет'} "
              f"{err or ''}", flush=True)
        time.sleep(PAUSE)

    print(f"\nсобрано: {len(done)} из {len(questions)}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
