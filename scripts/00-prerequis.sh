#!/usr/bin/env bash
# Prérequis système (à lancer UNE fois).
# PC de test (x86, avec sudo) :
#   1. driver NVIDIA 595 (recommandé par Ubuntu pour ce GPU) (vLLM récent est compilé en CUDA 13 -> driver 580 minimum)
#   2. NVIDIA Container Toolkit (accès GPU depuis Docker)
#   3. plugin "docker compose"
#   Un redémarrage est nécessaire après la mise à jour du driver.
# DGX Spark (arm64) : DGX OS fournit déjà pilote, Docker, compose et toolkit, et Docker est déjà configuré.
#   Rien n'est installé ni modifié, pas de sudo : le script vérifie seulement, et prépare l'outil de téléchargement hf.
set -euo pipefail
cd "$(dirname "$0")/.."

if [ "$(uname -m)" = "aarch64" ]; then
  echo "==> DGX Spark : vérifications (rien n'est installé)"
  ok=1
  verifier() { if eval "$2" >/dev/null 2>&1; then echo "  [OK]    $1"; else echo "  [ECHEC] $1 : $3"; ok=0; fi; }
  verifier "pilote NVIDIA" "nvidia-smi" "nvidia-smi ne répond pas"
  verifier "docker sans sudo" "docker info" "ajoute le compte au groupe docker (sudo usermod -aG docker \$USER, puis reconnecte-toi)"
  verifier "docker compose" "docker compose version" "plugin compose absent"
  verifier "python3" "python3 --version" "python3 absent"
  nvidia-smi --query-gpu=name,driver_version --format=csv,noheader 2>/dev/null | sed 's/^/  GPU : /'
  echo "  Stockage Docker : $(docker info --format '{{.DockerRootDir}}' 2>/dev/null || echo '?')"
  # Outil de téléchargement hf dans .venv. Si le module venv manque, les scripts passent par un conteneur Python : pas grave.
  if [ ! -x .venv/bin/hf ]; then
    if python3 -m venv .venv >/dev/null 2>&1 && .venv/bin/pip install -q --upgrade pip huggingface_hub; then
      echo "  [OK]    outil hf installé dans .venv"
    else
      rm -rf .venv
      echo "  [INFO]  pas de module venv : les téléchargements passeront par un conteneur Python (aucune action requise)"
    fi
  else
    echo "  [OK]    outil hf déjà présent dans .venv"
  fi
  [ "$ok" = 1 ] || { echo "Corrige les lignes [ECHEC] avant de continuer."; exit 1; }
  echo "OK. Étape suivante : ./scripts/preparer-env-spark.sh <nom-du-serveur> /home/data/ws"
  exit 0
fi

sudo apt-get update
echo "==> Driver NVIDIA 595"
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
# docker-compose-v2 (Ubuntu) entre en conflit avec le plugin de Docker CE : seulement s'il manque
docker compose version >/dev/null 2>&1 || sudo apt-get install -y docker-compose-v2
sudo apt-get install -y python3.12-venv python3-pip

echo "==> Outils Python côté hôte (.venv) : hf CLI (téléchargement des modèles)"
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install huggingface_hub

echo
echo "OK. Redémarre la machine, puis vérifie :"
echo "  nvidia-smi                       (Driver Version: 595.x)"
echo "  docker run --rm --gpus all ubuntu nvidia-smi"
