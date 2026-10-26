"""Indexe ./docs-internes (PDF, DOCX, MD, TXT) dans Qdrant.

Usage :  docker compose exec mcp-docs python ingest.py               (incrémental)
         docker compose exec mcp-docs python ingest.py --reset       (tout réindexer)
         docker compose exec mcp-docs python ingest.py --sans-images (ne pas solliciter le LLM, images en attente)

Tableaux Word : convertis en tableaux Markdown, à leur place dans le texte.
Images (PDF et Word, pages scannées comprises) : transcrites par le LLM actif s'il lit les images
(Nemotron 3.5 Super VL par défaut), puis indexées comme le reste. Avec un modèle texte seul, elles sont
comptées « en attente » et traitées au premier lancement qui dispose d'un modèle vision.
"""

import base64
import hashlib
import io
import os
import sys
import uuid
from pathlib import Path

import httpx
from docx import Document
from docx.table import Table
from PIL import Image
from pypdf import PdfReader
from qdrant_client import models

from server import COLLECTION, embed, qdrant

DOCS = Path("/docs")
TAILLE, CHEVAUCHEMENT = 1200, 200  # en caractères (~300 tokens)
# À changer quand l'extraction change : les documents indexés avec une autre version sont réindexés
VERSION = 2

# Le LLM servi par vLLM, appelé directement (même réseau Docker, pas de clé)
VISION_URL = os.environ.get("VISION_URL", "http://vllm:8000/v1")
VISION_MODELE = os.environ.get("VISION_MODELE", "qwen-local")
IMAGE_MIN = 100  # en pixels : en dessous, icône, puce ou filet -> ignorée
IMAGE_MAX = 1600  # plus grand côté envoyé au modèle
CONSIGNE_IMAGE = """Cette image est extraite d'un document technique interne. Transcris son contenu pour une base documentaire :
- tableau : tableau Markdown complet, toutes les lignes et colonnes, en-têtes et valeurs exactes ;
- schéma, diagramme, chronogramme : description précise des éléments, des valeurs, des flèches et de leurs relations ;
- texte (capture d'écran, page scannée) : le texte intégral, mot pour mot.
Ne résume pas, n'invente rien, n'ajoute aucun commentaire.
Si l'image n'apporte aucune information (logo, décoration, photo d'illustration), réponds seulement : RIEN"""

llm = httpx.Client(base_url=VISION_URL, timeout=600)
_vision = None  # None : pas encore testé
_transcriptions = {}  # empreinte de l'image -> texte (logos et images répétés : un seul appel)


def _png(img: Image.Image) -> str:
    img = img.convert("RGB")
    img.thumbnail((IMAGE_MAX, IMAGE_MAX))
    tampon = io.BytesIO()
    img.save(tampon, format="PNG")
    return base64.b64encode(tampon.getvalue()).decode()


def _demander(png: str, consigne: str, max_tokens: int) -> dict:
    r = llm.post("/chat/completions", json={
        "model": VISION_MODELE,
        "temperature": 0,
        "max_tokens": max_tokens,
        # Pas de raisonnement : une transcription n'en a pas besoin, et il mangerait les jetons
        "chat_template_kwargs": {"enable_thinking": False},
        "messages": [{"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{png}"}},
            {"type": "text", "text": consigne},
        ]}],
    })
    r.raise_for_status()
    return r.json()["choices"][0]


def vision_dispo() -> bool:
    """Teste une fois si le modèle actif accepte les images."""
    global _vision
    if _vision is None:
        if "--sans-images" in sys.argv:
            _vision = False
            print("Images : non lues (--sans-images), comptées en attente")
            return _vision
        try:
            _demander(_png(Image.new("RGB", (64, 64), "white")), "Réponds OK.", 16)
            _vision = True
            print(f"Images : transcrites par {VISION_MODELE} ({VISION_URL})")
        except httpx.HTTPStatusError as e:
            _vision = False
            print(f"Images : non lues, le modèle actif n'accepte pas les images (HTTP {e.response.status_code} : "
                  f"{e.response.text[:200]}). Elles seront lues au prochain lancement avec un modèle vision.")
        except httpx.HTTPError as e:
            _vision = False
            print(f"Images : non lues, LLM injoignable ({e!r}). Le mode agent (vLLM) doit tourner.")
    return _vision


def transcrire(donnees: bytes, ou: str, stats: dict) -> str:
    """Texte tiré d'une image ("" si décorative, trop petite ou non lue)."""
    try:
        img = Image.open(io.BytesIO(donnees))
        img.load()
    except Exception as e:
        print(f"  ! {ou} : image illisible ({e})")
        return ""
    if min(img.size) < IMAGE_MIN:
        return ""
    cle = hashlib.sha256(donnees).hexdigest()
    if cle in stats["vues"]:  # même image plus haut dans le document (logo d'en-tête…)
        return ""
    stats["vues"].add(cle)
    stats["images"] += 1
    if cle not in _transcriptions:
        if not vision_dispo():
            stats["en_attente"] += 1
            return ""
        try:
            choix = _demander(_png(img), CONSIGNE_IMAGE, 4096)
        except httpx.HTTPError as e:
            print(f"  ! {ou} : transcription échouée ({e!r})")
            stats["en_attente"] += 1
            return ""
        texte = (choix["message"].get("content") or "").strip()
        if "</think>" in texte:
            texte = texte.split("</think>", 1)[1].strip()
        if choix.get("finish_reason") == "length":
            print(f"  ! {ou} : transcription tronquée (image très dense)")
        _transcriptions[cle] = "" if texte.strip(" .«»\"'").upper() == "RIEN" else texte
        print(f"  {ou} : {'décorative' if not _transcriptions[cle] else 'transcrite'}")
    return _transcriptions[cle]


def _bloc_image(texte: str, ou: str) -> str:
    return f"[{ou}]\n{texte}" if texte else ""


def _tableau_markdown(table: Table) -> str:
    """Tableau Markdown, coupé en blocs qui répètent l'en-tête : chaque morceau indexé garde le nom des colonnes."""
    lignes = ["| " + " | ".join(" ".join(c.text.split()).replace("|", "\\|") for c in ligne.cells) + " |"
              for ligne in table.rows]
    if not lignes:
        return ""
    entete = lignes[0] + "\n|" + " --- |" * lignes[0].count(" | ") + " --- |"
    blocs, bloc = [], entete
    for ligne in lignes[1:]:
        if len(bloc) + len(ligne) > TAILLE and bloc != entete:
            blocs.append(bloc)
            bloc = entete
        bloc += "\n" + ligne
    blocs.append(bloc)
    return "\n\n".join(blocs)


def pages(fichier: Path, stats: dict):
    """Rend (numéro de page ou None, texte)."""
    ext = fichier.suffix.lower()
    if ext == ".pdf":
        for i, page in enumerate(PdfReader(fichier).pages, 1):
            blocs = [page.extract_text() or ""]
            try:
                images = page.images
                n = len(images)
            except Exception as e:
                print(f"  ! p.{i} : images non extraites ({e})")
                n = 0
            for k in range(n):
                ou = f"Image, page {i}" + (f" n°{k + 1}" if n > 1 else "")
                try:
                    donnees = images[k].data
                except Exception as e:  # codage d'image non géré (JBIG2…)
                    print(f"  ! {ou} : image non décodée ({e})")
                    continue
                blocs.append(_bloc_image(transcrire(donnees, ou, stats), ou))
            texte = "\n\n".join(b for b in blocs if b.strip())
            if not texte.strip():
                print(f"  ! p.{i} : aucun texte lu (page blanche, scannée ou image non transcrite)")
            yield i, texte
    elif ext == ".docx":
        doc = Document(fichier)
        # Paragraphes, tableaux et images dans l'ordre du document
        blocs, n = [], 0
        for element in doc.iter_inner_content():
            if isinstance(element, Table):
                blocs.append("\n\n" + _tableau_markdown(element) + "\n\n")
                continue
            blocs.append(element.text)
            for rid in element._p.xpath(".//a:blip/@r:embed"):
                n += 1
                ou = f"Image n°{n}"
                partie = doc.part.related_parts.get(rid)
                if partie is not None:
                    image = _bloc_image(transcrire(partie.blob, ou, stats), ou)
                    if image:
                        blocs.append("\n" + image + "\n")
        yield None, "\n".join(blocs)
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

    total_attente = 0
    for fichier in sorted(p for p in DOCS.rglob("*") if p.is_file()):
        source = str(fichier.relative_to(DOCS))
        empreinte = hashlib.sha256(fichier.read_bytes()).hexdigest()
        filtre = models.Filter(must=[models.FieldCondition(key="source", match=models.MatchValue(value=source))])
        deja, _ = qdrant.scroll(COLLECTION, scroll_filter=filtre, limit=1)
        # Version absente ou ancienne : indexé avant les liens vers les extraits ou la lecture des images -> on réindexe
        if deja and deja[0].payload.get("empreinte") == empreinte and deja[0].payload.get("version") == VERSION:
            attente = deja[0].payload.get("images_en_attente", 0)
            if not (attente and vision_dispo()):
                print(f"= {source} (inchangé" + (f", {attente} image(s) en attente)" if attente else ")"))
                total_attente += attente
                continue
        print(f"> {source}")

        stats = {"images": 0, "en_attente": 0, "vues": set()}
        morceaux = [(page, m) for page, texte in pages(fichier, stats) for m in decouper(texte)]
        qdrant.delete(COLLECTION, points_selector=models.FilterSelector(filter=filtre))
        if not morceaux:  # rien n'est enregistré : le fichier sera retraité au prochain lancement
            if stats["en_attente"]:
                print(f"! {source} : rien d'indexable sans les images, {stats['en_attente']} en attente")
                total_attente += stats["en_attente"]
            else:
                print(f"! {source} : format non géré ou vide")
            continue
        for i in range(0, len(morceaux), 16):
            lot = morceaux[i : i + 16]
            vecteurs = embed([m for _, m in lot])
            qdrant.upsert(COLLECTION, points=[
                models.PointStruct(
                    id=str(uuid.uuid5(uuid.NAMESPACE_URL, f"{source}#{i + j}")),
                    vector=v,
                    payload={"source": source, "page": page, "texte": m, "empreinte": empreinte, "num": i + j,
                             "version": VERSION, "images_en_attente": stats["en_attente"]},
                )
                for j, ((page, m), v) in enumerate(zip(lot, vecteurs))
            ])
        total_attente += stats["en_attente"]
        attente = f", {stats['en_attente']} en attente" if stats["en_attente"] else ""
        print(f"+ {source} : {len(morceaux)} morceaux, {stats['images']} image(s){attente}")

    if total_attente:
        print(f"\n{total_attente} image(s) non lue(s) : relancer ingest.py avec un modèle vision actif "
              "(ex. ./scripts/modeles.py utiliser nemotron-3.5-super-vl-120b-a12b-nvfp4).")


if __name__ == "__main__":
    main()
