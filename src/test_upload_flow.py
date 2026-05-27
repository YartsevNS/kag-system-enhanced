#!/usr/bin/env python3
"""Test upload flow via internal API."""
import json, subprocess, time

r = subprocess.run([
    "curl", "-sf", "-X", "POST", "http://localhost:8000/api/v1/auth/login",
    "-H", "Content-Type: application/json",
    "-d", '{"username":"admin","password":"Kagsecret569!"}'
], capture_output=True, text=True, timeout=10)
data = json.loads(r.stdout)
tok = data["access_token"]
print(f"Token OK: len={len(tok)}")

r = subprocess.run([
    "curl", "-sf", "-X", "POST", "http://localhost:8000/api/v1/upload/",
    "-H", "Authorization: Bearer " + tok,
    "-F", "file=@/app/src/__init__.py;type=text/x-python"
], capture_output=True, text=True, timeout=30)
d = json.loads(r.stdout)
doc_id = d.get("document_id", "?")
print(f"Upload: status={d.get('status','?')} progress={d.get('progress',0)} id={doc_id[:20]}")

for i in range(15):
    time.sleep(3)
    r = subprocess.run([
        "curl", "-sf", "http://localhost:8000/api/v1/upload/list",
        "-H", "Authorization: Bearer " + tok
    ], capture_output=True, text=True, timeout=10)
    d = json.loads(r.stdout)
    docs = d.get("documents", [])
    for doc in docs:
        if doc.get("document_id") == doc_id:
            p = doc.get("progress", 0)
            s = doc.get("status", "?")
            print(f"  [{i*3}s] {doc['filename']}: {s} ({p}%)")
            if s in ("completed", "failed"):
                print("DONE")
                exit(0)
            break
print("TIMEOUT - still processing")
