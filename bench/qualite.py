"""Phase 7 — qualité sur vos cas embarqués : tickets internes résolus par OpenCode, questions vision.

Tickets : chaque dossier de `taches` (config) contient un `tache.toml` et un dossier `depot/`
avec le code de départ. Pour chaque ticket :
  1. le dépôt est copié dans un dossier temporaire et commité (pour mesurer le diff) ;
  2. l'agent est lancé en mode non interactif (`opencode run --auto "<consigne>"`) avec un délai max ;
  3. les fichiers protégés (tests, Makefile…) sont remis à leur état d'origine : un agent qui
     modifie les tests pour les faire passer est signalé et ne gagne rien ;
  4. la commande `verifier` (ex. make test) décide de la réussite, puis les `controles`
     (cppcheck, clang-tidy, MISRA, absence de malloc…) doivent tous passer.
Les journaux de l'agent sont gardés dans resultats/qualite/.

⚠ L'agent tourne avec --auto (il exécute ses commandes sans demander) : ne lancer que sur
des dépôts de test, jamais sur un dossier de travail.

Vision : --vision lit un JSONL {image, question, attendus: [mots-clés]} et envoie chaque image au
modèle du rôle `role_vision` ; la réponse est juste si elle contient tous les mots-clés.
"""
import base64
import json
import mimetypes
import shutil
import subprocess
import tempfile
import time
import tomllib
from pathlib import Path

from bench.commun import DEPOT, chat, charger_config, cle_maitre, dossier_resultats, role, sauver


def options(p):
    p.add_argument("--taches", help="noms des tickets à jouer, séparés par des virgules")
    p.add_argument("--vision", action="store_true", help="jouer aussi les questions vision")
    p.add_argument("--seulement-vision", action="store_true")
    p.add_argument("--repetitions", type=int, default=1, help="jouer chaque ticket N fois (variance du modèle)")


def _sh(cmd, cwd, timeout=600):
    try:
        p = subprocess.run(cmd, shell=isinstance(cmd, str), cwd=cwd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout + p.stderr)[-20000:]
    except subprocess.TimeoutExpired as e:
        return 124, f"délai dépassé ({timeout}s)\n{(e.stdout or '')[-5000:] if isinstance(e.stdout, str) else ''}"


def jouer_ticket(dossier: Path, q: dict, essai: int) -> dict:
    t = tomllib.loads((dossier / "tache.toml").read_text())
    nom = dossier.name
    tmp = Path(tempfile.mkdtemp(prefix=f"bench-{nom}-"))
    depot = tmp / "depot"
    shutil.copytree(dossier / "depot", depot)
    git = ["git", "-c", "user.name=bench", "-c", "user.email=bench@localhost"]
    for c in (["git", "init", "-q"], ["git", "add", "-A"], git + ["commit", "-qm", "départ"]):
        subprocess.run(c, cwd=depot, check=True, capture_output=True)

    cmd = list(q.get("commande_agent", ["opencode", "run", "--auto"]))
    if t.get("modele") or q.get("modele_agent"):
        cmd += ["--model", t.get("modele") or q["modele_agent"]]
    cmd.append(t["consigne"])
    print(f"\n=== {nom} (essai {essai}) ===\n  {' '.join(cmd[:-1])} \"{t['consigne'][:70]}…\"", flush=True)
    t0 = time.monotonic()
    try:
        code_agent, journal = _sh(cmd, depot, timeout=t.get("delai_s", 900))
    except FileNotFoundError:
        shutil.rmtree(tmp, ignore_errors=True)
        raise SystemExit(f"Commande introuvable : {cmd[0]} (installe OpenCode : ./scripts/installer-opencode.sh)")
    duree = time.monotonic() - t0
    dossier_journaux = dossier_resultats() / "qualite"
    dossier_journaux.mkdir(exist_ok=True)
    (dossier_journaux / f"{nom}-{essai}-{time.strftime('%Y%m%d-%H%M%S')}.log").write_text(journal)

    touches = []
    for f in t.get("fichiers_proteges", []):
        if subprocess.run(["git", "diff", "--quiet", "HEAD", "--", f], cwd=depot).returncode != 0:
            touches.append(f)
            subprocess.run(["git", "checkout", "HEAD", "--", f], cwd=depot, capture_output=True)
    subprocess.run(["git", "add", "-A"], cwd=depot, capture_output=True)
    stat = subprocess.run(["git", "diff", "--cached", "--shortstat"], cwd=depot, capture_output=True, text=True).stdout.strip()

    code_verif, sortie_verif = _sh(t["verifier"], depot)
    controles = []
    for c in t.get("controles", []):
        code, sortie = _sh(c, depot)
        controles.append({"commande": c, "reussi": code == 0, "sortie": sortie[-1000:]})
    reussi = code_verif == 0 and all(c["reussi"] for c in controles)
    print(f"  agent : code {code_agent} en {duree:.0f}s ; diff : {stat or 'aucun'}"
          f"{' ; ⚠ fichiers protégés modifiés : ' + ', '.join(touches) if touches else ''}")
    print(f"  vérification : {'✅' if code_verif == 0 else '❌'}  contrôles : "
          f"{', '.join(('✅' if c['reussi'] else '❌') + ' ' + c['commande'][:40] for c in controles) or '—'}")
    shutil.rmtree(tmp, ignore_errors=True)
    return {"tache": nom, "essai": essai, "reussi": reussi, "duree_s": round(duree, 1), "code_agent": code_agent,
            "diff": stat, "fichiers_proteges_modifies": touches, "verification": code_verif == 0,
            "sortie_verification": sortie_verif[-1500:], "controles": controles}


def vision(cfg, q):
    nom_role = q.get("role_vision")
    if not nom_role:
        print("Pas de rôle vision dans ce profil : questions vision ignorées.")
        return None
    r = role(cfg, nom_role)
    fichier = Path(q.get("questions_vision", "bench/donnees/questions_vision.jsonl"))
    fichier = fichier if fichier.is_absolute() else DEPOT / fichier
    lignes = [json.loads(l) for l in fichier.read_text().splitlines() if l.strip() and not l.startswith("//")]
    if not lignes:
        print(f"Aucune question vision dans {fichier}.")
        return None
    details, bons = [], 0
    for x in lignes:
        img = Path(x["image"])
        img = img if img.is_absolute() else fichier.parent / img
        mime = mimetypes.guess_type(img.name)[0] or "image/png"
        url = f"data:{mime};base64,{base64.b64encode(img.read_bytes()).decode()}"
        m = chat(cfg["passerelle"], cle_maitre(), r["modele"], [{"role": "user", "content": [
            {"type": "text", "text": x["question"]}, {"type": "image_url", "image_url": {"url": url}}]}],
            max_tokens=1024, timeout=600, garder_texte=True)
        ok = m.ok and all(a.lower() in m.texte.lower() for a in x["attendus"])
        bons += ok
        details.append({"image": x["image"], "question": x["question"], "reussi": ok, "reponse": m.texte[-800:],
                        "erreur": m.erreur})
        print(f"  {'✅' if ok else '❌'} {x['question'][:80]}")
    return {"questions": len(lignes), "taux_reussite": round(bons / len(lignes), 3), "details": details}


def lancer(args):
    cfg = charger_config(args.profil)
    q = cfg.get("qualite", {})
    out = {}
    if not args.seulement_vision:
        racine = Path(q.get("taches", "bench/donnees/taches"))
        racine = racine if racine.is_absolute() else DEPOT / racine
        dossiers = sorted(d for d in racine.iterdir() if (d / "tache.toml").is_file())
        if args.taches:
            dossiers = [d for d in dossiers if d.name in args.taches.split(",")]
        tickets = [jouer_ticket(d, q, e + 1) for d in dossiers for e in range(args.repetitions)]
        n = len(tickets) or 1
        out["tickets"] = tickets
        out["taux_reussite"] = round(sum(t["reussi"] for t in tickets) / n, 3)
        print(f"\nTickets résolus : {sum(t['reussi'] for t in tickets)}/{len(tickets)}")
    if args.vision or args.seulement_vision:
        out["vision"] = vision(cfg, q)
    sauver("qualite", cfg, out)
