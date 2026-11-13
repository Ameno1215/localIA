#!/usr/bin/env python3
"""Benchmarks publics (volet 1 du PLAN-EVALUATION.md) : comparer vite les profils et les installations, sans donnée interne.

  ./scripts/bench-public.py preparer                     images et jeux de données (Internet), une fois
  ./scripts/bench-public.py lancer                       préréglage « rapide » sur le profil actif (~1 h selon le modèle)
  ./scripts/bench-public.py lancer --preset complet      tout (une nuit)
  ./scripts/bench-public.py lancer --seulement code,charge
  ./scripts/bench-public.py lancer --sans-raisonnement   même modèle, réflexion désactivée (à comparer)
  ./scripts/bench-public.py lister                       exécutions passées

Familles :
  connaissances  MMLU-Pro et GPQA Diamond (lm-evaluation-harness)
  consignes      IFEval : le modèle respecte-t-il les consignes de forme
  code           HumanEval+ / MBPP+ (EvalPlus) et HumanEval C++ (MultiPL-E), tests exécutés sans réseau
  agent          Aider polyglot : corriger des exercices avec retour des tests (C++ seul en « rapide »)
  contexte       RULER (aiguilles, suivi de variables) et LongBench v2 (complet seulement)
  rag            BEIR (SciFact…) avec les embeddings et le reranker de mcp-docs, sans LLM
  charge         vllm bench serve : requêtes courtes (chat) et longues (agent), plusieurs concurrences

Le modèle est appelé directement sur vLLM (127.0.0.1:8000, clé VLLM_API_KEY) : le banc ne se mélange pas au trafic réel
compté par LiteLLM. --via-litellm pour passer par la passerelle.
Résultats : WS_DIR/benchmarks/resultats/<date>_<profil>/ (détail, réponses) et <date>_<profil>.resume.json, lu par
rapport-modeles.py qui moyenne les exécutions d'un même profil, même installation et même préréglage.
Bibliothèque standard uniquement (le reste tourne dans des conteneurs).
"""

import argparse
import hashlib
import io
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

PROJET = Path(__file__).resolve().parent.parent
IMAGE = "ia-locale/bench-public"
IMAGE_AIDER = "ia-locale/aider-bench"
AIDER_DEPOT, AIDER_COMMIT = "https://github.com/Aider-AI/aider.git", "5dc9490bb35f9729ef2c95d00a19ccd30c26339c"
POLYGLOT_DEPOT = "https://github.com/Aider-AI/polyglot-benchmark.git"
BEIR = "https://public.ukp.informatik.tu-darmstadt.de/thakur/BEIR/datasets/{}.zip"
FAMILLES = ["connaissances", "consignes", "code", "contexte", "rag", "agent", "charge"]

# Objectifs de service pour la charge (à ajuster, voir PLAN-EVALUATION.md § 6) : premier jeton, puis ≥ 15 jetons/s
SCENARIOS = {"chat": {"entree": 1000, "sortie": 500, "ttft_ms": 3000},
             "agent": {"entree": 32000, "sortie": 800, "ttft_ms": 15000}}
TPOT_MAX_MS = 66

# limite = nombre de questions PAR sous-tâche (0 = tout). Changer un préréglage rend ses anciens résultats incomparables :
# le rapport ne moyenne que des exécutions au préréglage identique (empreinte « jeux »).
PRESETS = {
    "rapide": {
        "connaissances": {"taches": "mmlu_pro_engineering,mmlu_pro_computer_science,gpqa_diamond_cot_zeroshot", "limite": 40},
        "consignes": {"taches": "ifeval", "limite": 100},
        "code": {"humaneval": 0, "cpp": 60},
        "contexte": {"ruler": "niah_single_2,niah_multikey_2,ruler_vt", "longueurs": [8192, 32768], "limite": 20},
        "rag": {"scifact": 0},
        "agent": {"langages": "cpp"},
        "charge": {"chat": [1, 8], "agent": [4]},
    },
    "complet": {
        "connaissances": {"taches": "mmlu_pro,gpqa_diamond_cot_zeroshot", "limite": 100},
        "consignes": {"taches": "ifeval", "limite": 0},
        "code": {"humaneval": 0, "mbpp": 0, "cpp": 0},
        "contexte": {"ruler": "ruler", "longueurs": [8192, 32768, 65536, 131072], "limite": 50,
                     "longbench": {"longueurs": "short,medium", "limite": 0}},
        "rag": {"scifact": 0, "nfcorpus": 0, "fiqa": 0},
        "agent": {"langages": ""},
        "charge": {"chat": [1, 4, 8, 16, 32], "agent": [1, 2, 4, 8]},
    },
}


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


def erreur(msg: str) -> None:
    sys.exit(f"✗ {msg}")


def lancer_cmd(cmd: list[str], env: dict | None = None, capture: bool = False, **kw) -> tuple[int, str]:
    """Affiche la sortie en direct ; la garde aussi si capture (pour lire le résumé d'Aider)."""
    print("  $ " + " ".join(cmd[:6]) + (" …" if len(cmd) > 6 else ""), flush=True)
    if not capture:
        return subprocess.run(cmd, env=env, **kw).returncode, ""
    p = subprocess.Popen(cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, **kw)
    lignes = []
    for l in p.stdout:
        print(l, end="", flush=True)
        lignes.append(l)
    return p.wait(), "".join(lignes)


class Banc:
    def __init__(self, a):
        self.a = a
        self.env = lire_env(PROJET / a.env)
        self.ws = ws_dir(self.env)
        self.bench = self.ws / "benchmarks"
        self.profil = self.env.get("VLLM_PROFIL") or self.env.get("VLLM_MODEL") or "inconnu"
        if getattr(a, "via_litellm", False):
            self.url, self.cle = "http://127.0.0.1:4000/v1", self.env.get("LITELLM_MASTER_KEY", "")
        else:
            self.url, self.cle = "http://127.0.0.1:8000/v1", self.env.get("VLLM_API_KEY", "")
        self.contexte = int(self.env.get("VLLM_MAX_MODEL_LEN") or 131072)
        self.concurrence = getattr(a, "concurrence", None) or int(self.env.get("VLLM_MAX_SEQS") or 8)

    # ------------------------------------------------------------------ conteneurs

    def docker_banc(self, args: list[str], reseau: bool = True, modeles: bool = False) -> int:
        cmd = ["docker", "run", "--rm", "--network", "host" if reseau else "none",
               "-e", "OPENAI_API_KEY", "-e", "HF_TOKEN", "-v", f"{self.bench}:/bench"]
        if not getattr(self.a, "internet", False):
            cmd += ["-e", "HF_HUB_OFFLINE=1", "-e", "HF_DATASETS_OFFLINE=1"]
        if modeles:
            cmd += ["-v", f"{self.ws / 'models'}:/models:ro"]
        if not reseau:  # le code produit par le modèle s'exécute ici : ni réseau ni ressources illimitées
            cmd += ["--memory", "4g", "--pids-limit", "512"]
        env = os.environ | {"OPENAI_API_KEY": self.cle}
        return lancer_cmd([*cmd, IMAGE, *args], env=env)[0]

    def options_modele(self) -> list[str]:
        o = ["--url", self.url, "--max-tokens", str(self.a.max_tokens), "--concurrence", str(self.concurrence)]
        return o + (["--sans-raisonnement"] if self.a.sans_raisonnement else [])

    # ------------------------------------------------------------------ installation comparée

    def installation(self, modele_servi: str) -> dict:
        e = self.env
        vllm_env = (PROJET / ".env.vllm").read_text() if (PROJET / ".env.vllm").exists() else ""
        inst = {
            "profil": self.profil, "mode": "duo" if "spark-duo" in e.get("COMPOSE_FILE", "") else "simple",
            "modele_servi": modele_servi, "vllm_image": e.get("VLLM_IMAGE", ""), "contexte": self.contexte,
            "gpu_util": e.get("VLLM_GPU_UTIL", ""), "max_seqs": e.get("VLLM_MAX_SEQS", ""),
            "spec_args": e.get("VLLM_SPEC_ARGS", ""), "extra_args": e.get("VLLM_EXTRA_ARGS", ""),
            "lora": e.get("VLLM_LORA_ARGS", ""), "env_vllm": vllm_env.strip(),
            "raisonnement": not self.a.sans_raisonnement, "max_tokens": self.a.max_tokens,
            "voie": "litellm" if self.a.via_litellm else "vllm",
        }
        try:  # configuration RAG telle que mcp-docs la voit (reranker local ou sur le serveur annexe)
            r = subprocess.run(["docker", "compose", "config", "--format", "json"], cwd=PROJET, capture_output=True, text=True, timeout=60)
            env_mcp = json.loads(r.stdout)["services"]["mcp-docs"].get("environment") or {}
            inst["rag"] = {k: env_mcp.get(k, "") for k in ("RAG_BACKEND", "EMBED_URL", "RERANK_URL", "COLLECTION")}
        except Exception:
            inst["rag"] = {}
        return inst

    def verifier_vllm(self) -> str:
        req = urllib.request.Request(f"{self.url}/models", headers={"Authorization": f"Bearer {self.cle}"} if self.cle else {})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                modeles = json.load(r)["data"]
        except Exception as e:
            erreur(f"le modèle ne répond pas sur {self.url} ({e}). vLLM est-il démarré ?")
        noms = [m["id"] for m in modeles]
        if "qwen-local" not in noms:
            erreur(f"modèle « qwen-local » absent de {self.url} (servis : {noms})")
        return next((m.get("root") or "" for m in modeles if m["id"] == "qwen-local"), "")

    def prometheus(self, requete: str) -> float | None:
        q = urllib.parse.urlencode({"query": requete})
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:9090/api/v1/query?{q}", timeout=10) as r:
                res = json.load(r)["data"]["result"]
            v = float(res[0]["value"][1]) if res else None
            return v if v is not None and v == v else None
        except Exception:
            return None

    # ------------------------------------------------------------------ familles

    def lm_eval(self, dossier: Path, taches: str, limite: int, extra: list[str] | None = None, modeles=False) -> dict:
        args = ["lm-eval", *self.options_modele(), "--taches", taches, "--limite", str(limite),
                "--contexte", str(self.contexte), "--sortie", f"/bench/resultats/{dossier.parent.name}/{dossier.name}", *(extra or [])]
        self.docker_banc(args, modeles=modeles)
        return lire_score(dossier)

    def f_connaissances(self, p, d):
        return self.lm_eval(d / "connaissances", p["taches"], p["limite"])

    def f_consignes(self, p, d):
        return self.lm_eval(d / "consignes", p["taches"], p["limite"])

    def f_code(self, p, d):
        scores = {}
        for jeu, limite in p.items():
            sous = d / f"code-{jeu}"
            chemin = f"/bench/resultats/{d.name}/{sous.name}"
            self.docker_banc(["code-gen", *self.options_modele(), "--jeu", jeu, "--limite", str(limite), "--sortie", chemin])
            if (sous / "generation.json").exists():
                self.docker_banc(["code-note", "--jeu", jeu, "--sortie", chemin], reseau=False)
            scores |= lire_score(sous)
        return scores

    def f_contexte(self, p, d):
        dossier_modele = f"/models/{self.env.get('VLLM_MODEL', '')}"
        longueurs = [n for n in p["longueurs"] if n + self.a.max_tokens <= self.contexte] or [p["longueurs"][0]]
        scores = self.lm_eval(d / "ruler", p["ruler"], p["limite"], modeles=True,
                              extra=["--tokenizer", dossier_modele, "--longueurs", ",".join(map(str, longueurs))])
        if p.get("longbench"):
            lb = p["longbench"]
            self.docker_banc(["longbench", *self.options_modele(), "--longueurs", lb["longueurs"], "--limite", str(lb["limite"]),
                              "--contexte", str(self.contexte), "--tokenizer", dossier_modele,
                              "--sortie", f"/bench/resultats/{d.name}/longbench"], modeles=True)
            scores |= lire_score(d / "longbench")
        return scores

    def f_rag(self, p, d):
        scores = {}
        for jeu, limite in p.items():
            if not (self.bench / "donnees" / "beir" / jeu / "corpus.jsonl").exists():
                print(f"  {jeu} absent : ./scripts/bench-public.py preparer")
                continue
            cmd = ["docker", "compose", "run", "--rm", "--no-deps", "-T", "-v", f"{self.bench}:/bench",
                   "-v", f"{PROJET / 'services' / 'bench-public'}:/bench-code:ro", "--entrypoint", "python", "mcp-docs",
                   "/bench-code/rag_beir.py", "--jeu", jeu, "--limite", str(limite), "--sortie", f"/bench/resultats/{d.name}/rag-{jeu}"]
            lancer_cmd(cmd, cwd=PROJET)
            scores |= lire_score(d / f"rag-{jeu}")
        return scores

    def f_agent(self, p, d):
        if subprocess.run(["docker", "image", "inspect", IMAGE_AIDER], capture_output=True).returncode != 0:
            print(f"  image {IMAGE_AIDER} absente : ./scripts/bench-public.py preparer (sans --sans-aider)")
            return {}
        sorties = self.bench / "aider"
        nom = d.name
        cmd = ["docker", "run", "--rm", "--network", "host", "--memory", "12g", "-e", "OPENAI_API_KEY",
               "-e", f"OPENAI_API_BASE={self.url}", "-e", "AIDER_DOCKER=1", "-e", "AIDER_BENCHMARK_DIR=/benchmarks",
               "-v", f"{sorties}:/benchmarks", "-w", "/aider", IMAGE_AIDER,
               "./benchmark/benchmark.py", nom, "--model", "openai/qwen-local", "--edit-format", "whole",
               "--threads", str(self.concurrence), "--exercises-dir", "polyglot-benchmark", "--new"]
        if p.get("langages"):
            cmd += ["--languages", p["langages"]]
        _, texte = lancer_cmd(cmd, env=os.environ | {"OPENAI_API_KEY": self.cle or "aucune"}, capture=True)
        lu = dict(re.findall(r"^\s*(pass_rate_\d|percent_cases_well_formed|seconds_per_case|test_cases):\s*([\d.]+)", texte, re.M))
        prefixe = f"aider_{p.get('langages') or 'tous'}"
        scores = {f"{prefixe}:{k}": float(v) for k, v in lu.items()}
        (d / "agent").mkdir(parents=True, exist_ok=True)
        (d / "agent" / "score.json").write_text(json.dumps({"scores": scores, "details": {"sortie": texte[-4000:]}}, indent=2) + "\n")
        return scores

    def f_charge(self, p, d):
        image, dossier_modele = self.env.get("VLLM_IMAGE", ""), f"/models/{self.env.get('VLLM_MODEL', '')}"
        sortie = d / "charge"
        sortie.mkdir(parents=True, exist_ok=True)
        base = self.url.rsplit("/v1", 1)[0]
        scores = {}
        for nom, concurrences in p.items():
            s = SCENARIOS[nom]
            if s["entree"] + s["sortie"] > self.contexte:
                continue
            tenus = 0
            for c in concurrences:
                fichier = f"{nom}-c{c}.json"
                t0 = time.time()
                # Complétions brutes (pas de modèle de chat ni de raisonnement) : longueurs exactes, comparables entre modèles
                cmd = ["docker", "run", "--rm", "--network", "host", "-e", "OPENAI_API_KEY",
                       "-v", f"{self.ws / 'models'}:/models:ro", "-v", f"{sortie}:/out", "--entrypoint", "vllm", image,
                       "bench", "serve", "--backend", "openai", "--base-url", base, "--endpoint", "/v1/completions",
                       "--model", dossier_modele, "--served-model-name", "qwen-local", "--trust-remote-code",
                       "--dataset-name", "random", "--random-input-len", str(s["entree"]), "--random-output-len", str(s["sortie"]),
                       "--num-prompts", str(max(10, 4 * c)), "--max-concurrency", str(c), "--ignore-eos", "--seed", "1",
                       "--percentile-metrics", "ttft,tpot,itl,e2el", "--metric-percentiles", "50,95",
                       "--goodput", f"ttft:{s['ttft_ms']}", f"tpot:{TPOT_MAX_MS}",
                       "--save-result", "--result-dir", "/out", "--result-filename", fichier]
                lancer_cmd(cmd, env=os.environ | {"OPENAI_API_KEY": self.cle})
                f = sortie / fichier
                if not f.exists():
                    continue
                r = json.loads(f.read_text())
                duree = max(60, int(time.time() - t0) + 30)
                k = f"charge_{nom}_c{c}"
                val = {
                    "ttft_p50_ms": r.get("p50_ttft_ms", r.get("median_ttft_ms")), "ttft_p95_ms": r.get("p95_ttft_ms"),
                    "tpot_p50_ms": r.get("p50_tpot_ms", r.get("median_tpot_ms")), "itl_p95_ms": r.get("p95_itl_ms"),
                    "debit_sortie_tok_s": r.get("output_throughput"), "goodput_req_s": r.get("request_goodput"),
                    "echecs": (r.get("num_prompts") or 0) - (r.get("completed") or 0) if r.get("num_prompts") else None,
                    "kv_max_pct": self.prometheus(f'100*max(max_over_time(vllm:kv_cache_usage_perc{{profil="{self.profil}"}}[{duree}s]))'),
                    "preemptions": self.prometheus(f'sum(increase(vllm:num_preemptions_total{{profil="{self.profil}"}}[{duree}s]))'),
                }
                scores |= {f"{k}:{m}": round(v, 1) for m, v in val.items() if isinstance(v, (int, float))}
                if (val["ttft_p95_ms"] or 1e12) <= s["ttft_ms"] and (val["tpot_p50_ms"] or 1e12) <= TPOT_MAX_MS:
                    tenus = max(tenus, c)
            scores[f"charge_{nom}:concurrence_tenue"] = tenus
        (sortie / "score.json").write_text(json.dumps({"scores": scores, "details": {"scenarios": SCENARIOS, "tpot_max_ms": TPOT_MAX_MS}}, indent=2) + "\n")
        return scores


def lire_score(dossier: Path) -> dict:
    f = dossier / "score.json"
    if not f.exists():
        print(f"  ⚠ pas de score dans {dossier}")
        return {}
    return json.loads(f.read_text()).get("scores") or {}


# ----------------------------------------------------------------------------- commandes

def cmd_preparer(a) -> None:
    b = Banc(a)
    b.bench.mkdir(parents=True, exist_ok=True)
    print("1. Image des benchmarks")
    if lancer_cmd(["docker", "build", "-t", IMAGE, str(PROJET / "services" / "bench-public")])[0]:
        erreur("construction de l'image échouée")
    print("2. Jeux lm-eval, EvalPlus, MultiPL-E, LongBench v2")
    taches = sorted({t for p in PRESETS.values() for f in ("connaissances", "consignes") for t in p[f]["taches"].split(",")})
    a.internet = True
    code = b.docker_banc(["preparer", "--taches-lm-eval", ",".join(taches)])
    print("3. Jeux BEIR (recherche RAG)")
    for jeu in sorted({j for p in PRESETS.values() for j in p["rag"]}):
        cible = b.bench / "donnees" / "beir"
        if (cible / jeu / "corpus.jsonl").exists():
            continue
        print(f"  {jeu}…", flush=True)
        try:
            with urllib.request.urlopen(BEIR.format(jeu), timeout=600) as r:
                zipfile.ZipFile(io.BytesIO(r.read())).extractall(cible)
        except Exception as e:
            code = 1
            print(f"  ✗ {jeu} : {e}")
    if not a.sans_aider:
        print("4. Aider polyglot (image lourde : Python, C++, Go, Rust, Java, Node ; 20 à 40 min la première fois)")
        src = b.bench / "aider-src"
        if not (src / ".git").exists():
            lancer_cmd(["git", "clone", "-q", AIDER_DEPOT, str(src)])
        lancer_cmd(["git", "-C", str(src), "fetch", "-q", "origin", AIDER_COMMIT])
        lancer_cmd(["git", "-C", str(src), "checkout", "-q", AIDER_COMMIT])
        poly = b.bench / "aider" / "polyglot-benchmark"
        if not (poly / ".git").exists():
            poly.parent.mkdir(parents=True, exist_ok=True)
            lancer_cmd(["git", "clone", "-q", POLYGLOT_DEPOT, str(poly)])
        if lancer_cmd(["docker", "build", "-f", "benchmark/Dockerfile", "-t", "aider-benchmark", "."], cwd=src)[0] == 0:
            derive = f"FROM aider-benchmark\nCOPY . /aider\nWORKDIR /aider\nRUN pip install --no-cache-dir -e .[dev]\n"
            p = subprocess.run(["docker", "build", "-t", IMAGE_AIDER, "-f", "-", "."], cwd=src, input=derive, text=True)
            code |= p.returncode
        else:
            code = 1
    print("\n" + ("✓ Prêt. Lancer :  ./scripts/bench-public.py lancer" if code == 0 else
                  "⚠ Terminé avec des échecs (voir plus haut) : les familles concernées seront sautées."))


def cmd_lancer(a) -> None:
    b = Banc(a)
    preset = PRESETS[a.preset]
    inconnues = set(f"{a.seulement or ''},{a.sans or ''}".split(",")) - set(FAMILLES) - {""}
    if inconnues:
        erreur(f"familles inconnues : {', '.join(inconnues)} (connues : {', '.join(FAMILLES)})")
    familles = [f for f in FAMILLES if f in (a.seulement.split(",") if a.seulement else FAMILLES)]
    familles = [f for f in familles if f not in (a.sans.split(",") if a.sans else [])]
    if subprocess.run(["docker", "image", "inspect", IMAGE], capture_output=True).returncode != 0:
        erreur(f"image {IMAGE} absente : ./scripts/bench-public.py preparer")
    servi = b.verifier_vllm()
    inst = b.installation(servi)
    config = hashlib.sha256(json.dumps(inst, sort_keys=True).encode()).hexdigest()[:8]
    date = time.strftime("%Y-%m-%d_%H%M")
    dossier = b.bench / "resultats" / f"{date}_{b.profil}"
    dossier.mkdir(parents=True, exist_ok=True)
    try:
        commit = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=PROJET, capture_output=True, text=True).stdout.strip()
    except OSError:
        commit = ""
    resume = {"id_execution": dossier.name, "date": date, "preset": a.preset, "profil": b.profil, "config": config,
              "jeux": hashlib.sha256(json.dumps(preset, sort_keys=True).encode()).hexdigest()[:8],
              "commit": commit, "hote": socket.gethostname(), "note": a.note or "", "installation": inst,
              "familles": {}, "scores": {}}
    print(f"Profil {b.profil} ({inst['mode']}), installation {config}, préréglage {a.preset} : {', '.join(familles)}")
    print(f"Résultats : {dossier}\n")

    def sauver():
        (dossier.parent / f"{dossier.name}.resume.json").write_text(json.dumps(resume, ensure_ascii=False, indent=2) + "\n")

    for f in familles:
        print(f"━━ {f}", flush=True)
        t0 = time.time()
        try:
            scores = getattr(b, f"f_{f}")(preset[f], dossier)
            statut = "ok" if scores else "vide"
        except KeyboardInterrupt:
            resume["familles"][f] = {"statut": "interrompu", "duree_s": round(time.time() - t0)}
            sauver()
            raise
        except Exception as e:
            scores, statut = {}, f"erreur : {str(e)[:200]}"
        resume["familles"][f] = {"statut": statut, "duree_s": round(time.time() - t0)}
        resume["scores"] |= scores
        sauver()  # après chaque famille : une exécution interrompue garde ce qui est fait
        print(f"  → {statut}, {len(scores)} mesures, {round(time.time() - t0)} s\n", flush=True)

    print(json.dumps(resume["scores"], ensure_ascii=False, indent=2))
    print(f"\nRésumé : {dossier.parent / (dossier.name + '.resume.json')}\nComparer :  ./scripts/rapport-modeles.py")


def cmd_lister(a) -> None:
    b = Banc(a)
    fichiers = sorted((b.bench / "resultats").glob("*.resume.json"))
    if not fichiers:
        return print("Aucune exécution. ./scripts/bench-public.py lancer")
    for f in fichiers:
        r = json.loads(f.read_text())
        etats = " ".join(f"{k}:{v['statut'][:6]}" for k, v in r.get("familles", {}).items())
        print(f"{r['date']}  {r['preset']:8} {r['profil']:40} {r['installation'].get('mode', ''):6} config {r['config']}  {etats}")


def main() -> None:
    ap = argparse.ArgumentParser(description="Benchmarks publics : comparer les profils vLLM", formatter_class=argparse.RawDescriptionHelpFormatter,
                                 epilog=__doc__.split("\n\n", 1)[1])
    ap.add_argument("--env", default=".env")
    sp = ap.add_subparsers(dest="cmd", required=True)
    p = sp.add_parser("preparer", help="images et jeux de données (Internet)")
    p.add_argument("--sans-aider", action="store_true", help="ne pas construire l'image Aider (lourde)")
    p.set_defaults(f=cmd_preparer, via_litellm=False, sans_raisonnement=False, max_tokens=0)
    p = sp.add_parser("lancer", help="banc sur le profil actif")
    p.add_argument("--preset", choices=list(PRESETS), default="rapide")
    p.add_argument("--seulement", help=f"familles, parmi {','.join(FAMILLES)}")
    p.add_argument("--sans", help="familles à sauter")
    p.add_argument("--sans-raisonnement", action="store_true", help="chat_template_kwargs enable_thinking=false")
    p.add_argument("--max-tokens", type=int, default=16384, help="budget de sortie par réponse, raisonnement compris")
    p.add_argument("--concurrence", type=int, help="requêtes en parallèle (défaut : VLLM_MAX_SEQS)")
    p.add_argument("--via-litellm", action="store_true", help="passer par LiteLLM (clé maître) au lieu de vLLM direct")
    p.add_argument("--note", help="texte libre gardé dans le résumé")
    p.set_defaults(f=cmd_lancer)
    p = sp.add_parser("lister", help="exécutions passées")
    p.set_defaults(f=cmd_lister, via_litellm=False, sans_raisonnement=False, max_tokens=0)
    a = ap.parse_args()
    if not shutil.which("docker"):
        erreur("docker introuvable")
    a.f(a)


if __name__ == "__main__":
    main()
