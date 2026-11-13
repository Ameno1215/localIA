# Benchmarks publics (volet 1 du PLAN-EVALUATION.md) : lm-evaluation-harness, EvalPlus, MultiPL-E C++, LongBench v2.
# Construit par  ./scripts/bench-public.py preparer  (seul moment où il faut Internet, avec le téléchargement des jeux).
# g++ sert à compiler le code C++ produit par le modèle, dans un conteneur lancé SANS réseau.
FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends g++ git && rm -rf /var/lib/apt/lists/*
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt
# Tout ce qui est téléchargé ou mis en cache va dans /bench (WS_DIR/benchmarks), pas dans l'image
ENV HF_HOME=/bench/hf \
    XDG_CACHE_HOME=/bench/cache \
    NLTK_DATA=/bench/nltk \
    TOKENIZERS_PARALLELISM=false
COPY banc.py /app/banc.py
WORKDIR /app
ENTRYPOINT ["python", "/app/banc.py"]
