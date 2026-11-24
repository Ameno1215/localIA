"""Journal structuré du RAG, pour comparer les modèles et suivre la qualité de la recherche dans le temps.

Fichiers (dossier RAG_LOG_DIR, WS_DIR/logs/rag sur le Spark), un par mois :
  rag-AAAA-MM.jsonl        une ligne par recherche ou résolution de références : ids, scores, durées.
                           Pas de texte libre, seulement l'empreinte de la question. Conservé.
  questions-AAAA-MM.jsonl  texte des questions (RAG_LOG_QUESTIONS=texte seulement), supprimé
                           après RAG_LOG_RETENTION_DAYS jours : il peut contenir des informations de travail.
  index_version            écrit par ingest.py : version de l'index, pour comparer à index égal.

RAG_LOG_QUESTIONS : texte | hash (défaut) | non   (non : ni texte ni empreinte)
"""

import hashlib
import json
import os
import threading
import time
from pathlib import Path

DOSSIER = Path(os.environ.get("RAG_LOG_DIR", "/data/logs/rag"))
QUESTIONS = os.environ.get("RAG_LOG_QUESTIONS", "hash").lower()
RETENTION_JOURS = int(os.environ.get("RAG_LOG_RETENTION_DAYS", "90"))
ACTIF = os.environ.get("RAG_LOG", "1") == "1"

_verrou = threading.Lock()
_derniere_purge = 0.0


def empreinte(texte: str) -> str:
    return hashlib.sha256(texte.strip().lower().encode()).hexdigest()[:16]


def version_index() -> str | None:
    try:
        return ((DOSSIER / "index_version").read_text().split() or [None])[0]
    except OSError:
        return None


def ecrire_version_index(valeur: str) -> None:
    if ACTIF:
        DOSSIER.mkdir(parents=True, exist_ok=True)
        (DOSSIER / "index_version").write_text(valeur + "\n")


def _ajouter(prefixe: str, ligne: dict) -> None:
    DOSSIER.mkdir(parents=True, exist_ok=True)
    with (DOSSIER / f"{prefixe}-{time.strftime('%Y-%m')}.jsonl").open("a") as f:
        f.write(json.dumps(ligne, ensure_ascii=False) + "\n")


def _purger() -> None:
    """Supprime les fichiers de questions plus vieux que la rétention (au plus une fois par heure)."""
    global _derniere_purge
    if time.time() - _derniere_purge < 3600:
        return
    _derniere_purge = time.time()
    limite = time.time() - RETENTION_JOURS * 86400
    for f in DOSSIER.glob("questions-*.jsonl"):
        if f.stat().st_mtime < limite:
            f.unlink(missing_ok=True)


def recherche(id_requete: str, question: str, client: str | None, **champs) -> None:
    """Une recherche : la question selon RAG_LOG_QUESTIONS, le reste (ids, scores, durées) tel quel."""
    if not ACTIF:
        return
    ligne = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "type": "recherche", "id": id_requete, "client": client,
             "question_hash": empreinte(question) if QUESTIONS != "non" else None,
             "question_longueur": len(question), "index_version": version_index(), **champs}
    try:
        with _verrou:
            _ajouter("rag", ligne)
            if QUESTIONS == "texte":
                _ajouter("questions", {"ts": ligne["ts"], "id": id_requete, "question": question})
            _purger()
    except OSError:
        pass  # le journal ne doit jamais casser la recherche


def references(demandees: list[str], inconnues: list[str], modele: str | None, client: str | None) -> None:
    """Références citées dans une réponse (envoyées par le filtre Open WebUI) : les inconnues sont inventées."""
    if not ACTIF:
        return
    ligne = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "type": "references", "client": client, "modele": modele,
             "citees": len(demandees), "inconnues": len(inconnues), "refs": demandees, "refs_inconnues": inconnues,
             "index_version": version_index()}
    try:
        with _verrou:
            _ajouter("rag", ligne)
    except OSError:
        pass
