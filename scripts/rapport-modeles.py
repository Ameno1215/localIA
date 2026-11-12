#!/usr/bin/env python3
"""Compare les modèles (profils vLLM) sur le trafic réel et les évaluations : une ligne par profil.

  ./scripts/rapport-modeles.py                      tout l'historique
  ./scripts/rapport-modeles.py --depuis 2026-11-01  à partir d'une date
  ./scripts/rapport-modeles.py --csv rapport.csv    en plus, tableau CSV

Sources (toutes en lecture seule) :
  WS_DIR/logs/profils.jsonl      quel profil était actif quand (écrit par modeles.py utiliser)
  Postgres LiteLLM_SpendLogs      une ligne par requête : heures, TTFT, jetons, clé (jamais le texte)
  WS_DIR/open-webui/webui.db      pouces haut/bas d'Open WebUI
  WS_DIR/logs/rag/rag-*.jsonl     recherches RAG (durées, scores) et références inventées
  Prometheus (127.0.0.1:9090)     TTFT et latence p95 mesurés par vLLM, puissance GPU
  WS_DIR/evals/*.resume.json      dernières évaluations fixes (scripts/evaluer.py)

Seuls des agrégats sont affichés : ni prompt, ni conversation, ni nom d'utilisateur.
Bibliothèque standard uniquement.
"""

import argparse
import csv
import hashlib
import json
import sqlite3
import statistics
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

PROJET = Path(__file__).resolve().parent.parent
INCONNU = "(avant le journal)"


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


def epoch(ts: str) -> float:
    return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%S%z").timestamp()


def centile(valeurs: list[float], q: float) -> float | None:
    if not valeurs:
        return None
    v = sorted(valeurs)
    return v[min(len(v) - 1, int(round(q * (len(v) - 1))))]


# ----------------------------------------------------------------------------- profils

def periodes(ws: Path) -> list[tuple[str, float, float, str]]:
    """[(profil, début, fin, mode)] d'après le journal : un changement de profil ouvre une période."""
    f = ws / "logs" / "profils.jsonl"
    actifs = []
    if f.exists():
        for l in f.read_text().splitlines():
            if l.strip():
                e = json.loads(l)
                # « configure » : utiliser --sans-redemarrer (duo), vLLM redémarré à la main juste après
                if e.get("evenement") in ("actif", "configure") and not (actifs and actifs[-1][0] == e["profil"]):
                    actifs.append((e["profil"], epoch(e["ts"]), e.get("mode", "simple")))
    res = [(INCONNU, 0.0, actifs[0][1] if actifs else float("inf"), "?")]
    for i, (nom, debut, mode) in enumerate(actifs):
        fin = actifs[i + 1][1] if i + 1 < len(actifs) else float("inf")
        res.append((nom, debut, fin, mode))
    return res


def profil_a(per: list, t: float) -> str:
    for nom, debut, fin, _ in per:
        if debut <= t < fin:
            return nom
    return INCONNU


# ----------------------------------------------------------------------------- LiteLLM

def requetes_litellm(depuis: float) -> list[dict]:
    """Lignes de LiteLLM_SpendLogs via psql dans le conteneur postgres (pas de pilote Python à installer)."""
    colonnes = psql("SELECT column_name FROM information_schema.columns WHERE table_name = 'LiteLLM_SpendLogs'")
    if colonnes is None:
        return []
    noms = set(colonnes.split())
    statut = '"status"' if "status" in noms else "''"
    sql = (
        'SELECT extract(epoch from "startTime"), extract(epoch from "endTime"), '
        'coalesce(extract(epoch from "completionStartTime"), 0), coalesce(model_group, model), call_type, '
        f'coalesce(prompt_tokens, 0), coalesce(completion_tokens, 0), api_key, {statut} '
        f'FROM "LiteLLM_SpendLogs" WHERE "startTime" >= to_timestamp({depuis}) AT TIME ZONE \'UTC\''
    )
    sortie = psql(sql)
    res = []
    for l in (sortie or "").splitlines():
        c = l.split("\t")
        if len(c) < 9:
            continue
        res.append({"debut": float(c[0]), "fin": float(c[1]), "premier": float(c[2]), "modele": c[3], "type": c[4],
                    "entree": int(c[5]), "sortie": int(c[6]), "cle": c[7], "statut": c[8]})
    return res


def psql(sql: str) -> str | None:
    r = subprocess.run(["docker", "compose", "exec", "-T", "postgres", "psql", "-U", "litellm", "-d", "litellm",
                        "-At", "-F", "\t", "-c", sql], cwd=PROJET, capture_output=True, text=True)
    if r.returncode != 0:
        print(f"⚠ Postgres illisible ({r.stderr.strip()[:200]}) : colonnes LiteLLM vides", file=sys.stderr)
        return None
    return r.stdout


# ----------------------------------------------------------------------------- Open WebUI

def avis_openwebui(ws: Path, depuis: float) -> list[tuple[float, int]]:
    """[(heure, +1/-1)] depuis la table feedback d'Open WebUI (ouverte en lecture seule)."""
    db = ws / "open-webui" / "webui.db"
    if not db.exists():
        return []
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        lignes = con.execute("SELECT created_at, data FROM feedback WHERE created_at >= ?", (int(depuis),)).fetchall()
        con.close()
    except sqlite3.Error as e:
        print(f"⚠ webui.db illisible ({e})", file=sys.stderr)
        return []
    res = []
    for cree, data in lignes:
        try:
            note = (json.loads(data) or {}).get("rating")
        except (TypeError, ValueError):
            continue
        if note in (1, -1, "1", "-1"):
            res.append((float(cree), int(note)))
    return res


# ----------------------------------------------------------------------------- RAG

def journal_rag(ws: Path, depuis: float) -> list[dict]:
    res = []
    for f in sorted((ws / "logs" / "rag").glob("rag-*.jsonl")):
        for l in f.read_text().splitlines():
            if l.strip():
                e = json.loads(l)
                e["t"] = epoch(e["ts"])
                if e["t"] >= depuis:
                    res.append(e)
    return res


# ----------------------------------------------------------------------------- Prometheus

def prometheus(url: str, requete: str, a: float) -> float | None:
    q = urllib.parse.urlencode({"query": requete, "time": f"{a:.0f}"})
    try:
        with urllib.request.urlopen(f"{url}/api/v1/query?{q}", timeout=10) as r:
            res = json.load(r)["data"]["result"]
        return float(res[0]["value"][1]) if res else None
    except Exception:
        return None


def mesures_prometheus(url: str, nom: str, debut: float, fin: float) -> dict:
    fin = min(fin, time.time())
    duree = int(fin - debut)
    if duree < 60 or nom == INCONNU:
        return {}
    f = f'profil="{nom}"'
    h = lambda m: f'histogram_quantile(0.95, sum by (le) (increase(vllm:{m}_bucket{{{f}}}[{duree}s])))'
    val = {
        "prom_ttft_p95_s": prometheus(url, h("time_to_first_token_seconds"), fin),
        "prom_latence_p95_s": prometheus(url, h("e2e_request_latency_seconds"), fin),
        "prom_attente_p95_s": prometheus(url, h("request_queue_time_seconds"), fin),
        "prom_jetons_generes": prometheus(url, f"sum(increase(vllm:generation_tokens_total{{{f}}}[{duree}s]))", fin),
        "gpu_puissance_moy_w": prometheus(url, f"avg(avg_over_time(gpu_puissance_watts[{duree}s]))", fin),
    }
    return {k: v for k, v in val.items() if v is not None and v == v}  # NaN exclu


# ----------------------------------------------------------------------------- évaluations

def evals(ws: Path) -> dict[str, dict]:
    """Dernier résumé d'évaluation par profil."""
    res = {}
    for f in sorted((ws / "evals").glob("*.resume.json")):
        r = json.loads(f.read_text())
        res[r["profil"]] = r
    return res


# ----------------------------------------------------------------------------- rapport

def arrondi(x, n=2):
    return None if x is None else round(x, n)


def main() -> None:
    ap = argparse.ArgumentParser(description="Comparaison des profils vLLM")
    ap.add_argument("--depuis", help="date ISO, ex. 2026-11-01")
    ap.add_argument("--env", default=".env")
    ap.add_argument("--prometheus", default="http://127.0.0.1:9090")
    ap.add_argument("--csv")
    a = ap.parse_args()
    env = lire_env(PROJET / a.env)
    ws = ws_dir(env)
    depuis = datetime.fromisoformat(a.depuis).replace(tzinfo=timezone.utc).timestamp() if a.depuis else 0.0
    per = periodes(ws)
    # LiteLLM stocke l'empreinte SHA-256 des clés : celle de la clé maître = Open WebUI, les autres = postes (OpenCode…)
    maitre = hashlib.sha256(env.get("LITELLM_MASTER_KEY", "").encode()).hexdigest()

    stats: dict[str, dict] = {}

    def s(nom):
        return stats.setdefault(nom, {"lat": [], "ttft": [], "debit": [], "entree": [], "sortie": [], "cles": set(),
                                      "web": 0, "postes": 0, "erreurs": 0, "n": 0, "plus": 0, "moins": 0,
                                      "rag_n": 0, "rag_t": [], "rag_score": [], "citees": 0, "inventees": 0})

    for r in requetes_litellm(depuis):
        if r["type"] not in ("acompletion", "completion", "atext_completion", "text_completion", "aresponses", "responses"):
            continue  # embeddings, etc.
        x = s(profil_a(per, r["debut"]))
        x["n"] += 1
        x["cles"].add(r["cle"])
        x["web" if r["cle"] == maitre else "postes"] += 1
        if r["statut"] and r["statut"] != "success":
            x["erreurs"] += 1
            continue
        x["lat"].append(r["fin"] - r["debut"])
        x["entree"].append(r["entree"])
        x["sortie"].append(r["sortie"])
        if r["premier"] and r["debut"] < r["premier"] < r["fin"]:  # streaming : heure du premier jeton connue
            x["ttft"].append(r["premier"] - r["debut"])
            if r["fin"] - r["premier"] > 0.2 and r["sortie"] > 1:
                x["debit"].append(r["sortie"] / (r["fin"] - r["premier"]))

    for t, note in avis_openwebui(ws, depuis):
        s(profil_a(per, t))["plus" if note > 0 else "moins"] += 1

    for e in journal_rag(ws, depuis):
        x = s(profil_a(per, e["t"]))
        if e["type"] == "recherche":
            x["rag_n"] += 1
            if "t_total_ms" in e:
                x["rag_t"].append(e["t_total_ms"])
            retenus = set(e.get("retenus") or [])
            scores = [c["score_rerank"] for c in e.get("candidats") or [] if c.get("ref") in retenus and "score_rerank" in c]
            if scores:
                x["rag_score"].append(statistics.mean(scores))
        elif e["type"] == "references":
            x["citees"] += e.get("citees", 0)
            x["inventees"] += e.get("inconnues", 0)

    ev = evals(ws)
    lignes = []
    for nom, debut, fin, mode in per:
        if nom not in stats and nom not in ev:
            continue
        x = s(nom)
        avis = x["plus"] + x["moins"]
        ligne = {
            "profil": nom, "mode": mode,
            "actif_depuis": "" if nom == INCONNU else datetime.fromtimestamp(debut).strftime("%Y-%m-%d %H:%M"),
            "requetes": x["n"], "open_webui": x["web"], "postes": x["postes"], "utilisateurs": len(x["cles"]),
            "erreurs_pct": arrondi(100 * x["erreurs"] / x["n"], 1) if x["n"] else None,
            "jetons_entree_moy": arrondi(statistics.mean(x["entree"]), 0) if x["entree"] else None,
            "jetons_sortie_moy": arrondi(statistics.mean(x["sortie"]), 0) if x["sortie"] else None,
            "latence_p50_s": arrondi(centile(x["lat"], 0.5)), "latence_p95_s": arrondi(centile(x["lat"], 0.95)),
            "ttft_p50_s": arrondi(centile(x["ttft"], 0.5)), "ttft_p95_s": arrondi(centile(x["ttft"], 0.95)),
            "jetons_par_s_p50": arrondi(centile(x["debit"], 0.5), 1),
            "avis_positifs_pct": arrondi(100 * x["plus"] / avis, 0) if avis else None, "avis": avis,
            "rag_recherches": x["rag_n"], "rag_p95_ms": arrondi(centile(x["rag_t"], 0.95), 0),
            "rag_score_moy": arrondi(statistics.mean(x["rag_score"]), 3) if x["rag_score"] else None,
            "refs_inventees_pct": arrondi(100 * x["inventees"] / x["citees"], 1) if x["citees"] else None,
        }
        ligne |= {k: arrondi(v, 2) for k, v in mesures_prometheus(a.prometheus, nom, max(debut, depuis), fin).items()}
        if nom in ev:
            r = ev[nom]
            ligne |= {"eval_date": r.get("date"), **{f"eval_{k}": v for k, v in (r.get("scores") or {}).items()}}
        lignes.append(ligne)

    if not lignes:
        sys.exit("Aucune donnée : la pile a-t-elle servi des requêtes depuis l'activation du journal ?")
    cles = list(dict.fromkeys(k for l in lignes for k in l))
    # Markdown transposé : une colonne par profil, plus lisible avec beaucoup de mesures
    print("| mesure | " + " | ".join(l["profil"] for l in lignes) + " |")
    print("|---|" + "---|" * len(lignes))
    for k in cles[1:]:
        print(f"| {k} | " + " | ".join("" if l.get(k) is None else str(l.get(k)) for l in lignes) + " |")
    if a.csv:
        with open(a.csv, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=cles)
            w.writeheader()
            w.writerows(lignes)
        print(f"\nCSV : {a.csv}", file=sys.stderr)


if __name__ == "__main__":
    main()
