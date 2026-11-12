"""pvc-plumber stand-in for the Kyverno CEL migration rehearsal (staging only).

Answers GET /exists/<namespace>/<pvc> with the same JSON shape as production's
pvc-plumber 3.1.0, e.g.
  {"exists":true,"decision":"restore","authoritative":true,"namespace":"ns",
   "pvc":"data","backend":"kopia-s3","source":"data-backup@ns:/data"}

The answer is chosen by a word in the PVC name, so every policy branch can be
exercised on purpose:
  *restore*  -> backup exists, decision=restore, authoritative
  *nonauth*  -> authoritative=false (oracle unsure)
  *error*    -> authoritative=false plus an error string
  *slow*     -> restore answer after 25s (past the 20s webhook timeout)
  anything else -> no backup, decision=fresh, authoritative
"""
import json
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def answer(namespace, pvc):
    base = {"namespace": namespace, "pvc": pvc, "backend": "kopia-s3"}
    if "slow" in pvc:
        time.sleep(25)
    if "error" in pvc:
        return {**base, "exists": False, "decision": "unknown", "authoritative": False,
                "error": "stub: simulated backend error"}
    if "nonauth" in pvc:
        return {**base, "exists": False, "decision": "unknown", "authoritative": False}
    if "restore" in pvc or "slow" in pvc:
        return {**base, "exists": True, "decision": "restore", "authoritative": True,
                "source": f"{pvc}-backup@{namespace}:/data"}
    return {**base, "exists": False, "decision": "fresh", "authoritative": True}


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        parts = self.path.strip("/").split("/")
        if self.path in ("/healthz", "/readyz"):
            body, code = {"status": "ok"}, 200
        elif len(parts) == 3 and parts[0] == "exists":
            body, code = answer(parts[1], parts[2]), 200
        else:
            body, code = {"error": "not found"}, 404
        raw = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


ThreadingHTTPServer(("", 8080), Handler).serve_forever()
