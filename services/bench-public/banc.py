#!/usr/bin/env python3
"""Benchmarks publics (volet 1 du PLAN-EVALUATION.md), côté conteneur. Lancé par scripts/bench-public.py, pas à la main.

  banc.py preparer   --taches-lm-eval mmlu_pro,ifeval,...  télécharge jeux et données (accès Internet)
  banc.py lm-eval    --taches ... --limite N --sortie DIR   MMLU-Pro, GPQA, IFEval, RULER via lm-evaluation-harness
  banc.py code-gen   --jeu humaneval|mbpp|cpp --sortie DIR  génère les solutions (appels au modèle, en parallèle)
  banc.py code-note  --jeu ... --sortie DIR                 exécute les tests (conteneur SANS réseau)
  banc.py longbench  --longueurs short,medium --sortie DIR  LongBench v2, questions à choix, invite officielle

Chaque commande écrit DIR/score.json : {"scores": {nom: valeur}, "details": {...}}. Les réponses brutes restent à côté.
Racine des données : /bench (WS_DIR/benchmarks sur le Spark), ou BANC_RACINE.
"""

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

RACINE = Path(os.environ.get("BANC_RACINE", "/bench"))
DONNEES = RACINE / "donnees"
BLOC_CODE = re.compile(r"```[ \t]*([\w+#-]*)[ \t]*\n(.*?)```", re.DOTALL)
EVALPLUS_CONSIGNE = ("Please provide a self-contained Python script that solves the following problem "
                     "in a markdown code block:")
CPP_CONSIGNE = ("Complete the following C++ function. Reply with a single ```cpp code block containing the whole "
                "function, including the #include lines shown, and no main() function.")
LONGBENCH_INVITE = """Please read the following text and answer the question below.

<text>
{contexte}
</text>

What is the correct answer to this question: {question}
Choices:
(A) {a}
(B) {b}
(C) {c}
(D) {d}

Format your response as follows: "The correct answer is (insert answer here)"."""


def ecrire(chemin: Path, objet) -> None:
    chemin.parent.mkdir(parents=True, exist_ok=True)
    chemin.write_text(json.dumps(objet, ensure_ascii=False, indent=2, default=str) + "\n")


def jsonl(chemin: Path) -> list[dict]:
    return [json.loads(l) for l in chemin.read_text().splitlines() if l.strip()]


def options_modele(ap: argparse.ArgumentParser) -> None:
    ap.add_argument("--url", default="http://127.0.0.1:8000/v1", help="API OpenAI : vLLM direct par défaut")
    ap.add_argument("--modele", default="qwen-local")
    ap.add_argument("--max-tokens", type=int, default=16384, help="raisonnement compris")
    ap.add_argument("--concurrence", type=int, default=8)
    ap.add_argument("--sans-raisonnement", action="store_true", help="chat_template_kwargs enable_thinking=false")
    ap.add_argument("--limite", type=int, default=0, help="0 = tout le jeu")
    ap.add_argument("--sortie", required=True)


def extra(a) -> dict:
    return {"chat_template_kwargs": {"enable_thinking": False}} if a.sans_raisonnement else {}


# ----------------------------------------------------------------------------- appels directs (code, LongBench)

def chat(a, contenu: str) -> dict:
    """Un appel /chat/completions, 3 essais. Clé : OPENAI_API_KEY (vide si vLLM n'en exige pas)."""
    corps = {"model": a.modele, "messages": [{"role": "user", "content": contenu}], "max_tokens": a.max_tokens,
             "temperature": 0, "seed": 1, **extra(a)}
    entetes = {"Content-Type": "application/json"}
    if os.environ.get("OPENAI_API_KEY"):
        entetes["Authorization"] = f"Bearer {os.environ['OPENAI_API_KEY']}"
    req = urllib.request.Request(a.url.rstrip("/") + "/chat/completions", data=json.dumps(corps).encode(), headers=entetes)
    for essai in range(3):
        t0 = time.perf_counter()
        try:
            with urllib.request.urlopen(req, timeout=3600) as r:
                res = json.load(r)
            choix = res["choices"][0]
            msg = choix.get("message") or {}
            return {"texte": msg.get("content") or "", "fin": choix.get("finish_reason"),
                    "jetons": (res.get("usage") or {}).get("completion_tokens"),
                    "jetons_entree": (res.get("usage") or {}).get("prompt_tokens"),
                    "duree_s": round(time.perf_counter() - t0, 2)}
        except urllib.error.HTTPError as e:
            erreur = f"HTTP {e.code} : {e.read()[:300].decode(errors='replace')}"
            if e.code < 500:
                break
        except (urllib.error.URLError, TimeoutError, ConnectionError, ValueError, KeyError) as e:
            erreur = str(e)[:300]
        time.sleep(5 * (essai + 1))
    return {"texte": "", "fin": "erreur", "erreur": erreur}


def en_parallele(a, invites: list[str]) -> list[dict]:
    fait = [0]

    def un(invite):
        r = chat(a, invite)
        fait[0] += 1
        if fait[0] % 10 == 0 or fait[0] == len(invites):
            print(f"  {fait[0]}/{len(invites)}", flush=True)
        return r

    with ThreadPoolExecutor(max(1, a.concurrence)) as ex:
        return list(ex.map(un, invites))


def bloc_code(texte: str, langages: tuple[str, ...]) -> str:
    blocs = BLOC_CODE.findall(texte)
    bons = [c for l, c in blocs if l.lower() in langages] or [c for _, c in blocs]
    return max(bons, key=len) if bons else texte


def stats_reponses(reponses: list[dict]) -> dict:
    n = len(reponses) or 1
    jetons = sorted(r["jetons"] for r in reponses if r.get("jetons"))
    return {"tronquees_pct": round(100 * sum(r.get("fin") == "length" for r in reponses) / n, 1),
            "erreurs_api": sum(r.get("fin") == "erreur" for r in reponses),
            "jetons_sortie_p50": jetons[len(jetons) // 2] if jetons else None}


# ----------------------------------------------------------------------------- préparation (avec Internet)

def cmd_preparer(a) -> None:
    echecs = []

    def etape(nom, f):
        print(f"• {nom}…", flush=True)
        try:
            f()
        except Exception as e:  # une source indisponible ne bloque pas les autres
            echecs.append(nom)
            print(f"  ✗ {nom} : {str(e)[:400]}", flush=True)

    def lm_eval_jeux():
        from lm_eval.tasks import TaskManager, get_task_dict
        taches = [t for t in a.taches_lm_eval.split(",") if t and not t.startswith(("niah_", "ruler"))]
        for t in taches:  # une par une : GPQA (accès soumis à conditions) peut échouer seul
            etape(f"lm-eval {t}", lambda t=t: get_task_dict([t], TaskManager()))

    def nltk_punkt():
        import nltk
        nltk.download("punkt_tab", download_dir=os.environ.get("NLTK_DATA", str(RACINE / "nltk")), quiet=True)

    def evalplus_jeux():
        from evalplus.data import get_human_eval_plus, get_mbpp_plus
        print(f"  HumanEval+ {len(get_human_eval_plus())}, MBPP+ {len(get_mbpp_plus())}")

    def multipl_e():
        from datasets import load_dataset
        d = load_dataset("nuprl/MultiPL-E", "humaneval-cpp", split="test")
        f = DONNEES / "multipl-e" / "humaneval-cpp.jsonl"
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("".join(json.dumps({k: r[k] for k in ("name", "prompt", "tests")}) + "\n" for r in d))
        print(f"  {len(d)} problèmes C++")

    def longbench():
        from datasets import load_dataset
        for nom in ("zai-org/LongBench-v2", "THUDM/LongBench-v2"):
            try:
                d = load_dataset(nom, split="train")
                break
            except Exception as e:
                derniere = e
        else:
            raise derniere
        f = DONNEES / "longbench2.jsonl"
        f.parent.mkdir(parents=True, exist_ok=True)
        cles = ("_id", "domain", "sub_domain", "difficulty", "length", "question",
                "choice_A", "choice_B", "choice_C", "choice_D", "answer", "context")
        f.write_text("".join(json.dumps({k: r[k] for k in cles}, ensure_ascii=False) + "\n" for r in d))
        print(f"  {len(d)} questions")

    etape("lm-eval (jeux de données)", lm_eval_jeux)
    etape("IFEval : nltk punkt_tab", nltk_punkt)
    etape("EvalPlus (HumanEval+, MBPP+)", evalplus_jeux)
    etape("MultiPL-E HumanEval C++", multipl_e)
    etape("LongBench v2", longbench)
    print("\nRULER : jeux générés au lancement (tokenizer du modèle + essais publics téléchargés à chaque fois).")
    if echecs:
        print(f"⚠ Échecs : {', '.join(echecs)}. GPQA est soumis à conditions : accepter sur huggingface.co/datasets/Idavidrein/gpqa "
              "puis relancer avec HF_TOKEN dans l'environnement.")
        sys.exit(1)


# ----------------------------------------------------------------------------- lm-evaluation-harness

def score_principal(tache: str, metriques: dict) -> dict:
    """Garde les métriques utiles (pas les erreurs-types), au format tache:metrique, en %. RULER : une par longueur."""
    res = {}
    for k, v in metriques.items():
        if not isinstance(v, (int, float)) or isinstance(v, bool) or "stderr" in k or k in ("samples", "sample_len"):
            continue
        metrique, _, filtre = k.partition(",")
        if tache.startswith("mmlu_pro") and filtre != "custom-extract":
            continue
        if tache.startswith("gpqa") and filtre != "flexible-extract":
            continue
        if tache == "ifeval" and metrique not in ("prompt_level_strict_acc", "inst_level_strict_acc"):
            continue
        if v < 0:  # RULER : -1 pour une longueur non demandée
            continue
        res[f"{tache}:{metrique}"] = round(100 * float(v), 1)  # lm-eval note de 0 à 1, ici en %
    return res


def cmd_lm_eval(a) -> None:
    import lm_eval

    sortie = Path(a.sortie)
    taches = [t for t in a.taches.split(",") if t]
    args = {"model": a.modele, "base_url": a.url.rstrip("/") + "/chat/completions", "num_concurrent": a.concurrence,
            "max_retries": 3, "timeout": 3600, "tokenized_requests": False, "max_length": a.contexte,
            "max_gen_toks": a.max_tokens, "think_end_token": "</think>"}
    metadata = None
    if any(t.startswith(("niah_", "ruler")) for t in taches):
        if not a.tokenizer:
            sys.exit("RULER a besoin du tokenizer du modèle (--tokenizer /models/<dossier>)")
        args |= {"tokenizer": a.tokenizer, "tokenizer_backend": "huggingface"}
        metadata = {"max_seq_lengths": [int(x) for x in a.longueurs.split(",")], "tokenizer": a.tokenizer}
    else:
        args["tokenizer_backend"] = None
    # Modèles de raisonnement : pas de séquence d'arrêt (elle couperait la réflexion) et un budget de jetons large
    gen = {"max_gen_toks": a.max_tokens, "until": [], **extra(a)}
    t0 = time.time()
    res = lm_eval.simple_evaluate(model="local-chat-completions", model_args=args, tasks=taches,
                                  limit=a.limite or None, apply_chat_template=True, fewshot_as_multiturn=True,
                                  gen_kwargs=gen, log_samples=True, metadata=metadata)
    scores = {}
    for tache, metriques in (res.get("results") or {}).items():
        scores |= score_principal(tache, metriques)
    sortie.mkdir(parents=True, exist_ok=True)
    with (sortie / "reponses.jsonl").open("w") as f:
        for tache, lignes in (res.get("samples") or {}).items():
            for l in lignes:
                f.write(json.dumps({"tache": tache, "doc_id": l.get("doc_id"), "cible": l.get("target"),
                                    "reponse": l.get("filtered_resps"), "brut": l.get("resps")},
                                   ensure_ascii=False, default=str) + "\n")
    ecrire(sortie / "score.json", {"scores": scores, "details": {"resultats": res.get("results"), "n": res.get("n-samples"),
                                                                 "versions": res.get("versions"), "duree_s": round(time.time() - t0)}})
    print(json.dumps(scores, indent=2))


# ----------------------------------------------------------------------------- code : HumanEval+ / MBPP+ / MultiPL-E C++

def problemes(jeu: str) -> list[dict]:
    if jeu in ("humaneval", "mbpp"):
        from evalplus.data import get_human_eval_plus, get_mbpp_plus
        d = get_human_eval_plus() if jeu == "humaneval" else get_mbpp_plus()
        return [{"id": k, "prompt": v["prompt"], "entree": v["entry_point"]} for k, v in d.items()]
    f = DONNEES / "multipl-e" / "humaneval-cpp.jsonl"
    if not f.exists():
        sys.exit(f"{f} absent : lancer d'abord  ./scripts/bench-public.py preparer")
    return [{"id": r["name"], "prompt": r["prompt"], "tests": r["tests"]} for r in jsonl(f)]


def cmd_code_gen(a) -> None:
    tous = problemes(a.jeu)
    choisis = tous[: a.limite] if a.limite else tous  # toujours les mêmes, dans l'ordre du jeu
    if a.jeu == "cpp":
        invites = [f"{CPP_CONSIGNE}\n```cpp\n{p['prompt'].strip()}\n```" for p in choisis]
    else:
        invites = [f"{EVALPLUS_CONSIGNE}\n```\n{p['prompt'].strip()}\n```" for p in choisis]
    print(f"{a.jeu} : {len(choisis)}/{len(tous)} problèmes", flush=True)
    reponses = en_parallele(a, invites)
    sortie = Path(a.sortie)
    sortie.mkdir(parents=True, exist_ok=True)
    with (sortie / "reponses.jsonl").open("w") as f:
        for p, r in zip(choisis, reponses):
            f.write(json.dumps({"id": p["id"], **r}, ensure_ascii=False) + "\n")
    ecrire(sortie / "generation.json", {"jeu": a.jeu, "choisis": [p["id"] for p in choisis], **stats_reponses(reponses)})


def cmd_code_note(a) -> None:
    sortie = Path(a.sortie)
    gen = json.loads((sortie / "generation.json").read_text())
    reponses = {r["id"]: r for r in jsonl(sortie / "reponses.jsonl")}
    if a.jeu == "cpp":
        res = noter_cpp(problemes("cpp"), reponses, a.parallele)
        scores = {"multipl-e_cpp:pass@1": round(100 * sum(res.values()) / max(1, len(res)), 1)}
    else:
        scores, res = noter_evalplus(a.jeu, reponses, sortie, a.parallele)
    stats = {k: gen[k] for k in ("tronquees_pct", "erreurs_api", "jetons_sortie_p50")}
    ecrire(sortie / "score.json", {"scores": scores, "details": {"problemes": len(res), **stats, "par_probleme": res}})
    print(json.dumps(scores, indent=2))


def noter_evalplus(jeu: str, reponses: dict, sortie: Path, parallele: int) -> tuple[dict, dict]:
    from evalplus.data import get_human_eval_plus, get_mbpp_plus
    from evalplus.evaluate import evaluate
    from evalplus.sanitize import sanitize

    tous = get_human_eval_plus() if jeu == "humaneval" else get_mbpp_plus()
    echantillons = sortie / "echantillons.jsonl"
    with echantillons.open("w") as f:  # EvalPlus exige tous les problèmes : vide pour ceux non tirés (ignorés au score)
        for tid, p in tous.items():
            code = sanitize(bloc_code(reponses[tid]["texte"], ("python", "py", "")), p["entry_point"]) if tid in reponses else ""
            f.write(json.dumps({"task_id": tid, "solution": code}) + "\n")
    resultat = sortie / "echantillons_eval_results.json"
    resultat.unlink(missing_ok=True)
    evaluate(dataset=jeu, samples=str(echantillons), parallel=parallele, i_just_wanna_run=True)
    ev = json.loads(resultat.read_text())["eval"]
    res = {tid: {"base": ev[tid][0]["base_status"] == "pass",
                 "plus": ev[tid][0]["base_status"] == ev[tid][0]["plus_status"] == "pass"} for tid in reponses}
    n = max(1, len(res))
    nom = "humaneval" if jeu == "humaneval" else "mbpp"
    return {f"{nom}:pass@1": round(100 * sum(r["base"] for r in res.values()) / n, 1),
            f"{nom}+:pass@1": round(100 * sum(r["plus"] for r in res.values()) / n, 1)}, res


def noter_cpp(probs: list[dict], reponses: dict, parallele: int) -> dict:
    """Code du modèle + tests MultiPL-E (main avec assert), compilé et exécuté. Le conteneur n'a pas de réseau."""
    tests = {p["id"]: p["tests"] for p in probs}

    def un(tid):
        code = bloc_code(reponses[tid]["texte"], ("cpp", "c++", "cxx", ""))
        t = tests[tid].lstrip()
        if t.startswith("}"):  # les tests MultiPL-E referment la fonction de l'invite : ici le modèle l'a déjà fermée
            t = t[1:]
        with tempfile.TemporaryDirectory() as d:
            Path(d, "p.cpp").write_text(code + "\n" + t)
            try:
                c = subprocess.run(["g++", "-std=c++17", "-O1", "-w", "p.cpp", "-o", "p"], cwd=d, capture_output=True, timeout=120)
                if c.returncode != 0:
                    return False
                return subprocess.run(["./p"], cwd=d, capture_output=True, timeout=15).returncode == 0
            except subprocess.TimeoutExpired:
                return False

    ids = list(reponses)
    with ThreadPoolExecutor(max(1, parallele)) as ex:
        return dict(zip(ids, ex.map(un, ids)))


# ----------------------------------------------------------------------------- LongBench v2

def cmd_longbench(a) -> None:
    f = DONNEES / "longbench2.jsonl"
    if not f.exists():
        sys.exit(f"{f} absent : lancer d'abord  ./scripts/bench-public.py preparer")
    longueurs = set(a.longueurs.split(","))
    lignes = [r for r in jsonl(f) if r["length"] in longueurs]
    lignes = lignes[: a.limite] if a.limite else lignes
    # Contexte trop long : on coupe au milieu, comme le script officiel (la question est à la fin)
    budget = a.contexte - a.max_tokens - 1024
    tok = None
    if a.tokenizer:
        from transformers import AutoTokenizer
        tok = AutoTokenizer.from_pretrained(a.tokenizer, trust_remote_code=True)

    def couper(texte: str) -> str:
        if tok is None:
            car = budget * 3  # sans tokenizer : 3 caractères par jeton, prudent
            return texte if len(texte) <= car else texte[: car // 2] + texte[-car // 2:]
        ids = tok.encode(texte, add_special_tokens=False)
        return texte if len(ids) <= budget else tok.decode(ids[: budget // 2]) + tok.decode(ids[-budget // 2:])

    invites = [LONGBENCH_INVITE.format(contexte=couper(r["context"]), question=r["question"].strip(),
                                       a=r["choice_A"], b=r["choice_B"], c=r["choice_C"], d=r["choice_D"]) for r in lignes]
    print(f"LongBench v2 : {len(lignes)} questions ({a.longueurs})", flush=True)
    reponses = en_parallele(a, invites)
    sortie = Path(a.sortie)
    sortie.mkdir(parents=True, exist_ok=True)
    justes, par = [], {}
    with (sortie / "reponses.jsonl").open("w") as fo:
        for r, rep in zip(lignes, reponses):
            texte = rep["texte"].replace("*", "")
            m = re.search(r"The correct answer is \(([A-D])\)", texte) or re.search(r"The correct answer is ([A-D])", texte)
            ok = bool(m) and m.group(1) == r["answer"]
            justes.append(ok)
            for cle in (f"longueur_{r['length']}", f"difficulte_{r['difficulty']}"):
                par.setdefault(cle, []).append(ok)
            fo.write(json.dumps({"id": r["_id"], "attendu": r["answer"], "lu": m.group(1) if m else None, **rep},
                                ensure_ascii=False) + "\n")
    scores = {"longbench2:acc": round(100 * sum(justes) / max(1, len(justes)), 1)}
    scores |= {f"longbench2:acc_{k}": round(100 * sum(v) / len(v), 1) for k, v in sorted(par.items())}
    ecrire(sortie / "score.json", {"scores": scores, "details": {"questions": len(lignes), **stats_reponses(reponses)}})
    print(json.dumps(scores, indent=2))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sp = ap.add_subparsers(dest="cmd", required=True)
    p = sp.add_parser("preparer")
    p.add_argument("--taches-lm-eval", default="")
    p.set_defaults(f=cmd_preparer)
    p = sp.add_parser("lm-eval")
    options_modele(p)
    p.add_argument("--taches", required=True)
    p.add_argument("--contexte", type=int, default=131072, help="longueur maximale du profil")
    p.add_argument("--tokenizer", help="dossier du modèle, pour RULER")
    p.add_argument("--longueurs", default="8192,32768", help="RULER : longueurs testées")
    p.set_defaults(f=cmd_lm_eval)
    p = sp.add_parser("code-gen")
    options_modele(p)
    p.add_argument("--jeu", choices=["humaneval", "mbpp", "cpp"], required=True)
    p.set_defaults(f=cmd_code_gen)
    p = sp.add_parser("code-note")
    p.add_argument("--jeu", choices=["humaneval", "mbpp", "cpp"], required=True)
    p.add_argument("--sortie", required=True)
    p.add_argument("--parallele", type=int, default=max(1, (os.cpu_count() or 2) // 2))
    p.set_defaults(f=cmd_code_note)
    p = sp.add_parser("longbench")
    options_modele(p)
    p.add_argument("--longueurs", default="short")
    p.add_argument("--contexte", type=int, default=131072)
    p.add_argument("--tokenizer")
    p.set_defaults(f=cmd_longbench)
    a = ap.parse_args()
    a.f(a)


if __name__ == "__main__":
    main()
