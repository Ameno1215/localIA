#!/usr/bin/env bash
# Prépare le .env du Spark : copie .env.spark.example, génère les secrets, fixe le nom du serveur.
#   ./scripts/preparer-env-spark.sh spark1.lan        (nom DNS ou adresse IP du Spark sur le réseau)
# N'écrase jamais un .env existant. La clé maître LiteLLM doit commencer par « sk- » : c'est géré ici.
set -euo pipefail
cd "$(dirname "$0")/.."
HOTE="${1:-}"
[ -n "$HOTE" ] || { echo "Usage : $0 <nom-dns-ou-ip-du-spark>"; exit 1; }
DEST="${ENV_FILE:-.env}"
[ ! -e "$DEST" ] || { echo "$DEST existe déjà : rien n'est écrasé (supprime-le ou renomme-le si tu veux repartir de zéro)."; exit 1; }
command -v openssl >/dev/null || { echo "openssl est requis pour générer les secrets."; exit 1; }

cp .env.spark.example "$DEST"
chmod 600 "$DEST"
secret() { openssl rand -hex 16; }
sed -i \
  -e "s|^LITELLM_MASTER_KEY=.*|LITELLM_MASTER_KEY=sk-$(secret)|" \
  -e "s|^POSTGRES_PASSWORD=.*|POSTGRES_PASSWORD=$(secret)|" \
  -e "s|^WEBUI_SECRET_KEY=.*|WEBUI_SECRET_KEY=$(secret)|" \
  -e "s|^MCP_TOKEN=.*|MCP_TOKEN=$(secret)|" \
  -e "s|^SPARK_HOSTNAME=.*|SPARK_HOSTNAME=$HOTE|" \
  "$DEST"
echo "$DEST créé (droits 600) : secrets générés, serveur « $HOTE »."
echo "Le jeton d'équipe MCP_TOKEN est à donner aux développeurs : grep MCP_TOKEN $DEST"
