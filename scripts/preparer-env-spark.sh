#!/usr/bin/env bash
# Prépare le .env du Spark : copie .env.spark.example, génère les secrets, fixe le nom du serveur.
#   ./scripts/preparer-env-spark.sh spark1.lan [/home/data/ws]
#       1er argument : nom DNS ou adresse IP du Spark sur le réseau
#       2e argument  : dossier des DONNÉES des conteneurs (modèles, caches, bases, documents). Défaut : ./ws
# N'écrase jamais un .env existant. La clé maître LiteLLM doit commencer par « sk- » : c'est géré ici.
set -euo pipefail
cd "$(dirname "$0")/.."
HOTE="${1:-}"
WS="${2:-./ws}"
[ -n "$HOTE" ] || { echo "Usage : $0 <nom-dns-ou-ip-du-spark> [dossier-des-données]"; exit 1; }
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
  -e "s|^WS_DIR=.*|WS_DIR=$WS|" \
  "$DEST"

# Dossiers de données, créés par TON utilisateur (sinon Docker les crée au nom de root et les téléchargements échouent)
for d in models loras cache/vllm cache/flashinfer cache/triton cache/b12x data workspace docs-internes postgres qdrant open-webui caddy; do
  mkdir -p "$WS/$d" || { echo "Impossible de créer $WS/$d : crée $WS avec les bons droits (sudo mkdir -p $WS && sudo chown \$USER $WS), puis relance."; rm -f "$DEST"; exit 1; }
done
# Norme d'exemple du dépôt, si aucun document n'est encore en place
[ -n "$(ls -A "$WS/docs-internes" 2>/dev/null)" ] || cp docs-internes/*.md "$WS/docs-internes/" 2>/dev/null || true
echo "Dossier des données : $(realpath "$WS")"
echo "$DEST créé (droits 600) : secrets générés, serveur « $HOTE »."
echo "Le jeton d'équipe MCP_TOKEN est à donner aux développeurs : grep MCP_TOKEN $DEST"
