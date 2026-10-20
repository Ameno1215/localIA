"""Serveur MCP "docs-internes".

Outils exposés aux agents (NVIDIA NAT, OpenCode, Open WebUI) :
  - rechercher_docs : RAG sur les normes / docs internes (Qdrant + bge-m3 + reranker)
  - lister_fichiers / lire_fichier / ecrire_fichier / executer_commande : travail sur ./workspace,
    seulement si OUTILS_CODE=1 (agent NAT local). OpenCode utilise ses propres outils, sur le poste.
"""

import html
import logging
import os
import subprocess
import uuid
from pathlib import Path
from urllib.parse import quote, unquote

import httpx
from mcp.server.fastmcp import FastMCP
from qdrant_client import QdrantClient
from starlette.requests import Request
from starlette.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, Response

QDRANT_URL = os.environ.get("QDRANT_URL", "http://localhost:6333")
EMBED_URL = os.environ.get("EMBED_URL", "http://localhost:8081")
RERANK_URL = os.environ.get("RERANK_URL", "http://localhost:8082")
COLLECTION = os.environ.get("COLLECTION", "docs_internes")
# "tei" : Text Embeddings Inference (PC x86, CPU). "vllm" : vLLM en mode pooling (Spark ARM64, où TEI n'existe pas).
RAG_BACKEND = os.environ.get("RAG_BACKEND", "tei")
WORKSPACE = Path(os.environ.get("WORKSPACE", "/workspace")).resolve()
# 1 : outils fichiers + commandes sur ./workspace (agent NAT local).
# 0 : seulement rechercher_docs — obligatoire dès que le serveur est joignable par le réseau (Spark).
OUTILS_CODE = os.environ.get("OUTILS_CODE", "1") == "1"
DOCS = Path(os.environ.get("DOCS_DIR", "/docs")).resolve()
# Adresse sous laquelle le navigateur de l'utilisateur joint ce serveur (liens vers les sources)
DOCS_PUBLIC_URL = os.environ.get("DOCS_PUBLIC_URL", "http://localhost:9901").rstrip("/")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("outils")
qdrant = QdrantClient(url=QDRANT_URL)
http = httpx.Client(timeout=120)
mcp = FastMCP("docs-internes", host="0.0.0.0", port=9901)
outil_code = mcp.tool() if OUTILS_CODE else (lambda f: f)


def embed(texts: list[str]) -> list[list[float]]:
    if RAG_BACKEND == "vllm":
        r = http.post(f"{EMBED_URL}/v1/embeddings", json={"model": "bge-m3", "input": texts})
        r.raise_for_status()
        return [d["embedding"] for d in sorted(r.json()["data"], key=lambda d: d["index"])]
    r = http.post(f"{EMBED_URL}/embed", json={"inputs": texts, "truncate": True})
    r.raise_for_status()
    return r.json()


def rerank(question: str, textes: list[str]) -> list[dict]:
    """Rend [{"index": i, "score": s}] quel que soit le moteur."""
    if RAG_BACKEND == "vllm":
        r = http.post(f"{RERANK_URL}/v1/rerank", json={"model": "bge-reranker", "query": question, "documents": textes})
        r.raise_for_status()
        return [{"index": x["index"], "score": x["relevance_score"]} for x in r.json()["results"]]
    r = http.post(f"{RERANK_URL}/rerank", json={"query": question, "texts": textes, "truncate": True})
    r.raise_for_status()
    return r.json()


def _chemin(chemin: str) -> Path:
    """Résout un chemin relatif au workspace et interdit d'en sortir."""
    p = (WORKSPACE / chemin).resolve()
    if p != WORKSPACE and WORKSPACE not in p.parents:
        raise ValueError(f"Chemin hors du workspace : {chemin}")
    return p


@mcp.custom_route("/docs/{chemin:path}", methods=["GET"])
async def servir_doc(request: Request) -> Response:
    """Sert les fichiers de docs-internes/ (lecture seule) pour que les sources citées soient cliquables."""
    fichier = (DOCS / unquote(request.path_params["chemin"])).resolve()
    if DOCS not in fichier.parents or not fichier.is_file():
        return PlainTextResponse("Document introuvable", status_code=404)
    return FileResponse(fichier, content_disposition_type="inline")


def _lien_document(source: str, page: int | None) -> str:
    ancre = f"#page={page}" if page else ""
    return f"{DOCS_PUBLIC_URL}/docs/{quote(source)}{ancre}"


PAGE_EXTRAIT = """<!doctype html><html lang="fr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Extrait – {titre}</title>
<style>
body {{ font-family: system-ui, sans-serif; max-width: 860px; margin: 2rem auto; padding: 0 1rem; line-height: 1.55; color: #222; background: #fafafa; }}
h1 {{ font-size: 1.15rem; margin-bottom: .2rem; }} .meta {{ color: #666; margin-bottom: 1.5rem; }}
pre {{ white-space: pre-wrap; font-family: inherit; margin: 0; padding: .8rem 1rem; border-radius: 6px; }}
.contexte {{ color: #888; background: #f0f0f0; }}
.extrait {{ background: #fff6c8; border-left: 4px solid #e0b400; margin: .6rem 0; }}
.etiquette {{ font-size: .8rem; color: #888; margin-top: 1rem; }}
@media (prefers-color-scheme: dark) {{
  body {{ background: #1b1b1b; color: #ddd; }} .meta, .etiquette {{ color: #999; }}
  .contexte {{ background: #262626; color: #888; }} .extrait {{ background: #3a3410; border-color: #c9a200; }}
  a {{ color: #8ab4f8; }} }}
</style></head><body>
<h1>{titre}</h1>
<div class="meta">{page} · <a href="{lien}">Ouvrir le document{a_la_page}</a></div>
{corps}
</body></html>"""


def _ref(id_point) -> str:
    """Référence courte d'un extrait (8 premiers caractères hexa de son id), que le modèle recopie dans sa réponse."""
    return str(id_point).replace("-", "")[:8]


_refs: dict[str, str] = {}


def _resoudre(ref: str) -> str | None:
    """Référence courte -> id complet. L'index est reconstruit si la référence est inconnue (nouvelle ingestion)."""
    if ref not in _refs:
        _refs.clear()
        offset = None
        while True:
            points, offset = qdrant.scroll(COLLECTION, limit=1000, offset=offset, with_payload=False)
            for pt in points:
                _refs.setdefault(_ref(pt.id), str(pt.id))
            if offset is None:
                break
    return _refs.get(ref)


@mcp.custom_route("/refs", methods=["GET"])
async def infos_refs(request: Request) -> Response:
    """Pour le filtre Open WebUI : /refs?r=a1b2c3d4,e5f6a7b8 -> {ref: {source, page, url} ou null}."""
    infos = {}
    for ref in request.query_params.get("r", "").split(",")[:50]:
        id_point = _resoudre(ref) if ref else None
        trouves = qdrant.retrieve(COLLECTION, ids=[id_point]) if id_point else []
        infos[ref] = {
            "source": trouves[0].payload["source"],
            "page": trouves[0].payload.get("page"),
            "url": f"{DOCS_PUBLIC_URL}/x/{ref}",
        } if trouves else None
    return JSONResponse(infos)


@mcp.custom_route("/x/{ref}", methods=["GET"])
async def servir_ref(request: Request) -> Response:
    id_point = _resoudre(request.path_params["ref"])
    if not id_point:
        return PlainTextResponse("Extrait introuvable (document réindexé depuis ?)", status_code=404)
    return _page_extrait(id_point)


@mcp.custom_route("/extrait/{id}", methods=["GET"])
async def servir_extrait(request: Request) -> Response:
    try:
        return _page_extrait(str(uuid.UUID(request.path_params["id"])))
    except ValueError:
        return PlainTextResponse("Identifiant invalide", status_code=400)


def _page_extrait(id_point: str) -> Response:
    """Affiche le morceau exact utilisé par le RAG, entouré des morceaux voisins pour le contexte."""
    trouves = qdrant.retrieve(COLLECTION, ids=[id_point])
    if not trouves:
        return PlainTextResponse("Extrait introuvable (document réindexé depuis ?)", status_code=404)
    p = trouves[0].payload

    def voisin(num: int) -> str | None:
        v = qdrant.retrieve(COLLECTION, ids=[str(uuid.uuid5(uuid.NAMESPACE_URL, f"{p['source']}#{num}"))])
        return v[0].payload["texte"] if v else None

    avant = voisin(p["num"] - 1) if p.get("num") else None
    apres = voisin(p["num"] + 1) if p.get("num") is not None else None
    corps = ""
    if avant:
        corps += f'<div class="etiquette">Contexte précédent</div><pre class="contexte">{html.escape(avant)}</pre>'
    corps += f'<div class="etiquette">Extrait utilisé</div><pre class="extrait">{html.escape(p["texte"])}</pre>'
    if apres:
        corps += f'<div class="etiquette">Contexte suivant</div><pre class="contexte">{html.escape(apres)}</pre>'
    return HTMLResponse(PAGE_EXTRAIT.format(
        titre=html.escape(p["source"]),
        page=f"Page {p['page']}" if p.get("page") else "Page non disponible pour ce format",
        lien=html.escape(_lien_document(p["source"], p.get("page"))),
        a_la_page=f" à la page {p['page']}" if p.get("page") else "",
        corps=corps,
    ))


def _description_outil() -> str:
    """Description lue par le modèle pour décider d'appeler l'outil : elle liste les documents réellement présents."""
    fichiers = sorted(p.stem for p in DOCS.rglob("*") if p.is_file()) if DOCS.is_dir() else []
    liste = "\n".join(f"  - {f}" for f in fichiers) or "  (aucun document)"
    return (
        "Cherche dans la base documentaire interne (normes et spécifications techniques). "
        "Documents disponibles :\n" + liste + "\n"
        "À appeler dès que la question porte sur le contenu de ces documents : cartes à puce, "
        "APDU, ATR, protocoles T=0/T=1, ISO 7816, ISO 14443, sans contact, GlobalPlatform, "
        "ou une norme/convention interne. Ne pas l'appeler pour une question générale sans rapport. "
        "Retourne des extraits, chacun précédé de sa référence, par exemple [réf a1b2c3d4 | fichier, p.N]. "
        "OBLIGATOIRE dans ta réponse : après chaque phrase qui utilise un extrait, écris sa référence "
        "entre crochets, par exemple « L'ATR fait au plus 32 octets [a1b2c3d4]. » ; si une phrase "
        "s'appuie sur plusieurs extraits : [a1b2c3d4][e5f6a7b8]. Recopie les références exactement, "
        "n'en invente jamais, et n'affirme rien qui ne figure pas dans les extraits. "
        "N'écris PAS de section « Sources » ni de liens : ils sont ajoutés automatiquement."
    )


@mcp.tool(description=_description_outil())
def rechercher_docs(question: str, nb_resultats: int = 5) -> str:
    """Recherche dans les documents internes."""
    log.info("rechercher_docs(%r)", question)
    candidats = qdrant.query_points(COLLECTION, query=embed([question])[0], limit=20).points
    if not candidats:
        return "Aucun document indexé ne correspond."
    textes = [c.payload["texte"] for c in candidats]
    classes = sorted(rerank(question, textes), key=lambda x: x["score"], reverse=True)[:nb_resultats]
    blocs = []
    for x in classes:
        p = candidats[x["index"]].payload
        page = f", p.{p['page']}" if p.get("page") else ""
        blocs.append(f"[réf {_ref(candidats[x['index']].id)} | {p['source']}{page}]\n{p['texte']}")
    return "\n\n---\n\n".join(blocs)


@outil_code
def lister_fichiers(dossier: str = ".") -> str:
    """Liste récursivement les fichiers du projet de code (workspace)."""
    log.info("lister_fichiers(%r)", dossier)
    base = _chemin(dossier)
    ignores = {".git", "node_modules", "__pycache__", ".venv"}
    fichiers = [
        str(p.relative_to(WORKSPACE))
        for p in sorted(base.rglob("*"))
        if p.is_file() and not ignores.intersection(p.relative_to(WORKSPACE).parts)
    ]
    return "\n".join(fichiers[:500]) or "(vide)"


@outil_code
def lire_fichier(chemin: str) -> str:
    """Lit un fichier texte du workspace."""
    log.info("lire_fichier(%r)", chemin)
    return _chemin(chemin).read_text(errors="replace")


@outil_code
def ecrire_fichier(chemin: str, contenu: str) -> str:
    """Crée ou remplace un fichier du workspace avec le contenu donné."""
    log.info("ecrire_fichier(%r)", chemin)
    p = _chemin(chemin)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(contenu)
    return f"Écrit : {chemin} ({len(contenu)} caractères)"


@outil_code
def executer_commande(commande: str) -> str:
    """Exécute une commande shell dans le workspace (ex : 'python -m pytest -q').
    Tourne dans le conteneur MCP, timeout 120 s."""
    log.info("executer_commande(%r)", commande)
    try:
        r = subprocess.run(commande, shell=True, cwd=WORKSPACE, capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired:
        return "Timeout (120 s)."
    sortie = (r.stdout + r.stderr)[-8000:]
    return f"code retour {r.returncode}\n{sortie}"


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
