# Image vLLM pour NVIDIA Nemotron 3.5 Bolt (instructions de la fiche Hugging Face du modèle).
# Construite UNE fois avec internet :  ./scripts/modeles.py utiliser bolt-74b-a9b-24gb
# Ensuite tout tourne hors ligne : noyaux, plugin et parser sont dans l'image.
# Version vLLM figée, exigée par la fiche du modèle ("cette version ou plus récente").
FROM vllm/vllm-openai:nightly-77154ca6116ae34cbfe314beef1016bde9b63989

# Noyaux b12x (bibliothèque de noyaux pour DGX Spark / RTX 6000).
# « vllm download-kernels » exige un GPU, absent pendant un build : modeles.py le lance
# ensuite dans un conteneur avec le GPU et enregistre le résultat dans l'image.
RUN pip install --no-cache-dir b12x

# Parser de raisonnement fourni avec le modèle (chargé par --reasoning-parser-plugin)
RUN python3 -c "import urllib.request; urllib.request.urlretrieve('https://huggingface.co/nvidia/NVIDIA-Nemotron-3.5-Bolt-74B-A9B-24GB/resolve/main/ultra_v3_reasoning_parser.py', '/opt/ultra_v3_reasoning_parser.py')"
