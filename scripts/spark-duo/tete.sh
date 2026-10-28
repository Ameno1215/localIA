#!/usr/bin/env bash
# Spark 1 (tête du cluster) : démarre Ray, attend le GPU du Spark 2, puis lance vLLM sur les deux GPU.
# Appelé par docker-compose.spark-duo.yml ; reçoit en arguments la commande « vllm serve » habituelle.
set -euo pipefail
: "${DUO_IP_TETE:?DUO_IP_TETE manquant dans .env}"

ray start --head --node-ip-address="$DUO_IP_TETE" --port="${DUO_RAY_PORT:-6379}" \
  --include-dashboard=false --disable-usage-stats

# Sans le Spark 2, vLLM attendrait indéfiniment un 2e GPU : on l'attend ici, avec un message clair
until python3 -c 'import ray, sys; ray.init(address="auto", logging_level="ERROR"); sys.exit(0 if ray.cluster_resources().get("GPU", 0) >= 2 else 1)' 2>/dev/null; do
  echo "Attente du Spark 2 (ray-worker sur $DUO_IP_TETE:${DUO_RAY_PORT:-6379})…"
  sleep 10
done
echo "Cluster prêt : 2 GPU. Démarrage de vLLM (tenseurs répartis sur les deux Spark)."

exec vllm serve "$@" --tensor-parallel-size 2 --distributed-executor-backend ray
