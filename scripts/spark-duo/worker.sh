#!/usr/bin/env bash
# Spark 2 : rejoint le cluster Ray du Spark 1 et prête son GPU. Rien d'autre ne tourne ici.
# Si le Spark 1 n'est pas prêt (ou redémarre), le conteneur s'arrête et Docker le relance.
set -euo pipefail
: "${DUO_IP_TETE:?DUO_IP_TETE manquant dans .env}"
: "${DUO_IP_WORKER:?DUO_IP_WORKER manquant dans .env}"

# Ray n'est pas dans toutes les images vLLM (absent de l'image officielle vllm/vllm-openai)
command -v ray >/dev/null || { echo "ERREUR : Ray absent de l'image ${VLLM_IMAGE:-vLLM}. Le duo a besoin d'une image avec Ray (ex. eugr/spark-vllm) : changer VLLM_IMAGE dans le .env des deux Spark." >&2; sleep 60; exit 1; }

exec ray start --address="$DUO_IP_TETE:${DUO_RAY_PORT:-6379}" --node-ip-address="$DUO_IP_WORKER" \
  --disable-usage-stats --block
