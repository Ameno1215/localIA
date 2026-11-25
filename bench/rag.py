"""Phase 3 — RAG sous charge : latences embeddings / rerank / recherche vectorielle, et rappel@5.

À lancer là où TEI, le reranker et Qdrant sont joignables. Sur le PC de test ils n'ont pas de port
publié : lancer la phase dans le réseau docker, avec l'image du serveur MCP (voir README) :
  docker compose run --rm --no-deps -v ./bench:/b/bench -w /b mcp-docs python -m bench rag

Mesures :
  embeddings  latence p95 par niveau de concurrence (requêtes courtes, comme une question) ;
  rerank      latence p95 pour N candidats (50 par défaut, objectif < 500 ms) ;
  recherche   latence p95 d'une requête vectorielle Qdrant top 20 (objectif < 200 ms) ;
  rappel@5    sur le fichier de questions (JSONL : question, source attendue, page facultative),
              même chaîne que l'outil rechercher_docs : embed → Qdrant top 20 → rerank → top 5,
              comparée à la même chaîne sans reranker.

Pour tester l'ingestion pendant la charge (phase 3 du plan), lancer `python3 -m bench charge` dans
un autre terminal, puis l'ingestion d'un gros lot de datasheets, puis cette commande.
"""
import json
import random
import threading
import time
from pathlib import Path

from bench.commun import DEPOT, Generateur, centile, charger_config, requete, sauver


def options(p):
    p.add_argument("--sans-latences", action="store_true", help="seulement le rappel@5")
    p.add_argument("--sans-rappel", action="store_true", help="seulement les latences")
    p.add_argument("--questions", help="fichier JSONL de questions (défaut : config)")


class Pile:
    def __init__(self, r):
        self.r = r
        self.vllm = r.get("backend", "tei") == "vllm"

    def embed(self, textes):
        if self.vllm:
            s, d, _ = requete("POST", self.r["embed"] + "/v1/embeddings",
                              {"model": self.r.get("modele_embed", "bge-m3"), "input": textes})
            return s, [x["embedding"] for x in sorted(d["data"], key=lambda x: x["index"])] if s == 200 else d
        s, d, _ = requete("POST", self.r["embed"] + "/embed", {"inputs": textes, "truncate": True})
        return s, d

    def rerank(self, question, textes):
        if self.vllm:
            s, d, _ = requete("POST", self.r["rerank"] + "/v1/rerank",
                              {"model": self.r.get("modele_rerank", "bge-reranker"), "query": question,
                               "documents": textes})
            return s, ([{"index": x["index"], "score": x["relevance_score"]} for x in d["results"]]
                       if s == 200 else d)
        s, d, _ = requete("POST", self.r["rerank"] + "/rerank", {"query": question, "texts": textes, "truncate": True})
        return s, d

    def chercher(self, vecteur, limite=20):
        s, d, _ = requete("POST", f"{self.r['qdrant']}/collections/{self.r['collection']}/points/query",
                          {"query": vecteur, "limit": limite, "with_payload": True})
        return s, (d.get("result", {}).get("points", []) if s == 200 and isinstance(d, dict) else d)

    def dimension(self):
        s, d, _ = requete("GET", f"{self.r['qdrant']}/collections/{self.r['collection']}")
        if s != 200:
            return None
        v = d["result"]["config"]["params"]["vectors"]
        return v["size"] if "size" in v else next(iter(v.values()))["size"]


def _charge(fonction, concurrence, total):
    """Lance `total` appels répartis sur `concurrence` fils ; rend (durées ok en ms, erreurs)."""
    durees, erreurs, verrou = [], [], threading.Lock()
    compteur = iter(range(total))

    def fil():
        while True:
            with verrou:
                if next(compteur, None) is None:
                    return
            t0 = time.perf_counter()
            statut, d = fonction()
            dt = (time.perf_counter() - t0) * 1000
            with verrou:
                (durees.append(dt) if statut == 200 else erreurs.append(f"{statut} {str(d)[:120]}"))

    fils = [threading.Thread(target=fil) for _ in range(concurrence)]
    for f in fils:
        f.start()
    for f in fils:
        f.join()
    return durees, erreurs


def _ligne(nom, c, durees, erreurs):
    p50, p95 = centile(durees, 50), centile(durees, 95)
    print(f"  {nom:<11} concurrence {c:<3} p50={p50 and round(p50)}ms p95={p95 and round(p95)}ms "
          f"erreurs={len(erreurs)}", flush=True)
    return {"concurrence": c, "p50_ms": p50 and round(p50, 1), "p95_ms": p95 and round(p95, 1),
            "erreurs": len(erreurs), "exemples_erreurs": erreurs[:3]}


def latences(cfg, pile):
    r = cfg["rag"]
    n = r.get("requetes_par_niveau", 100)
    gen = Generateur(graine="rag")
    questions = [gen.texte(25) for _ in range(50)]
    candidats = [gen.texte(200) for _ in range(r.get("candidats_rerank", 50))]
    out = {"embeddings": [], "rerank": [], "recherche": []}
    dim = pile.dimension()
    print(f"\nLatences (dimension des vecteurs : {dim})")
    for c in r.get("concurrences", [1, 8]):
        out["embeddings"].append(_ligne("embeddings", c, *_charge(lambda: pile.embed([random.choice(questions)]), c, n)))
        out["rerank"].append(_ligne("rerank", c, *_charge(lambda: pile.rerank(random.choice(questions), candidats), c, n)))
        if dim:
            out["recherche"].append(_ligne("recherche", c, *_charge(
                lambda: pile.chercher([random.uniform(-1, 1) for _ in range(dim)]), c, n)))
    if not dim:
        print("  ⚠ Collection Qdrant introuvable : recherche non mesurée.")
    return out


def rappel(cfg, pile, fichier):
    chemin = Path(fichier)
    if not chemin.is_absolute() and not chemin.exists():
        chemin = DEPOT / fichier
    questions = [json.loads(l) for l in chemin.read_text().splitlines() if l.strip() and not l.startswith("//")]
    print(f"\nRappel@5 sur {len(questions)} questions ({chemin.name})")

    def trouve(payload, q):
        if q["source"].lower() not in str(payload.get("source", "")).lower():
            return False
        return q.get("page") is None or payload.get("page") == q["page"]

    avec, sans, details = 0, 0, []
    for q in questions:
        s, vec = pile.embed([q["question"]])
        if s != 200:
            raise SystemExit(f"Embeddings en échec : {s} {str(vec)[:200]}")
        s, points = pile.chercher(vec[0], 20)
        if s != 200:
            raise SystemExit(f"Recherche Qdrant en échec : {s} {str(points)[:200]}")
        top_sans = points[:5]
        if points:
            s, classes = pile.rerank(q["question"], [p["payload"].get("texte", "") for p in points])
            if s != 200:
                raise SystemExit(f"Rerank en échec : {s} {str(classes)[:200]}")
            ordre = sorted(classes, key=lambda x: x["score"], reverse=True)[:5]
            top_avec = [points[x["index"]] for x in ordre]
        else:
            top_avec = []
        ok_avec = any(trouve(p["payload"], q) for p in top_avec)
        ok_sans = any(trouve(p["payload"], q) for p in top_sans)
        avec += ok_avec
        sans += ok_sans
        details.append({"question": q["question"], "avec_reranker": ok_avec, "sans_reranker": ok_sans,
                        "sources_top5": [f"{p['payload'].get('source')} p.{p['payload'].get('page')}" for p in top_avec]})
        print(f"  {'✅' if ok_avec else '❌'} {'✅' if ok_sans else '❌'}  {q['question'][:80]}")
    n = len(questions) or 1
    print(f"  rappel@5 avec reranker : {avec / n:.0%}   sans : {sans / n:.0%}   (colonnes : avec, sans)")
    return {"questions": len(questions), "rappel5_avec_reranker": round(avec / n, 3),
            "rappel5_sans_reranker": round(sans / n, 3), "details": details}


def lancer(args):
    cfg = charger_config(args.profil)
    pile = Pile(cfg["rag"])
    out = {"backend": cfg["rag"].get("backend", "tei")}
    if not args.sans_latences:
        out["latences"] = latences(cfg, pile)
    if not args.sans_rappel:
        out["rappel"] = rappel(cfg, pile, args.questions or cfg["rag"]["questions"])
    sauver("rag", cfg, out)
