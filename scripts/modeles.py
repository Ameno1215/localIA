#!/usr/bin/env python3
"""Gère les modèles du DGX Spark : chercher, ajouter, télécharger, activer.

  ./scripts/modeles.py chercher [motif]          modèles dont la recette vLLM est vérifiée pour le GB10
  ./scripts/modeles.py nouveautes [auteur]       derniers modèles publiés sur Hugging Face (sans attendre leur recette)
  ./scripts/modeles.py ajouter <id_hf> [options] crée le profil config/modeles-spark/<nom>.env d'après la recette
  ./scripts/modeles.py lister                    profils connus, téléchargés ou non, actif
  ./scripts/modeles.py telecharger <nom>         télécharge les poids dans ./models
  ./scripts/modeles.py utiliser <nom>            télécharge si besoin, écrit .env, redémarre vLLM

Un profil = les réglages vLLM d'un modèle (image, parsers, options). Les recettes viennent de
recipes.vllm.ai (internet requis pour chercher/ajouter ; le reste marche hors ligne).
Bibliothèque standard uniquement.
"""

import argparse
import concurrent.futures as cf
import json
import math
import os
import re
import shlex
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

PROJET = Path(__file__).resolve().parent.parent
PROFILS = PROJET / "config" / "modeles-spark"
CACHE = PROJET / "data" / "cache-recettes-gb10.json"
RECETTES = "https://recipes.vllm.ai"
IMAGE_DEFAUT = "vllm/vllm-openai:v0.30.0"
MEMOIRE_GO = 128           # mémoire unifiée du Spark
PART_EMBEDDINGS = 0.08     # bge-m3 + reranker, servis par vLLM sur le même GPU (2 x 0.04)
CLES_ENV = ["VLLM_MODEL", "VLLM_IMAGE", "VLLM_MAX_MODEL_LEN", "VLLM_GPU_UTIL", "VLLM_MAX_SEQS",
            "VLLM_TOOL_PARSER", "VLLM_EXTRA_ARGS", "VLLM_SPEC_ARGS"]
# Options gérées par des variables dédiées, ou sans objet sur un seul GPU
GEREES = {"--max-model-len": "VLLM_MAX_MODEL_LEN", "--gpu-memory-utilization": "VLLM_GPU_UTIL", "--max-num-seqs": "VLLM_MAX_SEQS"}
IGNOREES = {"--tensor-parallel-size", "--mm-encoder-tp-mode", "--enable-auto-tool-choice", "--tool-call-parser",
            "--speculative-config", "--enable-expert-parallel"}


def erreur(msg: str) -> None:
    sys.exit(f"Erreur : {msg}")


def lire_json(chemin: str):
    req = urllib.request.Request(RECETTES + chemin, headers={"User-Agent": "ia-locale/modeles.py"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


# ----------------------------------------------------------------------------- recettes

def variante(recette: dict, hf_id: str) -> dict:
    """La variante de la recette qui correspond à ce checkpoint."""
    variantes = recette.get("variants") or {}
    for v in variantes.values():
        if v.get("model_id") == hf_id:
            return v
    return variantes.get("default") or next(iter(variantes.values()), {})


def statut_gb10(recette: dict) -> str | None:
    return ((recette.get("meta") or {}).get("hardware") or {}).get("dgx_spark_gb10")


def resume(hf_id: str, recette: dict) -> dict:
    v, m = variante(recette, hf_id), recette.get("model") or {}
    return {"id": hf_id, "statut": statut_gb10(recette), "archi": m.get("architecture"), "params": m.get("parameter_count"),
            "vram_min": v.get("vram_minimum_gb"), "precision": v.get("precision")}


def catalogue(rafraichir: bool = False) -> list[dict]:
    """Modèles avec recette GB10, en cache 24 h (491 recettes à lire sinon)."""
    if not rafraichir and CACHE.exists() and time.time() - CACHE.stat().st_mtime < 86400:
        return json.loads(CACHE.read_text())
    print("Lecture du catalogue de recettes (une trentaine de secondes)…", file=sys.stderr)
    index = lire_json("/models.json")

    def lire(m):
        try:
            return m["hf_id"], lire_json(m["json"])
        except Exception:
            return m["hf_id"], None

    with cf.ThreadPoolExecutor(8) as ex:
        lus = list(ex.map(lire, index))
    res = sorted((resume(h, r) for h, r in lus if r and statut_gb10(r)), key=lambda x: x["id"])
    CACHE.parent.mkdir(exist_ok=True)
    CACHE.write_text(json.dumps(res, ensure_ascii=False))
    return res


def cmd_chercher(a) -> None:
    motif = (a.motif or "").lower()
    lignes = [r for r in catalogue(a.rafraichir) if motif in r["id"].lower()]
    if not lignes:
        erreur("aucun modèle ne correspond")
    print(f"{'Modèle':58} {'Statut':9} {'Taille':>6} {'Mémoire min':>11}  Type")
    for r in lignes:
        vram = f"{r['vram_min']} Go" if r["vram_min"] else "?"
        print(f"{r['id']:58} {r['statut']:9} {r['params'] or '?':>6} {vram:>11}  {r['archi'] or ''} {r['precision'] or ''}")
    print(f"\n{len(lignes)} modèle(s). Pour en ajouter un :  ./scripts/modeles.py ajouter <modèle>   (modèles très récents :  nouveautes)")
    print("Le Spark a 128 Go de mémoire partagée avec le système et les autres services : jusqu'à ~85 Go de « Mémoire min » reste réaliste,")
    print("au-delà il y a trop peu de marge. Plus gros n'est pas plus rapide : la vitesse dépend surtout des paramètres actifs (MoE) et de la précision.")


def cmd_nouveautes(a) -> None:
    """Derniers modèles d'un auteur sur Hugging Face. Les récents n'ont souvent pas encore de recette vLLM."""
    url = f"https://huggingface.co/api/models?author={a.auteur}&sort=createdAt&direction=-1&limit=200"
    if a.motif:
        url += f"&search={a.motif}"
    req = urllib.request.Request(url, headers={"User-Agent": "ia-locale/modeles.py"})
    with urllib.request.urlopen(req, timeout=30) as r:
        modeles = json.load(r)
    avec_recette = {r["id"] for r in catalogue()} if CACHE.exists() else set()
    gen = {"text-generation", "image-text-to-text", "any-to-any"}
    lignes = [m for m in modeles if m.get("pipeline_tag") in gen and not m["id"].endswith(("GGUF", "Base-BF16"))][: a.nombre]
    print(f"{'Date':10}  {'Modèle':64} Recette GB10")
    for m in lignes:
        print(f"{m.get('createdAt', '')[:10]:10}  {m['id']:64} {'oui' if m['id'] in avec_recette else 'non (réglages à la main)'}")
    print("\nAvec recette : ajouter <modèle>. Sans recette : lis la fiche Hugging Face et crée le profil à la main (exemple : bolt-74b-a9b-24gb.env).")


# ----------------------------------------------------------------------------- profils

def separer(tokens: list[str]) -> dict[str, str | None]:
    """['--a', '1', '--b'] -> {'--a': '1', '--b': None}  (la dernière occurrence gagne)."""
    res, i = {}, 0
    while i < len(tokens):
        t = tokens[i]
        if i + 1 < len(tokens) and not tokens[i + 1].startswith("--"):
            res[t] = tokens[i + 1]
            i += 2
        else:
            res[t] = None
            i += 1
    return res


def citer(v: str) -> str:
    return shlex.quote(v) if re.search(r"[\s{}\"']", v) else v


def en_texte(flags: dict[str, str | None]) -> str:
    return " ".join(k if v is None else f"{k} {citer(v)}" for k, v in flags.items())


def version(image: str) -> tuple[int, ...] | None:
    m = re.fullmatch(r"vllm/vllm-openai:v(\d+)\.(\d+)\.(\d+)", image)
    return tuple(int(x) for x in m.groups()) if m else None


def choisir_image(recette: dict, v: dict, ho: dict) -> tuple[str, list[str]]:
    image = ho.get("docker_image") or v.get("docker_image") or (recette.get("recommended_command") or {}).get("docker_image") or IMAGE_DEFAUT
    notes = []
    vers = version(image)
    if image.endswith(":latest") or image.endswith(":nightly") or (vers and vers <= version(IMAGE_DEFAUT)):
        if image != IMAGE_DEFAUT:
            notes.append(f"La recette indique {image}, remplacée par l'image du projet {IMAGE_DEFAUT} (plus récente, épinglée).")
        return IMAGE_DEFAUT, notes
    if not image.startswith("vllm/vllm-openai:"):
        notes.append(f"Image NON officielle : {image}. À vérifier (architecture arm64, origine) avant usage.")
    else:
        notes.append(f"Image spécifique à ce modèle : {image}.")
    return image, notes


def choisir_spec(recette: dict) -> tuple[str, list[str]]:
    """Décodage spéculatif intégré au modèle (MTP). Ignoré s'il faut télécharger un modèle brouillon en plus."""
    sd = (recette.get("features") or {}).get("spec_decoding") or {}
    modes = sd.get("modes") or {}
    for nom in [sd.get("default_mode"), "mtp"]:
        mode = modes.get(nom)
        if not mode:
            continue
        args = ((mode.get("hardware_overrides") or {}).get("dgx_spark_gb10") or {}).get("args") or mode.get("args") or []
        cfg = next((a for a in args if a.startswith("{")), None)
        if cfg and "model" not in json.loads(cfg):
            note = [] if nom == sd.get("default_mode") else [f"Mode spéculatif par défaut ({sd.get('default_mode')}) ignoré : il demande un modèle brouillon à télécharger. MTP utilisé."]
            return "--speculative-config " + shlex.quote(cfg), note
    if sd.get("default_mode"):
        return "", [f"Décodage spéculatif ({sd.get('default_mode')}) non activé : il demande un modèle brouillon séparé."]
    return "", []


def generer(hf_id: str, nom: str, recette: dict, opts) -> str:
    statut = statut_gb10(recette)
    if not statut:
        erreur(f"{hf_id} n'a pas de recette pour le DGX Spark (GB10). Lance « chercher » pour voir ceux qui en ont.")
    v = variante(recette, hf_id)
    ho = (v.get("hardware_overrides") or {}).get("dgx_spark_gb10") or {}
    feats = recette.get("features") or {}
    notes = []

    flags = separer((v.get("extra_args") or []) + (ho.get("extra_args") or []))
    for f in ("reasoning", "tool_calling"):
        flags.update(separer((feats.get(f) or {}).get("args") or []))
    if "--trust-remote-code" in ((recette.get("recommended_command") or {}).get("command") or ""):
        flags["--trust-remote-code"] = None

    parser = flags.get("--tool-call-parser") or "hermes"
    valeurs = {}
    for flag, var in GEREES.items():
        if flag in flags:
            valeurs[var] = flags[flag]
    extra = {k: val for k, val in flags.items() if k not in GEREES and k not in IGNOREES}

    image, n = choisir_image(recette, v, ho)
    notes += n
    spec, n = choisir_spec(recette)
    notes += n
    if any("examples/" in str(val) for val in extra.values()):
        notes.append("Une option référence un fichier du dépôt vLLM (examples/…) : vérifier qu'il existe dans l'image, sinon le copier dans ./models.")

    ctx = opts.contexte or valeurs.get("VLLM_MAX_MODEL_LEN") or "131072"
    util = float(opts.gpu_util or valeurs.get("VLLM_GPU_UTIL") or 0.5)
    seqs = opts.sequences or valeurs.get("VLLM_MAX_SEQS") or "8"
    vram = v.get("vram_minimum_gb")
    if vram:
        besoin = math.ceil(vram * 1.3 / MEMOIRE_GO * 20) / 20
        if besoin > util and not opts.gpu_util:
            notes.append(f"Mémoire minimale {vram} Go : VLLM_GPU_UTIL relevé de {util} à {besoin}.")
            util = besoin
        if 60 <= vram <= 100:
            notes.append(f"Gros modèle ({vram} Go) : peu de marge pour le système et les autres services. Garder VLLM_MAX_SEQS bas (4 ou moins).")
        if vram > 100:
            notes.append(f"Mémoire minimale {vram} Go : ne tient PAS sur un seul Spark (recette pour 2 Spark ou plus).")
    if util + PART_EMBEDDINGS > 0.9:
        notes.append(f"VLLM_GPU_UTIL {util} + embeddings/reranker {PART_EMBEDDINGS} dépasse 0.9 de la mémoire : risque de saturer le système.")
    if not ho:
        notes.append("Pas de réglages GB10 dans la recette : valeurs par défaut du projet, À VÉRIFIER au premier démarrage.")

    env = {k: str(val) for k, val in (ho.get("extra_env") or {}).items() if "$" not in str(val)}
    desc = recette.get("meta", {}).get("description", hf_id)
    desc = desc if len(desc) <= 110 else desc[:110].rsplit(" ", 1)[0] + " …"
    l = [f"# {desc}",
         f"# Source : {RECETTES}/{hf_id}?hardware=dgx_spark_gb10  (statut GB10 : {statut}, généré le {time.strftime('%d/%m/%Y')})"]
    l += [f"# ⚠ {n}" for n in notes]
    l += ["", f"MODELE_HF={hf_id}", f"MODELE_DOSSIER={opts.dossier or hf_id.split('/')[-1]}", "",
          f"VLLM_MODEL={opts.dossier or hf_id.split('/')[-1]}", f"VLLM_IMAGE={image}", f"VLLM_MAX_MODEL_LEN={ctx}",
          f"VLLM_GPU_UTIL={util}", f"VLLM_MAX_SEQS={seqs}", f"VLLM_TOOL_PARSER={parser}",
          f"VLLM_EXTRA_ARGS={en_texte(extra)}", f"VLLM_SPEC_ARGS={spec}"]
    l += [f"ENV_{k}={val}" for k, val in env.items()]
    return "\n".join(l) + "\n"


def cmd_ajouter(a) -> None:
    nom = a.nom or re.sub(r"^nvidia-", "", re.sub(r"[^a-z0-9.]+", "-", a.id_hf.split("/")[-1].lower()).strip("-"))
    fichier = PROFILS / f"{nom}.env"
    if fichier.exists() and not a.ecraser:
        erreur(f"{fichier.relative_to(PROJET)} existe déjà (--ecraser pour le remplacer)")
    try:
        recette = lire_json(f"/{a.id_hf}.json")
    except Exception as e:
        erreur(f"recette introuvable pour {a.id_hf} ({e}). Vérifie l'identifiant avec « chercher ».")
    texte = generer(a.id_hf, nom, recette, a)
    PROFILS.mkdir(parents=True, exist_ok=True)
    fichier.write_text(texte)
    print(texte)
    print(f"Profil écrit : {fichier.relative_to(PROJET)}\nL'activer :  ./scripts/modeles.py utiliser {nom}")


def lire_profil(nom: str) -> dict[str, str]:
    f = PROFILS / f"{nom}.env"
    if not f.exists():
        erreur(f"profil « {nom} » introuvable. Profils connus : {', '.join(p.stem for p in sorted(PROFILS.glob('*.env'))) or 'aucun'}")
    res = {}
    for ligne in f.read_text().splitlines():
        if ligne.strip() and not ligne.startswith("#") and "=" in ligne:
            k, _, val = ligne.partition("=")
            res[k.strip()] = val
    return res


def lire_env(chemin: Path) -> dict[str, str]:
    res = {}
    if chemin.exists():
        for ligne in chemin.read_text().splitlines():
            if ligne.strip() and not ligne.startswith("#") and "=" in ligne:
                k, _, val = ligne.partition("=")
                res[k.strip()] = val
    return res


def telecharge(dossier: str) -> bool:
    return (PROJET / "models" / dossier / "config.json").exists()


def extras(p: dict) -> list[str]:
    """Modèles annexes du profil (ex. brouillon de décodage spéculatif)."""
    return p.get("MODELE_EXTRA_HF", "").split()


def tout_telecharge(p: dict) -> bool:
    return telecharge(p["MODELE_DOSSIER"]) and all(telecharge(e.split("/")[-1]) for e in extras(p))


def cmd_lister(a) -> None:
    actif = lire_env(PROJET / a.env).get("VLLM_MODEL")
    profils = sorted(PROFILS.glob("*.env"))
    if not profils:
        erreur("aucun profil. Crée-en avec « ajouter »")
    larg = max(len(f.stem) for f in profils) + 2
    print(f"{'Profil':{larg}} {'Poids':14} {'Modèle'}")
    for f in profils:
        p = lire_profil(f.stem)
        poids = "téléchargés" if tout_telecharge(p) else "à télécharger"
        print(f"{f.stem:{larg}} {poids:14} {p.get('MODELE_HF', '?')}{'   ◀ actif' if p['VLLM_MODEL'] == actif else ''}")


def hf(args: list[str]) -> None:
    local = PROJET / ".venv" / "bin" / "hf"
    if local.exists():
        cmd = [str(local), *args]
    else:
        cmd = ["docker", "run", "--rm", "--user", f"{os.getuid()}:{os.getgid()}", "-e", "HOME=/tmp", "-e", "HF_TOKEN",
               "-v", f"{PROJET}/models:/work/models", "-w", "/work", "python:3.12-slim", "sh", "-c",
               'pip install -q --user huggingface_hub && /tmp/.local/bin/hf "$@"', "hf", *args]
    if subprocess.run(cmd, cwd=PROJET).returncode != 0:
        erreur("le téléchargement a échoué (modèle protégé ? exporte HF_TOKEN=… puis relance)")


def cmd_telecharger(a) -> None:
    p = lire_profil(a.nom)
    if tout_telecharge(p) and not a.force:
        return print(f"{p['MODELE_HF']} est déjà dans models/{p['MODELE_DOSSIER']}")
    (PROJET / "models").mkdir(exist_ok=True)
    for hf_id, dossier in [(p["MODELE_HF"], p["MODELE_DOSSIER"])] + [(e, e.split("/")[-1]) for e in extras(p)]:
        if a.force or not telecharge(dossier):
            hf(["download", hf_id, "--local-dir", f"models/{dossier}"])
        print(f"OK : models/{dossier}")


def ecrire_env(chemin: Path, valeurs: dict[str, str]) -> None:
    lignes = chemin.read_text().splitlines() if chemin.exists() else []
    restantes = dict(valeurs)
    for i, ligne in enumerate(lignes):
        k = ligne.partition("=")[0].strip()
        if k in restantes and not ligne.startswith("#"):
            lignes[i] = f"{k}={restantes.pop(k)}"
    lignes += [f"{k}={val}" for k, val in restantes.items()]
    chemin.write_text("\n".join(lignes) + "\n")


def finaliser_image(image: str, commande: str) -> None:
    """Étape qui exige le GPU (impossible pendant « docker build ») : lancée dans un conteneur, puis enregistrée dans l'image."""
    entree, cmd = subprocess.run(["docker", "image", "inspect", image, "--format", "{{json .Config.Entrypoint}}|{{json .Config.Cmd}}"],
                                 capture_output=True, text=True, check=True).stdout.strip().split("|", 1)
    cmd = "[]" if cmd == "null" else cmd
    nom = "ia-locale-finalisation"
    subprocess.run(["docker", "rm", "-f", nom], capture_output=True)
    print(f"Finalisation de l'image (GPU requis) : {commande}")
    if subprocess.run(["docker", "run", "--gpus", "all", "--name", nom, "--entrypoint", "sh", image, "-c", commande]).returncode != 0:
        subprocess.run(["docker", "rm", "-f", nom], capture_output=True)
        erreur("l'étape de finalisation a échoué (internet et GPU requis)")
    subprocess.run(["docker", "commit", "--change", f"ENTRYPOINT {entree}", "--change", f"CMD {cmd}", nom, image],
                   capture_output=True, check=True)
    subprocess.run(["docker", "rm", "-f", nom], capture_output=True)


def cmd_utiliser(a) -> None:
    p = lire_profil(a.nom)
    env = PROJET / a.env
    if not env.exists():
        erreur(f"{a.env} introuvable : copie d'abord .env.spark.example en .env")
    if not tout_telecharge(p) and not a.sans_telecharger:
        cmd_telecharger(argparse.Namespace(nom=a.nom, force=False))
    courant = lire_env(env)
    if "docker-compose.spark.yml" not in courant.get("COMPOSE_FILE", "") and not a.force:
        erreur(f"{a.env} n'est pas un .env de Spark (COMPOSE_FILE absent) : ce script règle le Spark, pas le PC de test. --force pour passer outre.")
    if courant.get("VLLM_LORA_ARGS"):
        print("⚠ VLLM_LORA_ARGS est rempli : une LoRA entraînée sur un autre modèle ne fonctionne pas avec celui-ci. Vide-le ou réentraîne.")
    ecrire_env(env, {k: p.get(k, "") for k in CLES_ENV})
    # Réglages Docker propres au modèle (mémoire partagée, limites, sécurité) : fichier généré, retiré au changement de modèle
    extra_compose = PROJET / ".compose.vllm.yml"
    fichiers = [f for f in courant.get("COMPOSE_FILE", "").split(":") if f and f != extra_compose.name]
    if p.get("COMPOSE_EXTRA"):
        extra_compose.write_text(json.dumps({"services": {"vllm": json.loads(p["COMPOSE_EXTRA"])}}, indent=2) + "\n")
        fichiers.append(extra_compose.name)
        for v in json.loads(p["COMPOSE_EXTRA"]).get("volumes", []):
            (PROJET / v.split(":")[0]).mkdir(parents=True, exist_ok=True)
    else:
        extra_compose.unlink(missing_ok=True)
    ecrire_env(env, {"COMPOSE_FILE": ":".join(fichiers)})
    extra_env = {k[4:]: val for k, val in p.items() if k.startswith("ENV_")}
    fichier_env = PROJET / ".env.vllm"
    fichier_env.write_text("".join(f"{k}={val}\n" for k, val in extra_env.items()))
    print(f"{a.env} mis à jour pour « {a.nom} »" + (f" ; .env.vllm : {', '.join(extra_env)}" if extra_env else ""))
    if p.get("IMAGE_CONTEXTE") and not a.sans_telecharger:
        image = p["VLLM_IMAGE"]
        existe = subprocess.run(["docker", "image", "inspect", image], capture_output=True).returncode == 0
        if a.reconstruire or not existe:
            print(f"Construction de l'image {image} (internet requis, plusieurs minutes)…")
            if subprocess.run(["docker", "build", "-t", image, str(PROJET / p["IMAGE_CONTEXTE"])]).returncode != 0:
                erreur("la construction de l'image a échoué (voir GUIDE, section 9)")
            if p.get("IMAGE_APRES_BUILD"):
                finaliser_image(image, p["IMAGE_APRES_BUILD"])
    if a.sans_redemarrer:
        return print("Redémarre vLLM quand tu veux :  docker compose up -d --force-recreate vllm")
    compose = ["docker", "compose", "--env-file", str(env)] if a.env != ".env" else ["docker", "compose"]
    if subprocess.run([*compose, "up", "-d", "--force-recreate", "vllm"], cwd=PROJET).returncode != 0:
        erreur("docker compose a échoué")
    print("Chargement du modèle (plusieurs minutes)…", end="", flush=True)
    fin = time.time() + 1800
    while time.time() < fin:
        etat = subprocess.run([*compose, "ps", "--format", "{{.Health}}|{{.State}}", "vllm"], cwd=PROJET, capture_output=True, text=True).stdout.strip()
        if etat.startswith("healthy"):
            return print("\nvLLM répond. Vérification complète :  ./scripts/02-verifier-spark.sh")
        if "exited" in etat or "dead" in etat:
            break
        print(".", end="", flush=True)
        time.sleep(10)
    erreur("vLLM n'a pas démarré. Voir :  docker compose logs --tail 80 vllm  (et la section 9 du GUIDE)")


def main() -> None:
    ap = argparse.ArgumentParser(description="Modèles du DGX Spark")
    sp = ap.add_subparsers(dest="cmd", required=True)
    c = sp.add_parser("chercher"); c.add_argument("motif", nargs="?"); c.add_argument("--rafraichir", action="store_true"); c.set_defaults(f=cmd_chercher)
    c = sp.add_parser("nouveautes"); c.add_argument("auteur", nargs="?", default="nvidia"); c.add_argument("--motif"); c.add_argument("--nombre", type=int, default=25); c.set_defaults(f=cmd_nouveautes)
    c = sp.add_parser("ajouter"); c.add_argument("id_hf"); c.add_argument("--nom"); c.add_argument("--dossier")
    c.add_argument("--gpu-util"); c.add_argument("--contexte"); c.add_argument("--sequences"); c.add_argument("--ecraser", action="store_true"); c.set_defaults(f=cmd_ajouter)
    c = sp.add_parser("lister"); c.add_argument("--env", default=".env"); c.set_defaults(f=cmd_lister)
    c = sp.add_parser("telecharger"); c.add_argument("nom"); c.add_argument("--force", action="store_true"); c.set_defaults(f=cmd_telecharger)
    c = sp.add_parser("utiliser"); c.add_argument("nom"); c.add_argument("--env", default=".env")
    c.add_argument("--sans-redemarrer", action="store_true"); c.add_argument("--sans-telecharger", action="store_true"); c.add_argument("--force", action="store_true"); c.add_argument("--reconstruire", action="store_true"); c.set_defaults(f=cmd_utiliser)
    a = ap.parse_args()
    a.f(a)


if __name__ == "__main__":
    main()
