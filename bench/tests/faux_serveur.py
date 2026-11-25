"""Faux serveur pour tester la suite sans GPU : imite vLLM, LiteLLM, TEI et Qdrant sur un seul port.

  python3 -m bench.tests.faux_serveur --port 8765

- /v1/chat/completions en streaming (SSE), avec usage, appels d'outils, 400 au-delà du contexte,
  429 au-delà de max_parallel_requests par clé, en-tête x-litellm-overhead-duration-ms ;
- /v1/models (max_model_len), /metrics (jauges vLLM), /key/generate, /key/delete ;
- /embed, /rerank (TEI), /collections/<c> et /collections/<c>/points/query (Qdrant) ;
- /__panne?duree=S : les requêtes de chat échouent en 503 pendant S secondes.
"""
import argparse
import hashlib
import json
import math
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

CONTEXTE = 40000
DELAI_TOKEN = 0.002
DIM = 16
ETAT = {"en_cours": 0, "attente": 0, "cles": {}, "par_cle": {}, "panne_jusqu_a": 0.0,
        "hits": 0, "requetes_cache": 0}
VERROU = threading.Lock()
DOCS = [
    {"source": "NORME-DEV-001-conventions-python.md", "page": None,
     "texte": "La couverture minimale exigée est de 80 % par module. Tests pytest."},
    {"source": "NORME-DEV-001-conventions-python.md", "page": None,
     "texte": "print() est interdit dans le code livré ; utiliser logging. Niveaux INFO WARNING ERROR."},
    {"source": "autre.md", "page": 3, "texte": "Sans rapport : recette de cuisine."},
]


def _vecteur(texte):
    h = hashlib.sha256(texte.lower().encode()).digest()
    v = [b / 255 - 0.5 for b in h[:DIM]]
    n = math.sqrt(sum(x * x for x in v)) or 1
    return [x / n for x in v]


class Gestion(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _json(self, statut, donnees, entetes=None):
        corps = json.dumps(donnees).encode()
        self.send_response(statut)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(corps)))
        for k, v in (entetes or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(corps)

    def _corps(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    def do_GET(self):
        u = urlsplit(self.path)
        if u.path == "/v1/models":
            return self._json(200, {"data": [{"id": "faux", "max_model_len": CONTEXTE},
                                             {"id": "faux-fast", "max_model_len": CONTEXTE}]})
        if u.path == "/metrics":
            with VERROU:
                kv = min(1.0, ETAT["en_cours"] / 8)
                txt = (f"# HELP x\nvllm:kv_cache_usage_perc{{engine=\"0\"}} {kv}\n"
                       f"vllm:num_requests_running{{engine=\"0\"}} {ETAT['en_cours']}\n"
                       f"vllm:num_requests_waiting{{engine=\"0\"}} 0\n"
                       f"vllm:num_preemptions_total{{engine=\"0\"}} 0\n"
                       f"vllm:prefix_cache_hits_total{{engine=\"0\"}} {ETAT['hits']}\n"
                       f"vllm:prefix_cache_queries_total{{engine=\"0\"}} {ETAT['requetes_cache']}\n").encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(txt)))
            self.end_headers()
            self.wfile.write(txt)
            return
        if u.path == "/__panne":
            duree = float(parse_qs(u.query).get("duree", ["3"])[0])
            ETAT["panne_jusqu_a"] = time.time() + duree
            return self._json(200, {"panne": duree})
        if u.path.startswith("/collections/"):
            return self._json(200, {"result": {"config": {"params": {"vectors": {"size": DIM}}}}})
        if u.path == "/health":
            return self._json(200, {})
        self._json(404, {"error": "inconnu"})

    def do_POST(self):
        u = urlsplit(self.path)
        if u.path == "/v1/chat/completions":
            return self._chat(self._corps())
        if u.path == "/v1/embeddings":
            d = self._corps()
            entrees = d["input"] if isinstance(d["input"], list) else [d["input"]]
            return self._json(200, {"data": [{"index": i, "embedding": _vecteur(t)} for i, t in enumerate(entrees)]})
        if u.path == "/embed":
            return self._json(200, [_vecteur(t) for t in self._corps()["inputs"]])
        if u.path == "/rerank":
            d = self._corps()
            mots = set(d["query"].lower().split())
            return self._json(200, [{"index": i, "score": len(mots & set(t.lower().split())) / (len(mots) or 1)}
                                    for i, t in enumerate(d["texts"])])
        if u.path.endswith("/points/query"):
            d = self._corps()
            limite = d.get("limit", 10)
            pts = [{"id": str(uuid.uuid4()), "score": 0.5, "payload": DOCS[i % len(DOCS)]} for i in range(limite)]
            return self._json(200, {"result": {"points": pts}})
        if u.path == "/key/generate":
            d = self._corps()
            cle = "sk-bench-" + uuid.uuid4().hex[:12]
            ETAT["cles"][cle] = d.get("max_parallel_requests")
            return self._json(200, {"key": cle})
        if u.path == "/key/delete":
            for k in self._corps().get("keys", []):
                ETAT["cles"].pop(k, None)
            return self._json(200, {"deleted_keys": []})
        self._json(404, {"error": "inconnu"})

    def _chat(self, d):
        cle = (self.headers.get("Authorization") or "").removeprefix("Bearer ")
        if time.time() < ETAT["panne_jusqu_a"]:
            return self._json(503, {"error": {"message": "panne simulée"}})
        contenu = json.dumps(d["messages"], ensure_ascii=False)
        n_entree = int(len(contenu.split()) * 1.3)
        if n_entree > CONTEXTE:
            return self._json(400, {"error": {"message": f"maximum context length is {CONTEXTE}"}})
        limite = ETAT["cles"].get(cle)
        with VERROU:
            if limite and ETAT["par_cle"].get(cle, 0) >= limite:
                return self._json(429, {"error": {"message": "max_parallel_requests"}})
            ETAT["par_cle"][cle] = ETAT["par_cle"].get(cle, 0) + 1
            ETAT["en_cours"] += 1
            ETAT["requetes_cache"] += n_entree
            ETAT["hits"] += n_entree // 2
        try:
            max_tok = d.get("max_tokens") or 500
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("x-litellm-overhead-duration-ms", "4.2")
            self.send_header("Connection", "close")
            self.end_headers()

            def envoyer(obj):
                self.wfile.write(b"data: " + json.dumps(obj).encode() + b"\n\n")
                self.wfile.flush()

            base = {"id": "x", "model": d["model"], "object": "chat.completion.chunk"}
            time.sleep(0.01 + n_entree * 2e-6)          # « prefill »
            if d.get("tools"):
                args = json.dumps({"adresse": "0x40021000", "taille": 4})
                envoyer({**base, "choices": [{"index": 0, "delta": {"tool_calls": [
                    {"index": 0, "id": "c1", "type": "function", "function": {"name": "lire_registre", "arguments": ""}}]}}]})
                for i in range(0, len(args), 8):
                    time.sleep(DELAI_TOKEN)
                    envoyer({**base, "choices": [{"index": 0, "delta": {"tool_calls": [
                        {"index": 0, "function": {"arguments": args[i:i + 8]}}]}}]})
                n_sortie, fin = math.ceil(len(args) / 8), "tool_calls"
            else:
                for i in range(max_tok):
                    time.sleep(DELAI_TOKEN)
                    envoyer({**base, "choices": [{"index": 0, "delta": {"content": f" mot{i}"}}]})
                n_sortie, fin = max_tok, "length"
            envoyer({**base, "choices": [{"index": 0, "delta": {}, "finish_reason": fin}]})
            envoyer({**base, "choices": [], "usage": {"prompt_tokens": n_entree, "completion_tokens": n_sortie}})
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
            self.close_connection = True
        finally:
            with VERROU:
                ETAT["par_cle"][cle] -= 1
                ETAT["en_cours"] -= 1


def demarrer(port=0):
    srv = ThreadingHTTPServer(("127.0.0.1", port), Gestion)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--port", type=int, default=8765)
    srv = demarrer(p.parse_args().port)
    print(f"Faux serveur sur http://127.0.0.1:{srv.server_port}")
    threading.Event().wait()
