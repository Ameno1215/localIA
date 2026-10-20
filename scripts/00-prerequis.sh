#!/usr/bin/env bash
# Prérequis système (à lancer UNE fois, avec sudo) :
#   1. driver NVIDIA 595 (recommandé par Ubuntu pour ce GPU) (vLLM récent est compilé en CUDA 12.9 -> driver 575 minimum)
#   2. NVIDIA Container Toolkit (accès GPU depuis Docker)
#   3. plugin "docker compose"
# Un redémarrage est nécessaire après la mise à jour du driver.
set -euo pipefail

echo "==> Driver NVIDIA 595"
sudo apt-get update
sudo apt-get install -y nvidia-driver-595

echo "==> NVIDIA Container Toolkit"
curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey \
  | sudo gpg --dearmor --yes -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
curl -fsSL https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list \
  | sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' \
  | sudo tee /etc/apt/sources.list.d/nvidia-container-toolkit.list >/dev/null
sudo apt-get update
sudo apt-get install -y nvidia-container-toolkit
sudo nvidia-ctk runtime configure --runtime=docker
sudo systemctl restart docker

echo "==> docker compose + Python venv/pip"
sudo apt-get install -y docker-compose-v2 python3.12-venv python3-pip

echo "==> Outils Python côté hôte (.venv) : hf CLI + NeMo Agent Toolkit"
cd "$(dirname "$0")/.."
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r agent/requirements.txt

echo
echo "OK. Redémarre la machine, puis vérifie :"
echo "  nvidia-smi                       (Driver Version: 595.x)"
echo "  docker run --rm --gpus all ubuntu nvidia-smi"
