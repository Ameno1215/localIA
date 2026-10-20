#!/usr/bin/env bash
# Installe et configure OpenCode (agent de code dans le terminal) sur un poste de développeur.
#
#   ./scripts/installer-opencode.sh                                   # PC de test (serveur = ce PC)
#   ./scripts/installer-opencode.sh --serveur https://spark1.lan --contexte 131072 \
#       --mcp https://spark1.lan/mcp --jeton-mcp <jeton d'équipe> --ca ~/spark-ca.crt
#
# Relançable sans risque : il met à jour la config. La clé LiteLLM est demandée si elle n'est pas déjà enregistrée.
set -euo pipefail
cd "$(dirname "$0")/.."

VERSION_OPENCODE="1.18.33"
SERVEUR="http://localhost:4000"
CONTEXTE="65536"
MCP=""
JETON_MCP=""
CA=""
while [ $# -gt 0 ]; do
  case "$1" in
    --serveur) SERVEUR="${2%/}"; shift 2 ;;
    --contexte) CONTEXTE="$2"; shift 2 ;;
    --mcp) MCP="$2"; shift 2 ;;
    --jeton-mcp) JETON_MCP="$2"; shift 2 ;;
    --ca) CA="$(realpath "$2")"; shift 2 ;;
    -h|--help) sed -n 2,8p "$0"; exit 0 ;;
    *) echo "Option inconnue : $1"; exit 1 ;;
  esac
done
# Par défaut, le MCP est sur la même machine que LiteLLM, port 9901
[ -n "$MCP" ] || MCP="$(echo "$SERVEUR" | sed -E 's#:[0-9]+$##'):9901/mcp"

echo "==> Node.js"
export NVM_DIR="$HOME/.nvm"
if [ -s "$NVM_DIR/nvm.sh" ]; then . "$NVM_DIR/nvm.sh"; fi
if ! command -v node >/dev/null || [ "$(node -p 'process.versions.node.split(".")[0]')" -lt 20 ]; then
  echo "Node.js >= 20 est requis. Installe-le (par exemple via nvm), puis relance ce script."
  exit 1
fi
node --version

echo "==> OpenCode $VERSION_OPENCODE"
if [ "$(opencode --version 2>/dev/null || true)" != "$VERSION_OPENCODE" ]; then
  npm install -g "opencode-ai@$VERSION_OPENCODE"
fi
opencode --version
command -v rg >/dev/null || echo "   Conseil : installe ripgrep (sudo apt install ripgrep), sinon OpenCode le télécharge."

echo "==> Clé LiteLLM et variables d'environnement"
ENVFILE="$HOME/.config/ia-locale/env"
mkdir -p "$(dirname "$ENVFILE")"
CLE="$(grep -s '^export LITELLM_API_KEY=' "$ENVFILE" | cut -d= -f2- || true)"
if [ -z "$CLE" ]; then
  read -rsp "Colle ta clé LiteLLM (sk-..., créée avec ./scripts/creer-cle-utilisateur.sh) : " CLE; echo
fi
case "$CLE" in sk-*) ;; *) echo "La clé doit commencer par sk-"; exit 1 ;; esac
# Garder le jeton MCP et le certificat déjà enregistrés si on ne les repasse pas
[ -n "$JETON_MCP" ] || JETON_MCP="$(grep -s '^export MCP_TOKEN=' "$ENVFILE" | cut -d= -f2- || true)"
[ -n "$CA" ] || CA="$(grep -s '^export NODE_EXTRA_CA_CERTS=' "$ENVFILE" | cut -d= -f2- | cut -d' ' -f1 || true)"
umask 077
cat > "$ENVFILE" <<EOF
# Généré par installer-opencode.sh
export LITELLM_API_KEY=$CLE
# Jeton d'équipe pour la recherche dans les normes (vide sur le PC de test : pas d'authentification)
export MCP_TOKEN=$JETON_MCP
# Pas d'appels réseau superflus (préparation du réseau fermé)
export OPENCODE_DISABLE_AUTOUPDATE=1
export OPENCODE_DISABLE_MODELS_FETCH=1
export OPENCODE_DISABLE_DEFAULT_PLUGINS=1
export OPENCODE_DISABLE_LSP_DOWNLOAD=1
EOF
[ -z "$CA" ] || echo "export NODE_EXTRA_CA_CERTS=$CA   # certificat HTTPS interne du Spark" >> "$ENVFILE"
umask 022
grep -q 'ia-locale/env' "$HOME/.bashrc" || echo '[ -f "$HOME/.config/ia-locale/env" ] && . "$HOME/.config/ia-locale/env"' >> "$HOME/.bashrc"

echo "==> Vérification du serveur ($SERVEUR)"
if curl -sf -m 10 ${CA:+--cacert "$CA"} "$SERVEUR/v1/models" -H "Authorization: Bearer $CLE" | grep -q qwen-local; then
  echo "   OK : la clé fonctionne et qwen-local est disponible."
else
  echo "   ATTENTION : $SERVEUR ne répond pas, ou la clé est refusée. La config est écrite quand même."
fi

echo "==> Configuration OpenCode"
mkdir -p "$HOME/.config/opencode"
sed -e "s#__SERVEUR__#$SERVEUR#" -e "s#__CONTEXTE__#$CONTEXTE#g" -e "s#__MCP__#$MCP#" \
  clients/opencode.json > "$HOME/.config/opencode/opencode.json"
echo "   ~/.config/opencode/opencode.json (serveur $SERVEUR, contexte $CONTEXTE, MCP $MCP)"

echo
echo "Terminé. Ouvre un nouveau terminal (ou : source ~/.bashrc), puis :"
echo "   cd ton-projet"
echo "   cp \"$PWD/clients/AGENTS.md\" .     # consignes de l'agent, à adapter"
echo "   opencode"
