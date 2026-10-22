#!/usr/bin/env bash
# Télécharge les poids dans <WS_DIR>/models (./models si WS_DIR n'est pas défini dans .env) — à faire avec internet.
#   ./scripts/01-telecharger-modeles.sh           # PC de test (~25 Go)
#   ./scripts/01-telecharger-modeles.sh --spark   # DGX Spark (~82 Go)
# Ensuite tout tourne hors ligne (HF_HUB_OFFLINE=1 dans les conteneurs).
# Pour le réseau fermé : copie le dossier models sur disque externe, avec sha256sum.txt pour vérifier.
set -euo pipefail
cd "$(dirname "$0")/.."

# Dossier des données : WS_DIR du .env (sur le Spark). Sans .env : ./models, comme sur le PC de test.
WS_DIR="$(grep -s -E '^WS_DIR=' .env | cut -d= -f2- || true)"
if [ "${1:-}" = "--spark" ] && [ -z "$WS_DIR" ]; then
  echo "Crée d'abord le .env du Spark : ./scripts/preparer-env-spark.sh <nom-du-serveur> [dossier-des-données]"
  exit 1
fi
MODELS="${WS_DIR:-.}/models"
mkdir -p "$MODELS"
echo "==> Modèles dans $(realpath "$MODELS")"
# hf CLI : celui du .venv s'il existe, sinon via un conteneur Python jetable
if [ -x .venv/bin/hf ]; then
  DEST="$MODELS"          # chemin de destination vu par hf
  hf() { .venv/bin/hf "$@"; }
else
  DEST="/work/models"     # chemin vu depuis le conteneur de secours (voir le -v ci-dessous)
  hf() { docker run --rm --user "$(id -u):$(id -g)" -e HOME=/tmp -v "$(realpath "$MODELS"):/work/models" -w /work \
           python:3.12-slim sh -c 'pip install -q --user huggingface_hub && /tmp/.local/bin/hf "$@"' hf "$@"; }
fi

hf download BAAI/bge-reranker-v2-m3 --local-dir "$DEST"/bge-reranker-v2-m3 --exclude 'assets/*'

if [ "${1:-}" = "--spark" ]; then
  # Spark : embeddings servis par vLLM -> poids PyTorch (pas la version ONNX)
  hf download BAAI/bge-m3 --local-dir "$DEST"/bge-m3 --exclude 'onnx/*' --exclude 'imgs/*' --exclude '*.jpg'
  # Modèle principal : profil par défaut, Nemotron 3.5 Super VL (MoE 120B, 12B actifs, NVFP4, 75 Go + brouillon 1,3 Go).
  # Autres modèles : ./scripts/modeles.py lister | chercher | ajouter | utiliser
  python3 scripts/modeles.py telecharger nemotron-3.5-super-vl-120b-a12b-nvfp4
else
  # PC : embeddings sur CPU via TEI -> version ONNX (~2.3 Go)
  hf download BAAI/bge-m3 --local-dir "$DEST"/bge-m3 --exclude 'pytorch_model.bin' --exclude 'imgs/*' --exclude '*.jpg' --exclude '*.pt'
  # LLM principal (8 Go de VRAM) : Qwen3.5-4B quantifié AWQ 4 bits, ~4 Go
  hf download cyankiwi/Qwen3.5-4B-AWQ-4bit --local-dir "$DEST"/Qwen3.5-4B-AWQ
  # Même modèle en BF16 (~9 Go) : base pour l'entraînement LoRA sur les normes internes
  hf download Qwen/Qwen3.5-4B --local-dir "$DEST"/Qwen3.5-4B
fi

echo "==> Empreintes (pour vérifier après transfert : cd $MODELS && sha256sum -c sha256sum.txt)"
(cd "$MODELS" && find . -path ./sha256sum.txt -prune -o -type f -not -path '*/.cache/*' -print0 \
  | xargs -0 sha256sum > sha256sum.txt)
du -sh "$MODELS"/*
