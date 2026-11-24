#!/usr/bin/env bash
# Crée une clé API LiteLLM personnelle.  Usage : ./scripts/creer-cle-utilisateur.sh prenom.nom
# Chaque personne utilise SA clé dans Open WebUI / OpenCode -> suivi et quotas par personne.
set -euo pipefail
cd "$(dirname "$0")/.."
# Ne PAS faire « source .env » : le .env contient des lignes (options vLLM) que bash ne sait pas lire
LITELLM_MASTER_KEY="$(grep -E '^LITELLM_MASTER_KEY=' .env | cut -d= -f2-)"
[ -n "$LITELLM_MASTER_KEY" ] || { echo "LITELLM_MASTER_KEY introuvable dans .env"; exit 1; }
UTILISATEUR="${1:?usage: $0 <identifiant>}"
curl -s http://localhost:4000/key/generate \
  -H "Authorization: Bearer $LITELLM_MASTER_KEY" -H "Content-Type: application/json" \
  -d "{\"user_id\": \"$UTILISATEUR\", \"key_alias\": \"$UTILISATEUR\",
       \"models\": [\"qwen-local\", \"normes\", \"bge-m3\"],
       \"max_parallel_requests\": 2, \"rpm_limit\": 60}" \
  | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('key', d))"
