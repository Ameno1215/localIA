"""Panneau de contrôle de la stack IA locale.

Lancement :  ./scripts/panneau.sh   puis ouvrir http://localhost:7000
Écoute uniquement sur 127.0.0.1. Aucune dépendance (bibliothèque standard Python).
"""

import json
import os
import subprocess
import threading
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PORT = int(os.environ.get("PANNEAU_PORT", "7000"))
PROJET = Path(__file__).resolve().parent.parent
PAGE = Path(__file__).resolve().parent / "index.html"
PAGE_GUIDE = Path(__file__).resolve().parent / "guide.html"
GUIDE = PROJET / "GUIDE.md"

# Services du docker-compose.yml : rôle + sonde HTTP facultative (depuis l'hôte)
SERVICES = {
    "vllm": {"role": "LLM sur GPU (qwen-local)", "sonde": "http://127.0.0.1:8000/health", "lien": None},
    "litellm": {"role": "Passerelle : clés, quotas, file", "sonde": "http://127.0.0.1:4000/health/liveliness", "lien": "http://localhost:4000/ui"},
    "postgres": {"role": "Base de LiteLLM (clés, conso)", "sonde": None, "lien": None},
    "open-webui": {"role": "Chat web multi-comptes", "sonde": "http://127.0.0.1:3000/health", "lien": "http://localhost:3000"},
    "qdrant": {"role": "Base vectorielle du RAG", "sonde": "http://127.0.0.1:6333/readyz", "lien": "http://localhost:6333/dashboard"},
    "tei-embed": {"role": "Embeddings bge-m3", "sonde": None, "lien": None},
    "tei-rerank": {"role": "Reranker bge-reranker-v2-m3", "sonde": None, "lien": None},
    "mcp-docs": {"role": "Recherche dans les normes (MCP)", "sonde": None, "lien": None},
    "caddy": {"role": "Accès HTTPS depuis le réseau (Spark)", "sonde": None, "lien": None},
}

# Seuls les services définis par la config en cours sont gérés (caddy n'existe que sur le Spark)
_definis = subprocess.run(["docker", "compose", "config", "--services"], cwd=PROJET, capture_output=True, text=True).stdout.split()
if _definis:
    SERVICES = {n: v for n, v in SERVICES.items() if n in _definis}

# Modes prêts à l'emploi : services à démarrer (les autres sont arrêtés)
MODES = {
    "tout": list(SERVICES),
    "chat": ["vllm", "litellm", "postgres", "open-webui", "caddy"],
    "agent": ["vllm", "litellm", "postgres", "qdrant", "tei-embed", "tei-rerank", "mcp-docs", "caddy"],
    "finetune": ["qdrant", "tei-embed", "tei-rerank", "mcp-docs", "caddy"],
    "rien": [],
}
MODES = {nom: [s for s in svcs if s in SERVICES] for nom, svcs in MODES.items()}

verrou = threading.Lock()  # une action docker à la fois
verrou_index = threading.Lock()  # une indexation à la fois (longue : ne bloque pas les autres actions)
indexation = {"en_cours": False, "titre": "", "ok": None, "lignes": []}
OPTIONS_INDEX = {"nouveaux": [], "tout": ["--reset"], "texte": ["--sans-images"]}


def lancer(cmd: list[str], timeout: int = 300, limite: int | None = 6000) -> dict:
    """Exécute une commande dans le dossier du projet ; `limite` tronque la sortie affichée."""
    try:
        r = subprocess.run(cmd, cwd=PROJET, capture_output=True, text=True, timeout=timeout, env=env_commandes())
        sortie = r.stdout + r.stderr
        return {"ok": r.returncode == 0, "sortie": sortie[-limite:] if limite else sortie}
    except subprocess.TimeoutExpired:
        return {"ok": False, "sortie": f"Délai dépassé ({timeout} s)"}
    except FileNotFoundError as e:
        return {"ok": False, "sortie": str(e)}


def env_commandes() -> dict:
    return dict(os.environ, PATH=f"{Path.home()}/.local/bin:{os.environ.get('PATH', '')}")


def configs_modifiees(services: list[str]) -> list[str]:
    """Services dont un fichier monté (Caddyfile, litellm.yaml…) a changé depuis leur démarrage.

    `docker compose up -d` ne regarde que le docker-compose : il ne voit pas ces changements.
    """
    ids = lancer(["docker", "compose", "ps", "-q", *services], timeout=30, limite=None)["sortie"].split()
    if not ids:
        return []
    r = lancer(["docker", "inspect", *ids], timeout=30, limite=None)
    try:
        conteneurs = json.loads(r["sortie"])
    except ValueError:
        return []
    a_relancer = []
    for c in conteneurs:
        if not c["State"].get("Running"):
            continue
        debut = datetime.fromisoformat(c["State"]["StartedAt"][:19]).replace(tzinfo=timezone.utc).timestamp()
        for m in c.get("Mounts", []):
            source = Path(m.get("Source", ""))
            if m.get("Type") == "bind" and source.is_file() and source.stat().st_mtime > debut:
                a_relancer.append(c["Config"]["Labels"]["com.docker.compose.service"])
                break
    return a_relancer


def demarrer(services: list[str]) -> dict:
    """up -d avec reconstruction des images maison (mcp-docs), puis redémarrage des services dont la config a changé."""
    r = lancer(["docker", "compose", "up", "-d", "--build", *services], timeout=1200)
    sortie = r["sortie"]
    if r["ok"]:
        changes = configs_modifiees(services)
        if changes:
            sortie += f"\nConfiguration modifiée, redémarrage : {', '.join(changes)}\n"
            r2 = lancer(["docker", "compose", "restart", *changes])
            sortie += r2["sortie"]
            return {"ok": r2["ok"], "sortie": sortie}
    return {"ok": r["ok"], "sortie": sortie}


def dossier_docs() -> dict:
    """Dossier des documents vu par mcp-docs (/docs) et nombre de fichiers."""
    r = lancer(["docker", "compose", "config", "--format", "json"], timeout=30, limite=None)
    try:
        volumes = json.loads(r["sortie"])["services"]["mcp-docs"].get("volumes", [])
    except (ValueError, KeyError):
        return {"dossier": None, "fichiers": None}
    for v in volumes:
        if v.get("target") == "/docs":
            d = Path(v["source"])
            fichiers = sum(1 for f in d.rglob("*") if f.is_file()) if d.is_dir() else None
            return {"dossier": str(d), "fichiers": fichiers}
    return {"dossier": None, "fichiers": None}


def indexer(mode: str) -> None:
    """Lance ingest.py dans mcp-docs (journal suivi en direct), puis redémarre mcp-docs pour la liste des documents."""
    titres = {"nouveaux": "Indexation des documents nouveaux ou modifiés", "tout": "Réindexation complète",
              "texte": "Indexation du texte seul (images en attente)"}
    indexation.update(en_cours=True, titre=titres[mode], ok=None, lignes=[])
    try:
        p = subprocess.Popen(["docker", "compose", "exec", "-T", "mcp-docs", "python", "-u", "ingest.py", *OPTIONS_INDEX[mode]],
                             cwd=PROJET, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env_commandes())
        for ligne in p.stdout:
            indexation["lignes"].append(ligne.rstrip("\n"))
            del indexation["lignes"][:-3000]
        ok = p.wait() == 0
        if ok:
            indexation["lignes"].append("\nRedémarrage de mcp-docs (liste des documents à jour)…")
            r = lancer(["docker", "compose", "restart", "mcp-docs"])
            indexation["lignes"].append(r["sortie"].strip() or "ok")
            ok = r["ok"]
        else:
            indexation["lignes"].append("\nÉchec. mcp-docs doit tourner (Tout démarrer ou mode agent).")
        indexation["ok"] = ok
    except Exception as e:
        indexation["lignes"].append(f"Erreur : {e}")
        indexation["ok"] = False
    finally:
        indexation["en_cours"] = False
        verrou_index.release()


def sonder(url: str | None) -> bool | None:
    if not url:
        return None
    try:
        with urllib.request.urlopen(url, timeout=2) as r:
            return r.status < 500
    except Exception:
        return False


def modele_vllm() -> str:
    """Dossier du modèle servi (VLLM_MODEL dans .env)."""
    for ligne in (PROJET / ".env").read_text().splitlines():
        if ligne.startswith("VLLM_MODEL="):
            return ligne.split("=", 1)[1].strip()
    return "?"


def etat_services() -> list[dict]:
    r = lancer(["docker", "compose", "ps", "-a", "--format", "json"], timeout=30, limite=None)
    conteneurs = {}
    for ligne in r["sortie"].splitlines():
        ligne = ligne.strip()
        if ligne.startswith("{"):
            c = json.loads(ligne)
            conteneurs[c["Service"]] = c
    stats = {}
    s = lancer(["docker", "stats", "--no-stream", "--format", "{{.Name}}\t{{.MemUsage}}\t{{.CPUPerc}}"], timeout=30, limite=None)
    for ligne in s["sortie"].splitlines():
        parts = ligne.split("\t")
        if len(parts) == 3:
            stats[parts[0]] = {"ram": parts[1].split(" / ")[0], "cpu": parts[2]}
    resultat = []
    for nom, info in SERVICES.items():
        c = conteneurs.get(nom, {})
        etat = c.get("State", "absent")
        pret = sonder(info["sonde"]) if etat == "running" else None
        st = stats.get(c.get("Name", ""), {})
        role = f"LLM sur GPU : {modele_vllm()}" if nom == "vllm" else info["role"]
        resultat.append({
            "nom": nom, "role": role, "lien": info["lien"], "etat": etat,
            "sante": c.get("Health", ""), "pret": pret, "statut": c.get("Status", ""),
            "ram": st.get("ram", ""), "cpu": st.get("cpu", ""),
        })
    return resultat


def etat_machine() -> dict:
    gpu = lancer(["nvidia-smi", "--query-gpu=name,memory.used,memory.total,utilization.gpu,driver_version",
                  "--format=csv,noheader,nounits"], timeout=10)
    g = [x.strip() for x in gpu["sortie"].split(",")] if gpu["ok"] else []
    mem = {}
    for ligne in Path("/proc/meminfo").read_text().splitlines():
        k, v = ligne.split(":", 1)
        mem[k] = int(v.split()[0]) // 1024  # Mo
    def entier(v):
        try:
            return int(v)
        except ValueError:
            return None  # « [N/A] » : sur le Spark (mémoire unifiée), nvidia-smi ne donne pas la mémoire du GPU

    return {
        "gpu": {"nom": g[0], "utilise": entier(g[1]), "total": entier(g[2]), "charge": entier(g[3]), "driver": g[4]} if len(g) == 5 else None,
        "ram": {"total": mem["MemTotal"], "dispo": mem["MemAvailable"]},
    }


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _json(self, code: int, obj) -> None:
        corps = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(corps)))
        self.end_headers()
        self.wfile.write(corps)

    def _hote_valide(self) -> bool:
        # Protège contre le DNS rebinding : seul localhost est accepté
        return self.headers.get("Host", "").split(":")[0] in {"localhost", "127.0.0.1"}

    def do_GET(self):
        if not self._hote_valide():
            return self._json(403, {"erreur": "hôte refusé"})
        fichiers = {"/": (PAGE, "text/html"), "/index.html": (PAGE, "text/html"),
                    "/guide": (PAGE_GUIDE, "text/html"), "/guide.md": (GUIDE, "text/markdown")}
        if self.path in fichiers:
            chemin, type_ = fichiers[self.path]
            corps = chemin.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", f"{type_}; charset=utf-8")
            self.send_header("Content-Length", str(len(corps)))
            self.end_headers()
            return self.wfile.write(corps)
        if self.path == "/api/etat":
            return self._json(200, {"services": etat_services(), "machine": etat_machine()})
        if self.path == "/api/indexation":
            return self._json(200, {**indexation, "lignes": "\n".join(indexation["lignes"][-400:]), **dossier_docs()})
        if self.path.startswith("/api/logs/"):
            nom = self.path.rsplit("/", 1)[-1]
            if nom not in SERVICES:
                return self._json(404, {"erreur": "service inconnu"})
            return self._json(200, lancer(["docker", "compose", "logs", "--no-color", "--tail", "120", nom], timeout=30))
        self._json(404, {"erreur": "introuvable"})

    def do_POST(self):
        # Requête de la page uniquement : bon hôte + en-tête personnalisé (bloque les formulaires d'autres sites)
        if not self._hote_valide() or self.headers.get("X-Panneau") != "1":
            return self._json(403, {"erreur": "requête refusée"})
        parties = self.path.strip("/").split("/")  # api/<action>/<cible>
        if len(parties) != 3 or parties[0] != "api":
            return self._json(404, {"erreur": "introuvable"})
        _, action, cible = parties
        if action == "indexer" and cible in OPTIONS_INDEX:
            if not verrou_index.acquire(blocking=False):
                return self._json(409, {"ok": False, "sortie": "Une indexation est déjà en cours."})
            threading.Thread(target=indexer, args=(cible,), daemon=True).start()
            return self._json(200, {"ok": True, "sortie": "Indexation lancée."})
        if not verrou.acquire(timeout=1):
            return self._json(409, {"ok": False, "sortie": "Une action est déjà en cours, patiente."})
        try:
            if action in ("demarrer", "arreter", "redemarrer") and cible in SERVICES:
                if action == "demarrer":
                    return self._json(200, demarrer([cible]))
                cmd = {"arreter": ["stop"], "redemarrer": ["restart"]}[action]
                return self._json(200, lancer(["docker", "compose", *cmd, cible]))
            if action == "mode" and cible in MODES:
                garder = MODES[cible]
                arreter = [s for s in SERVICES if s not in garder]
                sortie = ""
                if arreter:
                    sortie += lancer(["docker", "compose", "stop", *arreter])["sortie"]
                if garder:
                    r = demarrer(garder)
                    sortie += r["sortie"]
                    return self._json(200, {"ok": r["ok"], "sortie": sortie})
                return self._json(200, {"ok": True, "sortie": sortie})
            return self._json(400, {"erreur": "action inconnue"})
        finally:
            verrou.release()


if __name__ == "__main__":
    print(f"Panneau IA locale : http://localhost:{PORT}  (Ctrl+C pour arrêter)")
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
