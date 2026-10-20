#!/usr/bin/env bash
# Télécharge les poids dans ./models (à faire avec internet).
#   ./scripts/01-telecharger-modeles.sh           # PC de test (~25 Go)
#   ./scripts/01-telecharger-modeles.sh --spark   # DGX Spark (~30 Go)
# Ensuite tout tourne hors ligne (HF_HUB_OFFLINE=1 dans les conteneurs).
# Pour le réseau fermé : copie ./models sur disque externe, avec sha256sum.txt pour vérifier.
set -euo pipefail
cd "$(dirname "$0")/.."
# hf CLI : celui du .venv s'il existe, sinon via un conteneur Python jetable
if [ -x .venv/bin/hf ]; then
  hf() { .venv/bin/hf "$@"; }
else
  hf() { docker run --rm --user "$(id -u):$(id -g)" -e HOME=/tmp -v "$PWD/models:/work/models" -w /work \
           python:3.12-slim sh -c 'pip install -q --user huggingface_hub && /tmp/.local/bin/hf "$@"' hf "$@"; }
fi

hf download BAAI/bge-reranker-v2-m3 --local-dir models/bge-reranker-v2-m3 --exclude 'assets/*'

if [ "${1:-}" = "--spark" ]; then
  # Spark : embeddings servis par vLLM -> poids PyTorch (pas la version ONNX)
  hf download BAAI/bge-m3 --local-dir models/bge-m3 --exclude 'onnx/*' --exclude 'imgs/*' --exclude '*.jpg'
  # Modèle principal : profil NVIDIA pour DGX Spark (MoE 35B, 3B actifs, NVFP4), ~23 Go
  hf download nvidia/Qwen3.6-35B-A3B-NVFP4 --local-dir models/Qwen3.6-35B-A3B-NVFP4
else
  # PC : embeddings sur CPU via TEI -> version ONNX (~2.3 Go)
  hf download BAAI/bge-m3 --local-dir models/bge-m3 --exclude 'pytorch_model.bin' --exclude 'imgs/*' --exclude '*.jpg' --exclude '*.pt'
  # LLM principal (8 Go de VRAM) : Qwen3.5-4B quantifié AWQ 4 bits, ~4 Go
  hf download cyankiwi/Qwen3.5-4B-AWQ-4bit --local-dir models/Qwen3.5-4B-AWQ
  # Même modèle en BF16 (~9 Go) : base pour l'entraînement LoRA sur les normes internes
  hf download Qwen/Qwen3.5-4B --local-dir models/Qwen3.5-4B
fi

echo "==> Empreintes (pour vérifier après transfert : cd models && sha256sum -c sha256sum.txt)"
(cd models && find . -path ./sha256sum.txt -prune -o -type f -not -path '*/.cache/*' -print0 \
  | xargs -0 sha256sum > sha256sum.txt)
du -sh models/*
