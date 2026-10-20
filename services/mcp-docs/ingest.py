"""Indexe ./docs-internes (PDF, DOCX, MD, TXT) dans Qdrant.

Usage :  docker compose exec mcp-docs python ingest.py          (incrémental)
         docker compose exec mcp-docs python ingest.py --reset  (tout réindexer)
"""

import hashlib
import sys
import uuid
from pathlib import Path

from docx import Document
from pypdf import PdfReader
from qdrant_client import models

from server import COLLECTION, embed, qdrant

DOCS = Path("/docs")
TAILLE, CHEVAUCHEMENT = 1200, 200  # en caractères (~300 tokens)


def pages(fichier: Path):
    """Rend (numéro de page ou None, texte)."""
    ext = fichier.suffix.lower()
    if ext == ".pdf":
        for i, page in enumerate(PdfReader(fichier).pages, 1):
            yield i, page.extract_text() or ""
    elif ext == ".docx":
        yield None, "\n".join(p.text for p in Document(fichier).paragraphs)
    elif ext in {".md", ".txt", ".rst", ".adoc"}:
        yield None, fichier.read_text(errors="replace")


def decouper(texte: str):
    """Découpe en morceaux en respectant les paragraphes quand c'est possible."""
    morceau = ""
    for para in texte.split("\n\n"):
        para = para.strip()
        if not para:
            continue
        if len(morceau) + len(para) > TAILLE and morceau:
            yield morceau
            morceau = morceau[-CHEVAUCHEMENT:] + "\n\n"
        morceau += para + "\n\n"
        while len(morceau) > TAILLE * 1.5:  # paragraphe géant
            yield morceau[:TAILLE]
            morceau = morceau[TAILLE - CHEVAUCHEMENT:]
    if morceau.strip():
        yield morceau


def main():
    reset = "--reset" in sys.argv
    if reset and qdrant.collection_exists(COLLECTION):
        qdrant.delete_collection(COLLECTION)
    if not qdrant.collection_exists(COLLECTION):
        dim = len(embed(["test"])[0])
        qdrant.create_collection(COLLECTION, vectors_config=models.VectorParams(size=dim, distance=models.Distance.COSINE))
        qdrant.create_payload_index(COLLECTION, "source", models.PayloadSchemaType.KEYWORD)

    for fichier in sorted(p for p in DOCS.rglob("*") if p.is_file()):
        source = str(fichier.relative_to(DOCS))
        empreinte = hashlib.sha256(fichier.read_bytes()).hexdigest()
        filtre = models.Filter(must=[models.FieldCondition(key="source", match=models.MatchValue(value=source))])
        deja, _ = qdrant.scroll(COLLECTION, scroll_filter=filtre, limit=1)
        # "num" absent : indexé avant l'ajout des liens vers les extraits -> on réindexe
        if deja and deja[0].payload.get("empreinte") == empreinte and "num" in deja[0].payload:
            print(f"= {source} (inchangé)")
            continue
        qdrant.delete(COLLECTION, points_selector=models.FilterSelector(filter=filtre))

        morceaux = [(page, m) for page, texte in pages(fichier) for m in decouper(texte)]
        if not morceaux:
            print(f"! {source} : format non géré ou vide")
            continue
        for i in range(0, len(morceaux), 16):
            lot = morceaux[i : i + 16]
            vecteurs = embed([m for _, m in lot])
            qdrant.upsert(COLLECTION, points=[
                models.PointStruct(
                    id=str(uuid.uuid5(uuid.NAMESPACE_URL, f"{source}#{i + j}")),
                    vector=v,
                    payload={"source": source, "page": page, "texte": m, "empreinte": empreinte, "num": i + j},
                )
                for j, ((page, m), v) in enumerate(zip(lot, vecteurs))
            ])
        print(f"+ {source} : {len(morceaux)} morceaux")


if __name__ == "__main__":
    main()
