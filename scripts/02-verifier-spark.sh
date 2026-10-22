#!/usr/bin/env bash
# Vérifie la stack une fois démarrée (sur le Spark ou le PC) :
#   ./scripts/02-verifier-spark.sh
# Teste via LiteLLM : chat, appel d'outil, débit de génération, embeddings ; puis le reranker en direct.
set -euo pipefail
cd "$(dirname "$0")/.."
KEY=$(grep -E '^LITELLM_MASTER_KEY=' .env | cut -d= -f2-)

KEY="$KEY" python3 -I - <<'EOF'
import json, os, time, urllib.request

def post(path, body):
    req = urllib.request.Request("http://127.0.0.1:4000" + path, json.dumps(body).encode(),
                                 {"Content-Type": "application/json", "Authorization": "Bearer " + os.environ["KEY"]})
    with urllib.request.urlopen(req, timeout=600) as r:
        return json.load(r)

def etape(nom, f):
    try:
        print(f"[OK]    {nom} : {f()}")
    except Exception as e:
        print(f"[ECHEC] {nom} : {e}")

def chat():
    r = post("/v1/chat/completions", {"model": "qwen-local", "max_tokens": 2048,
             "messages": [{"role": "user", "content": "Réponds juste : bonjour"}]})
    m = r["choices"][0]["message"]
    # Modèles à raisonnement (Nemotron) : la réponse peut être vide si le raisonnement a tout consommé
    return ((m.get("content") or "").strip() or "(réponse vide, raisonnement : " + (m.get("reasoning_content") or "")[:40] + "…)")[:60]

def outil():
    r = post("/v1/chat/completions", {"model": "qwen-local", "max_tokens": 2048,
             "messages": [{"role": "user", "content": "Quelle heure est-il à Paris ?"}],
             "tools": [{"type": "function", "function": {"name": "heure", "description": "Heure d'une ville",
                        "parameters": {"type": "object", "properties": {"ville": {"type": "string"}}, "required": ["ville"]}}}]})
    appels = r["choices"][0]["message"].get("tool_calls") or []
    if not appels:
        raise RuntimeError("aucun appel d'outil (vérifier VLLM_TOOL_PARSER)")
    return appels[0]["function"]["name"] + appels[0]["function"]["arguments"]

def debit():
    t = time.time()
    r = post("/v1/chat/completions", {"model": "qwen-local", "max_tokens": 512,
             "messages": [{"role": "user", "content": "Écris une fonction Python qui trie une liste, avec des tests."}]})
    n = r["usage"]["completion_tokens"]
    return f"{n} tokens en {time.time() - t:.1f} s = {n / (time.time() - t):.0f} tok/s (1 requête, préremplissage compris)"

def embeddings():
    r = post("/v1/embeddings", {"model": "bge-m3", "input": ["norme ISO 7816"]})
    return f"vecteur de {len(r['data'][0]['embedding'])} dimensions"

etape("chat", chat)
etape("appel d'outil", outil)
etape("débit", debit)
etape("embeddings", embeddings)
EOF

# Le reranker n'est pas exposé par LiteLLM : test depuis le conteneur mcp-docs, comme en usage réel
docker compose exec -T mcp-docs python -c "
import server
r = server.rerank('lecture de carte sans contact', ['ISO 14443 : cartes sans contact', 'recette de cuisine'])
print('[OK]    reranker :', sorted(r, key=lambda x: -x['score'])[0])
" 2>/dev/null || echo "[ECHEC] reranker : voir docker compose logs tei-rerank"
