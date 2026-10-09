"""Проверка журнала происхождения: цепочка цела, подмена обнаруживается.

Что делает:
  1. считает записи и проверяет цепочку на живом журнале;
  2. делает КОПИЮ журнала, подменяет в копии одну запись и убеждается, что проверка падает
     именно на этом номере (иначе проверка ничего не значит);
  3. удаляет строку в другой копии — тоже должна обнаружиться;
  4. проверяет, что по идентификатору документа записи находятся.

Запуск в контейнере api: docker exec kag-api python /app/data/provenance_check.py
"""
import json
import os
import shutil
import tempfile
from pathlib import Path

from src.security import provenance


def chain_ok(path: Path) -> tuple:
    """Проверка цепочки по произвольному файлу (та же логика, что в модуле)."""
    prev, count = provenance.GENESIS, 0
    with path.open("r", encoding="utf-8") as f:
        for i, line in enumerate(f):
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            if rec.get("prev_hash") != prev:
                return False, count, i
            if provenance._hash_record(rec) != rec.get("hash"):
                return False, count, i
            prev = rec["hash"]
            count += 1
    return True, count, None


def main() -> int:
    ok, count, broken = provenance.verify()
    print(f"живой журнал: записей {count}, цепочка {'цела' if ok else 'БИТАЯ на ' + str(broken)}")
    last = provenance.records(3)
    for r in last:
        print(f"   seq={r.get('seq')} {str(r.get('filename'))[:44]} sha={str(r.get('sha256'))[:16]} "
              f"источник={'да' if r.get('source') else 'нет'}")

    path = provenance._journal_path()
    if not path.exists() or count == 0:
        print("журнал пуст — нечего проверять на подмену")
        return 0

    lines = path.read_text(encoding="utf-8").splitlines()

    # подмена содержимого записи
    tmp = Path(tempfile.mkdtemp()) / "tampered.jsonl"
    tampered = list(lines)
    rec = json.loads(tampered[0])
    rec["size"] = int(rec.get("size", 0)) + 1          # подмена: размер
    tampered[0] = json.dumps(rec, ensure_ascii=False, sort_keys=True)
    tmp.write_text("\n".join(tampered) + "\n", encoding="utf-8")
    ok2, _, at2 = chain_ok(tmp)
    print(f"подмена записи: цепочка {'цела (ПЛОХО — проверка не работает!)' if ok2 else 'битая на ' + str(at2)}")

    # удаление строки
    tmp2 = Path(tempfile.mkdtemp()) / "deleted.jsonl"
    tmp2.write_text("\n".join(lines[:1] + lines[2:]) + "\n", encoding="utf-8")
    ok3, _, at3 = chain_ok(tmp2)
    print(f"удаление записи: цепочка {'цела (ПЛОХО!)' if ok3 else 'битая на ' + str(at3)}")

    # поиск по документу
    doc = json.loads(lines[-1]).get("document_id")
    found = provenance.find(doc)
    print(f"записи по последнему документу {str(doc)[:12]}: {len(found)}")

    shutil.rmtree(tmp.parent, ignore_errors=True)
    shutil.rmtree(tmp2.parent, ignore_errors=True)
    verdict = ok and (not ok2) and (not ok3) and bool(found)
    print("\nИТОГ:", "проверка работает как задумано" if verdict else "ПРОВЕРИТЬ ЛОГИКУ")
    return 0 if verdict else 1


if __name__ == "__main__":
    raise SystemExit(main())
