#!/usr/bin/env python3
"""Recherche RAG sur un jeu BEIR public, avec les mêmes embeddings et le même reranker que mcp-docs.

Lancé par scripts/bench-public.py dans un conteneur jetable du service mcp-docs (docker compose run) : mêmes
variables EMBED_URL / RERANK_URL / RAG_BACKEND que la production, y compris un reranker déporté (serveur annexe).
Aucun LLM : mesure seulement si les bons passages remontent, avant et après reranking.

  python rag_beir.py --jeu scifact --limite 300 --sortie /bench/resultats/.../rag-scifact
"""

import argparse
import hashlib
import json
import math
import os
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, "/app")
import server  # noqa: E402  (embed, rerank, configuration de mcp-docs)

RACINE = Path(os.environ.get("BANC_RACINE", "/bench"))


def lire(dossier: Path):
    corpus = {}
    for l in (dossier / "corpus.jsonl").open():
        d = json.loads(l)
        corpus[d["_id"]] = (d.get("title", "") + "\n" + d.get("text", "")).strip()[:4000]
    requetes = {json.loads(l)["_id"]: json.loads(l)["text"] for l in (dossier / "queries.jsonl").open()}
    qrels: dict[str, dict[str, int]] = {}
    for i, l in enumerate((dossier / "qrels" / "test.tsv").open()):
        q, d, s = l.rstrip("\n").split("\t")[:3]
        if i == 0 and not s.isdigit():
            continue  # en-tête
        if int(s) > 0:
            qrels.setdefault(q, {})[d] = int(s)
    return corpus, requetes, qrels


def ndcg(ordre: list[str], pertinents: dict[str, int], k: int = 10) -> float:
    dcg = sum((2 ** pertinents.get(d, 0) - 1) / math.log2(i + 2) for i, d in enumerate(ordre[:k]))
    ideal = sorted(pertinents.values(), reverse=True)[:k]
    idcg = sum((2 ** g - 1) / math.log2(i + 2) for i, g in enumerate(ideal))
    return dcg / idcg if idcg else 0.0


def rappel(ordre: list[str], pertinents: dict, k: int) -> float:
    return len(set(ordre[:k]) & set(pertinents)) / len(pertinents)


def mrr(ordre: list[str], pertinents: dict, k: int = 10) -> float:
    return next((1 / (i + 1) for i, d in enumerate(ordre[:k]) if d in pertinents), 0.0)


def vecteurs(textes: list[str], lot: int = 64) -> np.ndarray:
    res = []
    for i in range(0, len(textes), lot):
        res.extend(server.embed(textes[i:i + lot]))
        if len(textes) > lot and (i // lot) % 20 == 0:
            print(f"  embeddings {min(i + lot, len(textes))}/{len(textes)}", flush=True)
    m = np.asarray(res, dtype=np.float32)
    return m / np.maximum(np.linalg.norm(m, axis=1, keepdims=True), 1e-9)


def centile(v: list[float], q: float) -> float | None:
    v = sorted(v)
    return round(v[min(len(v) - 1, int(round(q * (len(v) - 1))))], 1) if v else None


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--jeu", required=True)
    ap.add_argument("--limite", type=int, default=0)
    ap.add_argument("--candidats", type=int, default=20, help="comme mcp-docs : 20 candidats vectoriels, puis rerank")
    ap.add_argument("--sortie", required=True)
    a = ap.parse_args()

    corpus, requetes, qrels = lire(RACINE / "donnees" / "beir" / a.jeu)
    ids_q = sorted(q for q in qrels if q in requetes)
    ids_q = ids_q[: a.limite] if a.limite else ids_q
    ids_d = list(corpus)
    print(f"{a.jeu} : {len(ids_d)} documents, {len(ids_q)} requêtes ; backend {server.RAG_BACKEND}, "
          f"embeddings {server.EMBED_URL}, reranker {server.RERANK_URL}", flush=True)

    # Les embeddings du corpus ne dépendent que du modèle : mis en cache (clé = jeu + service d'embedding)
    cle = hashlib.sha256(f"{a.jeu}|{server.RAG_BACKEND}|{server.EMBED_URL}|{len(ids_d)}".encode()).hexdigest()[:12]
    cache = RACINE / "cache" / "beir" / f"{a.jeu}-{cle}.npy"
    if cache.exists():
        docs = np.load(cache)
    else:
        docs = vecteurs([corpus[d] for d in ids_d])
        cache.parent.mkdir(parents=True, exist_ok=True)
        np.save(cache, docs)

    m = {"vect": {"ndcg@10": [], "rappel@10": [], "rappel@cand": [], "mrr@10": []},
         "rerank": {"ndcg@10": [], "rappel@10": [], "mrr@10": []}}
    t_embed, t_rerank = [], []
    for i, q in enumerate(ids_q):
        t0 = time.perf_counter()
        v = vecteurs([requetes[q]])[0]
        t1 = time.perf_counter()
        idx = np.argsort(-(docs @ v))[: a.candidats]
        ordre = [ids_d[j] for j in idx]
        t2 = time.perf_counter()
        scores = server.rerank(requetes[q], [corpus[d] for d in ordre])
        t3 = time.perf_counter()
        ordre_r = [ordre[x["index"]] for x in sorted(scores, key=lambda x: x["score"], reverse=True)]
        t_embed.append(1000 * (t1 - t0))
        t_rerank.append(1000 * (t3 - t2))
        p = qrels[q]
        m["vect"]["ndcg@10"].append(ndcg(ordre, p))
        m["vect"]["rappel@10"].append(rappel(ordre, p, 10))
        m["vect"]["rappel@cand"].append(rappel(ordre, p, a.candidats))
        m["vect"]["mrr@10"].append(mrr(ordre, p))
        m["rerank"]["ndcg@10"].append(ndcg(ordre_r, p))
        m["rerank"]["rappel@10"].append(rappel(ordre_r, p, 10))
        m["rerank"]["mrr@10"].append(mrr(ordre_r, p))
        if (i + 1) % 50 == 0:
            print(f"  requêtes {i + 1}/{len(ids_q)}", flush=True)

    scores = {}
    for etape, vals in m.items():
        for nom, v in vals.items():
            nom = nom.replace("cand", str(a.candidats))
            scores[f"rag_{a.jeu}:{etape}_{nom}"] = round(100 * sum(v) / max(1, len(v)), 1)
    scores |= {f"rag_{a.jeu}:embed_p50_ms": centile(t_embed, 0.5), f"rag_{a.jeu}:rerank_p50_ms": centile(t_rerank, 0.5),
               f"rag_{a.jeu}:rerank_p95_ms": centile(t_rerank, 0.95)}
    sortie = Path(a.sortie)
    sortie.mkdir(parents=True, exist_ok=True)
    (sortie / "score.json").write_text(json.dumps({"scores": scores, "details": {
        "documents": len(ids_d), "requetes": len(ids_q), "candidats": a.candidats, "backend": server.RAG_BACKEND,
        "embed_url": server.EMBED_URL, "rerank_url": server.RERANK_URL}}, indent=2) + "\n")
    print(json.dumps(scores, indent=2))


if __name__ == "__main__":
    main()
