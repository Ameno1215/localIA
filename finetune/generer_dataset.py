"""Génère un jeu d'entraînement questions/réponses à partir des docs indexés dans Qdrant.

Chaque morceau de norme -> le LLM local écrit N paires Q/R ancrées dans le texte.
Sortie : /data/dataset.jsonl au format "messages" (chat).

Lancement (la stack doit tourner, docs déjà indexés) :
  docker compose exec -e LITELLM_KEY=$LITELLM_MASTER_KEY mcp-docs python /finetune/generer_dataset.py
"""

import json
import os
import random

import httpx
from qdrant_client import QdrantClient

QPAR_MORCEAU = int(os.environ.get("QPAR_MORCEAU", "4"))
SYSTEME = "Tu es l'assistant interne de l'entreprise. Tu réponds en français, précisément, en citant la norme concernée."
CONSIGNE = """Voici un extrait de la documentation interne (source : {source}).

<extrait>
{texte}
</extrait>

Écris {n} paires question/réponse qu'un développeur de l'équipe pourrait poser sur cet extrait.
- Questions variées : définition, règle à appliquer, cas pratique, « est-ce autorisé de… ».
- Réponses complètes, fidèles à l'extrait uniquement, qui citent la source.
Réponds UNIQUEMENT avec un tableau JSON : [{{"question": "...", "reponse": "..."}}]"""

qdrant = QdrantClient(url=os.environ.get("QDRANT_URL", "http://qdrant:6333"))
llm = httpx.Client(
    base_url=os.environ.get("LLM_URL", "http://litellm:4000/v1"),
    headers={"Authorization": f"Bearer {os.environ['LITELLM_KEY']}"},
    timeout=300,
)


def paires(source: str, texte: str) -> list[dict]:
    r = llm.post("/chat/completions", json={
        "model": "qwen-local",
        "temperature": 0.7,
        "messages": [{"role": "user", "content": CONSIGNE.format(source=source, texte=texte, n=QPAR_MORCEAU)}],
    })
    r.raise_for_status()
    contenu = r.json()["choices"][0]["message"]["content"]
    debut, fin = contenu.find("["), contenu.rfind("]")
    return json.loads(contenu[debut : fin + 1])


def main():
    collection = os.environ.get("COLLECTION", "docs_internes")
    points, offset = [], None
    while True:
        lot, offset = qdrant.scroll(collection, limit=256, offset=offset, with_payload=True)
        points += lot
        if offset is None:
            break
    print(f"{len(points)} morceaux")

    exemples = []
    for i, p in enumerate(points, 1):
        try:
            for qr in paires(p.payload["source"], p.payload["texte"]):
                exemples.append({"messages": [
                    {"role": "system", "content": SYSTEME},
                    {"role": "user", "content": qr["question"]},
                    {"role": "assistant", "content": qr["reponse"]},
                ]})
        except (json.JSONDecodeError, KeyError, httpx.HTTPError) as e:
            print(f"  morceau {i} ignoré : {e}")
        print(f"\r{i}/{len(points)} -> {len(exemples)} exemples", end="", flush=True)

    random.shuffle(exemples)
    with open("/data/dataset.jsonl", "w") as f:
        for ex in exemples:
            f.write(json.dumps(ex, ensure_ascii=False) + "\n")
    print(f"\nÉcrit : data/dataset.jsonl ({len(exemples)} exemples). Relis-en un échantillon avant d'entraîner !")


if __name__ == "__main__":
    main()
