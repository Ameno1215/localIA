#!/usr/bin/env python3
"""Évaluation fixe du modèle actif : qualité (normes, code, outils) et performance (charge).

  ./scripts/evaluer.py                    tout, pour le profil actif (VLLM_PROFIL du .env)
  ./scripts/evaluer.py --seulement normes,charge
  ./scripts/evaluer.py --concurrences 1,4,8,16

Le trafic réel change d'une semaine à l'autre : seul ce jeu fixe compare les modèles à conditions égales.
Lancé automatiquement à la fin de  ./scripts/modeles.py utiliser <profil>  (--sans-eval pour l'éviter).

Jeux de cas : evals/*.jsonl (dans le dépôt, à compléter avec vos propres questions)
  normes.jsonl  question, faits attendus (regex), sources attendues : le modèle doit chercher via
                rechercher_docs (le vrai RAG de mcp-docs), répondre juste et citer la bonne source
  code.jsonl    consigne + tests pytest, exécutés dans un conteneur sans réseau
  outils.jsonl  le modèle appelle-t-il l'outil quand il le faut, et seulement alors ?
Résultats : WS_DIR/evals/<date>_<profil>.jsonl (détail) et .resume.json (lu par rapport-modeles.py).

Appelle LiteLLM (127.0.0.1:4000) avec la clé maître, comme un client. Température 0, graine fixe.
Bibliothèque standard uniquement.
"""

import argparse
import json
import re
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

PROJET = Path(__file__).resolve().parent.parent
EVALS = PROJET / "evals"
BLOC = re.compile(r"\[réf ([0-9a-f]{8}) \| ([^|\]]+?)(?:, p\.\d+)? \|")
CITE = re.compile(r"\[\s*(?:réf\.?\s*)?([0-9a-f]{8})\s*\]", re.IGNORECASE)
CODE = re.compile(r"```(?:python|py)?\s*\n(.*?)```", re.DOTALL)
IMAGE_TESTS = "python:3.12-slim"


def lire_env(chemin: Path) -> dict[str, str]:
    res = {}
    if chemin.exists():
        for ligne in chemin.read_text().splitlines():
            if ligne.strip() and not ligne.startswith("#") and "=" in ligne:
                k, _, v = ligne.partition("=")
                res[k.strip()] = v.strip()
    return res


def ws_dir(env: dict) -> Path:
    v = env.get("WS_DIR", "").strip()
    if not v:
        return PROJET
    p = Path(v).expanduser()
    return p if p.is_absolute() else (PROJET / p).resolve()


def cas(nom: str) -> list[dict]:
    f = EVALS / f"{nom}.jsonl"
    return [json.loads(l) for l in f.read_text().splitlines() if l.strip()] if f.exists() else []


class Client:
    def __init__(self, url: str, cle: str, modele: str):
        self.url, self.cle, self.modele = url.rstrip("/"), cle, modele

    def _requete(self, corps: dict, flux: bool = False):
        corps = {"model": self.modele, "temperature": 0, "seed": 1, **corps}
        if flux:
            corps |= {"stream": True, "stream_options": {"include_usage": True}}
        req = urllib.request.Request(f"{self.url}/v1/chat/completions", data=json.dumps(corps).encode(),
                                     headers={"Authorization": f"Bearer {self.cle}", "Content-Type": "application/json"})
        return urllib.request.urlopen(req, timeout=600)

    def chat(self, messages: list, outils: list | None = None, max_tokens: int = 2048) -> dict:
        corps = {"messages": messages, "max_tokens": max_tokens}
        if outils:
            corps["tools"] = outils
        t0 = time.perf_counter()
        with self._requete(corps) as r:
            res = json.load(r)
        res["_duree_s"] = time.perf_counter() - t0
        return res

    def flux(self, prompt: str, max_tokens: int) -> dict:
        """Une requête en streaming : TTFT, durée, jetons générés."""
        t0 = time.perf_counter()
        premier, jetons = None, 0
        with self._requete({"messages": [{"role": "user", "content": prompt}], "max_tokens": max_tokens}, flux=True) as r:
            for ligne in r:
                ligne = ligne.decode().strip()
                if not ligne.startswith("data:") or ligne == "data: [DONE]":
                    continue
                d = json.loads(ligne[5:])
                delta = (d.get("choices") or [{}])[0].get("delta") or {}
                if premier is None and (delta.get("content") or delta.get("reasoning_content") or delta.get("reasoning")):
                    premier = time.perf_counter()
                if d.get("usage"):
                    jetons = d["usage"].get("completion_tokens") or jetons
        fin = time.perf_counter()
        premier = premier or fin
        return {"ttft_s": premier - t0, "duree_s": fin - t0, "jetons": jetons,
                "jetons_par_s": jetons / (fin - premier) if fin - premier > 0.05 else None}


# ----------------------------------------------------------------------------- RAG (le vrai, dans mcp-docs)

def rag(question: str, nb: int = 5) -> str:
    """Appelle rechercher_docs dans le conteneur mcp-docs, sans écrire dans le journal RAG (trafic réel seulement)."""
    code = "import sys, server; print(server.rechercher_docs(sys.argv[1], int(sys.argv[2])))"
    r = subprocess.run(["docker", "compose", "exec", "-T", "-e", "RAG_LOG=0", "mcp-docs", "python", "-c", code, question, str(nb)],
                       cwd=PROJET, capture_output=True, text=True, timeout=120)
    if r.returncode != 0:
        raise RuntimeError(f"rechercher_docs a échoué : {r.stderr.strip()[-300:]}")
    return r.stdout.strip()


def description_outil() -> str:
    code = "import server; print(server._description_outil())"
    r = subprocess.run(["docker", "compose", "exec", "-T", "-e", "RAG_LOG=0", "mcp-docs", "python", "-c", code],
                       cwd=PROJET, capture_output=True, text=True, timeout=60)
    return r.stdout.strip() if r.returncode == 0 else "Cherche dans la base documentaire interne (normes)."


def outil_rag(description: str) -> list[dict]:
    return [{"type": "function", "function": {
        "name": "rechercher_docs", "description": description,
        "parameters": {"type": "object", "properties": {"question": {"type": "string"}, "nb_resultats": {"type": "integer", "default": 5}},
                       "required": ["question"]}}}]


def agent(client: Client, question: str, outils: list, tours: int = 4) -> dict:
    """Boucle d'appels d'outils, comme OpenCode / Open WebUI. Rend la réponse finale et ce qui a été cherché."""
    messages = [{"role": "user", "content": question}]
    appels, retrouves, jetons, duree = [], {}, 0, 0.0
    for _ in range(tours):
        r = client.chat(messages, outils)
        duree += r["_duree_s"]
        jetons += (r.get("usage") or {}).get("completion_tokens") or 0
        msg = r["choices"][0]["message"]
        if not msg.get("tool_calls"):
            return {"reponse": msg.get("content") or "", "appels": appels, "retrouves": retrouves, "jetons": jetons, "duree_s": duree}
        messages.append({k: v for k, v in msg.items() if k in ("role", "content", "tool_calls")})
        for tc in msg["tool_calls"]:
            nom = tc["function"]["name"]
            try:
                args = json.loads(tc["function"]["arguments"] or "{}")
                valide = isinstance(args.get("question"), str) and bool(args["question"].strip())
            except ValueError:
                args, valide = {}, False
            appels.append({"outil": nom, "arguments_valides": valide})
            if nom == "rechercher_docs" and valide:
                texte = rag(args["question"], int(args.get("nb_resultats") or 5))
                retrouves |= {ref: src.strip() for ref, src in BLOC.findall(texte)}
            else:
                texte = "Erreur : outil inconnu ou arguments invalides."
            messages.append({"role": "tool", "tool_call_id": tc["id"], "content": texte})
    return {"reponse": "", "appels": appels, "retrouves": retrouves, "jetons": jetons, "duree_s": duree, "erreur": "trop de tours"}


# ----------------------------------------------------------------------------- jeux

def eval_normes(client: Client, outils: list) -> list[dict]:
    res = []
    for c in cas("normes"):
        try:
            r = agent(client, c["question"], outils)
        except Exception as e:
            res.append({"jeu": "normes", "id": c["id"], "erreur": str(e)[:300], "score": 0})
            continue
        texte = r["reponse"]
        faits = [bool(re.search(f, texte, re.IGNORECASE)) for f in c.get("faits", [])]
        interdits = [bool(re.search(f, texte, re.IGNORECASE)) for f in c.get("interdits", [])]
        cites = {x.lower() for x in CITE.findall(texte)}
        inventees = [x for x in cites if x not in r["retrouves"]]
        bonne_source = any(any(s.lower() in r["retrouves"].get(x, "").lower() for s in c.get("sources", [])) for x in cites)
        score = (sum(faits) / len(faits) if faits else 1.0) * (0.0 if any(interdits) else 1.0)
        res.append({"jeu": "normes", "id": c["id"], "score": round(score, 3), "faits": faits, "a_cherche": bool(r["appels"]),
                    "source_citee": bonne_source, "citees": len(cites), "inventees": len(inventees),
                    "jetons": r["jetons"], "duree_s": round(r["duree_s"], 2), "reponse": texte})
    return res


def eval_outils(client: Client, outils: list) -> list[dict]:
    res = []
    for c in cas("outils"):
        try:
            r = client.chat([{"role": "user", "content": c["question"]}], outils, max_tokens=1024)
        except Exception as e:
            res.append({"jeu": "outils", "id": c["id"], "erreur": str(e)[:300], "score": 0})
            continue
        appels = r["choices"][0]["message"].get("tool_calls") or []
        noms = [a["function"]["name"] for a in appels]
        valides = all(_json_valide(a["function"]["arguments"]) for a in appels)
        attendu = c.get("outil_attendu")
        ok = (attendu in noms and valides) if attendu else not noms
        res.append({"jeu": "outils", "id": c["id"], "score": 1.0 if ok else 0.0, "appels": noms, "arguments_valides": valides})
    return res


def _json_valide(texte: str) -> bool:
    try:
        json.loads(texte or "{}")
        return True
    except ValueError:
        return False


def eval_code(client: Client) -> list[dict]:
    res = []
    for c in cas("code"):
        consigne = c["consigne"] + "\nRéponds avec un seul bloc ```python contenant tout le module (imports compris), sans les tests."
        try:
            r = client.chat([{"role": "user", "content": consigne}], max_tokens=4096)
        except Exception as e:
            res.append({"jeu": "code", "id": c["id"], "erreur": str(e)[:300], "score": 0})
            continue
        texte = r["choices"][0]["message"].get("content") or ""
        blocs = CODE.findall(texte)
        code = max(blocs, key=len) if blocs else ""
        ok, sortie = executer_tests(code, c["tests"]) if code else (False, "aucun bloc de code")
        res.append({"jeu": "code", "id": c["id"], "score": 1.0 if ok else 0.0, "sortie": sortie[-1500:],
                    "jetons": (r.get("usage") or {}).get("completion_tokens"), "duree_s": round(r["_duree_s"], 2)})
    return res


def executer_tests(code: str, tests: str) -> tuple[bool, str]:
    """pytest dans un conteneur jetable, SANS réseau ni accès au Spark (le code vient du modèle).
    pytest s'installe hors ligne depuis wheels/ du dépôt."""
    with tempfile.TemporaryDirectory() as d:
        Path(d, "solution.py").write_text(code)
        Path(d, "test_solution.py").write_text(tests)
        cmd = ["docker", "run", "--rm", "--network", "none", "--memory", "1g", "--cpus", "2", "--pids-limit", "256",
               "-v", f"{d}:/w", "-v", f"{PROJET / 'wheels'}:/wheels:ro", "-w", "/w", IMAGE_TESTS, "sh", "-c",
               "pip install -q --no-index --find-links /wheels pytest >/dev/null 2>&1 && python -m pytest -q -p no:cacheprovider 2>&1"]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
        except subprocess.TimeoutExpired:
            return False, "délai dépassé (180 s)"
        return r.returncode == 0, r.stdout + r.stderr


PROMPT_CHARGE = ("Explique en détail, en français, le fonctionnement d'un protocole de communication série entre une carte "
                 "à puce et un lecteur : initialisation, échange de commandes, gestion des erreurs. Au moins 400 mots.")


def eval_charge(client: Client, concurrences: list[int], max_tokens: int) -> list[dict]:
    """Même requête lancée N fois en parallèle : TTFT et débit par requête, débit total de la pile."""
    res = []
    client.flux("Bonjour", 8)  # échauffement (compilation, cache)
    for n in concurrences:
        t0 = time.perf_counter()
        with ThreadPoolExecutor(n) as ex:
            mesures = list(ex.map(lambda _: _sur(client.flux, PROMPT_CHARGE, max_tokens), range(n)))
        mur = time.perf_counter() - t0
        bonnes = [m for m in mesures if "erreur" not in m]
        total = sum(m["jetons"] for m in bonnes)
        debits = [m["jetons_par_s"] for m in bonnes if m["jetons_par_s"]]
        res.append({"jeu": "charge", "id": f"concurrence-{n}", "concurrence": n, "erreurs": n - len(bonnes),
                    "ttft_p50_s": _r(statistics.median([m["ttft_s"] for m in bonnes])) if bonnes else None,
                    "ttft_max_s": _r(max(m["ttft_s"] for m in bonnes)) if bonnes else None,
                    "jetons_par_s_requete": _r(statistics.median(debits), 1) if debits else None,
                    "jetons_par_s_total": _r(total / mur, 1) if mur else None})
        print(f"  concurrence {n:>2} : {res[-1]['jetons_par_s_total']} jetons/s au total, "
              f"TTFT médian {res[-1]['ttft_p50_s']} s", flush=True)
    return res


def _sur(f, *args):
    try:
        return f(*args)
    except Exception as e:
        return {"erreur": str(e)[:200]}


def _r(x, n=2):
    return None if x is None else round(x, n)


def moyenne(lignes: list[dict], cle: str = "score") -> float | None:
    v = [l[cle] for l in lignes if isinstance(l.get(cle), (int, float, bool))]
    return round(100 * statistics.mean(v), 1) if v else None


def main() -> None:
    ap = argparse.ArgumentParser(description="Évaluation fixe du modèle actif")
    ap.add_argument("--env", default=".env")
    ap.add_argument("--url", default="http://127.0.0.1:4000")
    ap.add_argument("--modele", default="qwen-local")
    ap.add_argument("--seulement", default="normes,outils,code,charge")
    ap.add_argument("--concurrences", default="1,4,8")
    ap.add_argument("--max-tokens-charge", type=int, default=512)
    a = ap.parse_args()
    env = lire_env(PROJET / a.env)
    if not env.get("LITELLM_MASTER_KEY"):
        sys.exit("LITELLM_MASTER_KEY absente du .env")
    profil = env.get("VLLM_PROFIL") or env.get("VLLM_MODEL") or "inconnu"
    jeux = set(a.seulement.split(","))
    client = Client(a.url, env["LITELLM_MASTER_KEY"], a.modele)
    lignes: list[dict] = []
    outils = outil_rag(description_outil()) if jeux & {"normes", "outils"} else []

    for nom, f in [("normes", lambda: eval_normes(client, outils)), ("outils", lambda: eval_outils(client, outils)),
                   ("code", lambda: eval_code(client))]:
        if nom in jeux:
            print(f"Jeu « {nom} »…", flush=True)
            r = f()
            lignes += r
            print(f"  {moyenne(r)} % ({len(r)} cas)", flush=True)
    if "charge" in jeux:
        print("Charge…", flush=True)
        lignes += eval_charge(client, [int(x) for x in a.concurrences.split(",")], a.max_tokens_charge)

    normes = [l for l in lignes if l["jeu"] == "normes"]
    charge = {l["concurrence"]: l for l in lignes if l["jeu"] == "charge"}
    scores = {
        "normes_pct": moyenne(normes),
        "normes_a_cherche_pct": moyenne(normes, "a_cherche"),
        "normes_source_citee_pct": moyenne(normes, "source_citee"),
        "normes_refs_inventees": sum(l.get("inventees", 0) for l in normes) if normes else None,
        "outils_pct": moyenne([l for l in lignes if l["jeu"] == "outils"]),
        "code_pct": moyenne([l for l in lignes if l["jeu"] == "code"]),
    }
    for n, l in charge.items():
        scores |= {f"c{n}_ttft_s": l["ttft_p50_s"], f"c{n}_jetons_s_requete": l["jetons_par_s_requete"],
                   f"c{n}_jetons_s_total": l["jetons_par_s_total"]}
    scores = {k: v for k, v in scores.items() if v is not None}

    date = time.strftime("%Y-%m-%d_%H%M")
    sortie = ws_dir(env) / "evals"
    sortie.mkdir(parents=True, exist_ok=True)
    (sortie / f"{date}_{profil}.jsonl").write_text("".join(json.dumps(l, ensure_ascii=False) + "\n" for l in lignes))
    resume = {"profil": profil, "date": date, "modele_servi": a.modele, "scores": scores,
              "cas": {j: len(cas(j)) for j in ("normes", "outils", "code")}}
    (sortie / f"{date}_{profil}.resume.json").write_text(json.dumps(resume, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(scores, ensure_ascii=False, indent=2))
    print(f"Détail : {sortie / f'{date}_{profil}.jsonl'}")


if __name__ == "__main__":
    main()
