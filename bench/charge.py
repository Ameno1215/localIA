"""Phase 2 — charge mixte réaliste à travers LiteLLM (et phase 4 avec --endurance).

Chaque développeur virtuel enchaîne des sessions d'agent : 15 à 30 étapes dont le contexte grossit
(10K → 80K tokens sur le Spark), avec un temps de réflexion ou d'outil entre deux étapes.
Le prompt système est le même pour tous (comme celui d'OpenCode) et l'historique ne fait que
s'allonger : le cache de préfixe de vLLM joue comme en vrai. Une part des étapes appelle aussi
les embeddings (outil RAG). Le mélange code-agent / fast / vision vient de la config.

Les paliers ajoutent des développeurs (ex. 10 → 20 → 30 → 45) ; chacun a sa clé LiteLLM :
  --cles fichier      une clé par ligne ;
  --creer-cles        crée des clés bench-vdev-NN avec la clé maître (supprimées à la fin) ;
  sinon               tout le monde utilise la clé maître (l'équité par clé n'est alors pas testée).

Endurance (phase 4) : --endurance 8 --devs 20 tient un seul palier 8 heures et découpe les
résultats en fenêtres de 10 minutes pour mesurer la dérive de latence par rapport à la première heure.
Lancer en parallèle bench/sonde_noeud.py sur chaque nœud (voir README) pour la thermique.
"""
import random
import threading
import time

from bench.commun import (Generateur, Surveillance, afficher_palier, centile, chat, charger_config,
                          cle_maitre, requete, resumer, sauver)


def options(p):
    p.add_argument("--paliers", help="nombre de développeurs virtuels par palier, ex. 10,20,30,45")
    p.add_argument("--duree", type=float, help="durée d'un palier en secondes")
    p.add_argument("--cles", help="fichier de clés LiteLLM, une par ligne")
    p.add_argument("--creer-cles", action="store_true", help="créer une clé par développeur virtuel")
    p.add_argument("--garder-cles", action="store_true", help="ne pas supprimer les clés créées")
    p.add_argument("--endurance", type=float, metavar="HEURES", help="phase 4 : un seul palier pendant HEURES")
    p.add_argument("--devs", type=int, help="développeurs virtuels pour --endurance (défaut : palier cible)")
    p.add_argument("--fenetre", type=float, default=600, help="fenêtre d'analyse en endurance (s)")
    p.add_argument("--echelle-reflexion", type=float, default=1.0,
                   help="multiplie les temps de réflexion (0.1 pour un essai rapide)")


# --------------------------------------------------------------------------- clés LiteLLM

def creer_cles(passerelle, maitre, noms, modeles, max_parallel=None, extra=None):
    cles = []
    for nom in noms:
        corps = {"key_alias": nom, "user_id": nom, "models": modeles, "metadata": {"bench": True}}
        if max_parallel:
            corps["max_parallel_requests"] = max_parallel
        corps.update(extra or {})
        statut, d, _ = requete("POST", passerelle.rstrip("/") + "/key/generate", corps, cle=maitre)
        if statut != 200 or not isinstance(d, dict) or "key" not in d:
            supprimer_cles(passerelle, maitre, cles)
            raise SystemExit(f"Création de la clé {nom} impossible : {statut} {str(d)[:200]}")
        cles.append(d["key"])
    return cles


def supprimer_cles(passerelle, maitre, cles):
    if cles:
        statut, d, _ = requete("POST", passerelle.rstrip("/") + "/key/delete", {"keys": cles}, cle=maitre)
        if statut != 200:
            print(f"⚠ Suppression des clés de test impossible ({statut}) : supprime-les à la main.")


# --------------------------------------------------------------------------- équipe virtuelle

class Equipe:
    def __init__(self, cfg, cles, echelle_reflexion=1.0):
        self.cfg, self.c = cfg, cfg["charge"]
        self.cles = cles
        self.echelle = echelle_reflexion
        self.mesures, self.rag = [], []
        self.verrou = threading.Lock()
        self.stop = threading.Event()
        self.palier = 0
        self.fils = []
        roles = self.c.get("melange", {"code-agent": 1.0})
        self.roles, self.poids = list(roles), list(roles.values())
        g = Generateur(graine="systeme-opencode")
        self.systeme = ("Tu es un agent de code. Tu disposes d'outils pour lire, écrire et exécuter.\n"
                        + g.texte(self.c.get("systeme_tokens", 2000)))

    def ajouter(self, n):
        while len(self.fils) < n:
            i = len(self.fils)
            f = threading.Thread(target=self._dev, args=(i,), daemon=True)
            self.fils.append(f)
            f.start()

    def _dev(self, i):
        rnd = random.Random(i)
        gen = Generateur(graine=f"dev-{i}")
        cle = self.cles[i % len(self.cles)]
        c = self.c
        passerelle = self.cfg["passerelle"]
        while not self.stop.is_set():
            nom_role = rnd.choices(self.roles, self.poids)[0]
            modele = self.cfg["roles"][nom_role]["modele"]
            n_etapes = rnd.randint(*c.get("etapes", [15, 30]))
            pas = (c["contexte_fin"] - c["contexte_debut"]) / max(n_etapes - 1, 1)
            historique = gen.unique() + "\n" + gen.texte(c["contexte_debut"])
            for k in range(n_etapes):
                if self.stop.is_set():
                    return
                msgs = [{"role": "system", "content": self.systeme},
                        {"role": "user", "content": historique + "\n\nPropose la prochaine modification."}]
                m = chat(passerelle, cle, modele, msgs, max_tokens=c.get("max_tokens", 300), timeout=900)
                m.info = {"role": nom_role, "etape": k, "palier": self.palier, "dev": i}
                with self.verrou:
                    self.mesures.append(m)
                if rnd.random() < c.get("prob_rag", 0):
                    t0 = time.perf_counter()
                    statut, _, _ = requete("POST", passerelle.rstrip("/") + "/v1/embeddings",
                                           {"model": c.get("modele_embeddings", "bge-m3"),
                                            "input": [gen.texte(40)]}, cle=cle, timeout=60)
                    with self.verrou:
                        self.rag.append({"debut": time.time(), "statut": statut, "palier": self.palier,
                                         "duree": time.perf_counter() - t0})
                historique += "\n" + gen.texte(pas)
                lo, hi = c.get("reflexion_s", [5, 30])
                if self.stop.wait(rnd.uniform(lo, hi) * self.echelle):
                    return

    def arreter(self, attente=120):
        self.stop.set()
        fin = time.monotonic() + attente
        for f in self.fils:
            f.join(timeout=max(0.0, fin - time.monotonic()))


def _bilan(mesures, rag, duree, metriques=None):
    res = resumer(mesures, duree)
    chaudes = [m for m in mesures if m.info.get("etape", 0) > 0]
    res["ttft_chaud_p95_s"] = resumer(chaudes)["ttft_p95_s"] if chaudes else None
    res["par_role"] = {}
    for nom in sorted({m.info["role"] for m in mesures}):
        res["par_role"][nom] = resumer([m for m in mesures if m.info["role"] == nom], duree)
    ok_rag = [x["duree"] for x in rag if x["statut"] == 200]
    res["rag"] = {"appels": len(rag), "erreurs": len(rag) - len(ok_rag),
                  "embeddings_p95_ms": round(centile(ok_rag, 95) * 1000, 1) if ok_rag else None}
    if metriques is not None:
        res["metriques"] = metriques
    return res


def lancer(args):
    cfg = charger_config(args.profil)
    c = cfg["charge"]
    maitre = cle_maitre()
    paliers = ([int(x) for x in args.paliers.split(",")] if args.paliers else c.get("paliers", [10, 20, 30]))
    if args.endurance:
        paliers = [args.devs or c.get("palier_cible", paliers[-1])]
    duree = args.endurance * 3600 if args.endurance else (args.duree or c.get("duree_palier_s", 900))
    besoin = max(paliers)

    cles_creees = []
    if args.cles:
        cles = [l.strip() for l in open(args.cles) if l.strip() and not l.startswith("#")]
    elif args.creer_cles:
        modeles = sorted({r["modele"] for r in cfg["roles"].values()} | {c.get("modele_embeddings", "bge-m3")})
        cles = cles_creees = creer_cles(cfg["passerelle"], maitre, [f"bench-vdev-{i:02d}" for i in range(besoin)],
                                        modeles, c.get("max_parallel_par_cle"))
        print(f"{len(cles)} clés de test créées.")
    else:
        cles = [maitre]
        print("⚠ Tous les développeurs virtuels utilisent la clé maître (--creer-cles pour une clé chacun).")
    if not cles or not cles[0]:
        raise SystemExit("Aucune clé : définis LITELLM_MASTER_KEY (ou BENCH_CLE), ou passe --cles.")

    equipe = Equipe(cfg, cles, args.echelle_reflexion)
    resultats = []
    try:
        if args.endurance:
            resultats = _endurance(cfg, equipe, paliers[0], duree, args.fenetre)
        else:
            for n in paliers:
                print(f"\n=== Palier : {n} développeurs virtuels, {duree:.0f} s ===")
                surv = Surveillance(cfg.get("metriques", []))
                surv.start()
                equipe.palier = n
                t0, debut = time.monotonic(), time.time()
                equipe.ajouter(n)
                while time.monotonic() - t0 < duree:
                    time.sleep(min(30, duree - (time.monotonic() - t0)))
                    with equipe.verrou:
                        en_cours = [m for m in equipe.mesures if m.debut >= debut]
                    print(f"  … {time.monotonic() - t0:4.0f}s : {len(en_cours)} requêtes terminées", flush=True)
                metriques = surv.arreter()
                with equipe.verrou:
                    mesures = [m for m in equipe.mesures if m.debut >= debut]
                    rag = [x for x in equipe.rag if x["debut"] >= debut]
                res = _bilan(mesures, rag, time.monotonic() - t0, metriques)
                res["devs"] = n
                resultats.append(res)
                afficher_palier(f"{n} devs", res)
                for nom, r in res["par_role"].items():
                    afficher_palier(f"  └ {nom}", r)
                print(f"  TTFT chaud p95={res['ttft_chaud_p95_s']}s  surcoût LiteLLM p95={res['surcout_p95_ms']}ms  "
                      f"préemptions={metriques['preemptions']}  KV max={metriques['kv_max']}  "
                      f"embeddings p95={res['rag']['embeddings_p95_ms']}ms")
    finally:
        print("\nArrêt des développeurs virtuels (requêtes en cours terminées)…")
        equipe.arreter()
        if cles_creees and not args.garder_cles:
            supprimer_cles(cfg["passerelle"], maitre, cles_creees)

    phase = "endurance" if args.endurance else "charge"
    sauver(phase, cfg, {"duree_s": duree, "palier_cible": c.get("palier_cible"), "cles_distinctes": len(cles),
                        "paliers" if not args.endurance else "endurance": resultats})


def _endurance(cfg, equipe, devs, duree, fenetre):
    print(f"\n=== Endurance : {devs} développeurs virtuels pendant {duree / 3600:.1f} h, fenêtres de {fenetre:.0f} s ===")
    equipe.palier = devs
    equipe.ajouter(devs)
    fenetres = []
    t_debut = time.time()
    while time.time() - t_debut < duree:
        surv = Surveillance(cfg.get("metriques", []))
        surv.start()
        debut = time.time()
        equipe.stop.wait(min(fenetre, duree - (debut - t_debut)))
        metriques = surv.arreter()
        fin = time.time()
        with equipe.verrou:
            mesures = [m for m in equipe.mesures if debut <= m.debut < fin]
            rag = [x for x in equipe.rag if debut <= x["debut"] < fin]
        r = _bilan(mesures, rag, fin - debut, metriques)
        r["debut_min"] = round((debut - t_debut) / 60, 1)
        fenetres.append(r)
        afficher_palier(f"t+{r['debut_min']:.0f} min", r)

    def p95(r):
        return r["tpot_p95_ms"], r["ttft_chaud_p95_s"]

    reference = [f for f in fenetres if f["debut_min"] < 60] or fenetres[:1]
    suite = [f for f in fenetres if f["debut_min"] >= 60]
    derive = None
    if reference and suite:
        def med(vals):
            vals = [v for v in vals if v is not None]
            return centile(vals, 50) if vals else None
        ref_tpot = med(p95(f)[0] for f in reference)
        ref_ttft = med(p95(f)[1] for f in reference)
        derives = []
        for f in suite:
            tp, tt = p95(f)
            if ref_tpot and tp:
                derives.append(tp / ref_tpot - 1)
            if ref_ttft and tt:
                derives.append(tt / ref_ttft - 1)
        derive = round(max(derives), 3) if derives else None
    total = resumer(equipe.mesures, duree)
    print(f"\nDérive max de latence p95 après la première heure : {derive if derive is None else f'{derive:+.1%}'}")
    return {"devs": devs, "fenetres": fenetres, "derive_max": derive, "global": total}
