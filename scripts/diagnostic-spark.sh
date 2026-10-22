#!/usr/bin/env bash
# Rassemble de quoi comprendre un problème sur le Spark, dans un seul fichier à transmettre :
#   ./scripts/diagnostic-spark.sh
# Ne contient AUCUN secret : le .env n'est lu que pour les noms de variables et les réglages vLLM.
cd "$(dirname "$0")/.."
OUT="diagnostic-spark-$(date +%Y%m%d-%H%M).txt"
sec() { printf '\n===== %s =====\n' "$1"; }
{
  sec "date, machine"; date; uname -a; uptime -p
  sec "version du projet"; git log --oneline -3 2>/dev/null; git branch --show-current 2>/dev/null
  sec "GPU / pilote"; nvidia-smi 2>&1 | head -25
  sec "mémoire"; free -h; df -h . | tail -2
  sec "docker"; docker --version; docker compose version
  sec "conteneurs"; docker compose ps -a --format 'table {{.Service}}\t{{.Status}}\t{{.Image}}' 2>&1
  sec "images"; docker images --format '{{.Repository}}:{{.Tag}}  {{.Size}}' | head -30
  sec "réglages vLLM du .env (sans secrets)"
  grep -E '^(COMPOSE_FILE|VLLM_|MCP_OUTILS_CODE|SPARK_HOSTNAME)' .env 2>/dev/null | sed -E 's/^(MCP_TOKEN)=.*/\1=…/'
  sec "variables présentes dans le .env (noms seulement)"; sed -E 's/=.*/=…/' .env 2>/dev/null | grep -v '^#' | grep -v '^$'
  sec "modèles téléchargés"; WS="$(grep -s -E '^WS_DIR=' .env | cut -d= -f2-)"; echo "WS_DIR=${WS:-<non défini>}"; ls "${WS:-.}/models" 2>&1; df -h "${WS:-.}" | tail -1
  sec "profil actif"; python3 scripts/modeles.py lister 2>&1 | head -15
  for s in vllm tei-embed tei-rerank mcp-docs caddy; do sec "journal $s (150 dernières lignes)"; docker compose logs --no-color --tail 150 "$s" 2>&1; done
} > "$OUT" 2>&1
echo "Diagnostic écrit dans $OUT ($(wc -l < "$OUT") lignes). Relis-le avant de le transmettre."
