"""Phase 1 — montée en charge par modèle, en direct sur chaque serveur vLLM (sans LiteLLM).

Pour chaque rôle, chaque longueur de prompt et chaque mode, la concurrence monte par paliers
(N clients qui enchaînent les requêtes en boucle fermée) jusqu'à ce que l'objectif casse.
Le dernier palier tenu est le « genou » du modèle.

Modes :
  froid    prompt aléatoire unique à chaque requête : pire cas, le cache de préfixe ne sert à rien.
  prefixe  chaque client garde son historique (préfixe fixe) et n'ajoute qu'un suffixe de 2K tokens,
           comme une session OpenCode qui renvoie sa conversation. Le TTFT est mesuré hors 1re requête.

Un palier tient si : taux d'erreur ≤ objectif, vitesse p95 ≥ vitesse_min du rôle, et TTFT p95 ≤
ttft_chaud_s (mode prefixe) ou max(ttft_chaud_s, ttft_froid_64k_s × longueur / 65 536) (mode froid).
Pour comparer avec et sans cache de préfixe, relancer vLLM sans --enable-prefix-caching et refaire --mode prefixe.
"""
import threading
import time

from bench.commun import (Generateur, Surveillance, afficher_palier, chat, charger_config, cle_directe,
                          cle_maitre, contexte_max, resumer, role, sauver)

SUFFIXE = 2000
CONSIGNE = "\n\nRésume ce qui précède en une liste de points, de façon détaillée."


def options(p):
    p.add_argument("--roles", help="rôles à tester, séparés par des virgules (défaut : tous)")
    p.add_argument("--niveaux", help="concurrences, ex. 1,2,4,8")
    p.add_argument("--longueurs", help="longueurs de prompt en tokens, ex. 4000,32000,64000")
    p.add_argument("--mode", choices=["froid", "prefixe", "les-deux"], default="les-deux")
    p.add_argument("--duree", type=float, help="durée d'un palier en secondes")
    p.add_argument("--max-tokens", type=int)
    p.add_argument("--via-passerelle", action="store_true", help="passer par LiteLLM au lieu de vLLM direct")
    p.add_argument("--continuer", action="store_true", help="ne pas s'arrêter au premier palier qui casse")


def _liste(texte, defaut):
    return [int(x) for x in texte.split(",")] if texte else list(defaut)


def palier(base, cle, modele, concurrence, duree, fabrique, max_tokens, urls_metriques):
    fin = time.monotonic() + duree
    mesures, verrou = [], threading.Lock()

    def client(i):
        k = 0
        while time.monotonic() < fin:
            m = chat(base, cle, modele, fabrique(i, k), max_tokens=max_tokens, timeout=max(600, duree * 2))
            m.info = {"client": i, "rang": k}
            with verrou:
                mesures.append(m)
            k += 1

    surv = Surveillance(urls_metriques)
    surv.start()
    t0 = time.monotonic()
    fils = [threading.Thread(target=client, args=(i,), daemon=True) for i in range(concurrence)]
    for f in fils:
        f.start()
    for f in fils:
        f.join()
    ecoule = time.monotonic() - t0
    return mesures, ecoule, surv.arreter()


def lancer(args):
    cfg = charger_config(args.profil)
    c = cfg.get("capacite", {})
    slo = cfg["slo"]
    cle = cle_maitre()
    roles = args.roles.split(",") if args.roles else list(cfg["roles"])
    niveaux = _liste(args.niveaux, c.get("niveaux", [1, 2, 4, 8]))
    longueurs = _liste(args.longueurs, c.get("longueurs", [4000, 32000]))
    duree = args.duree or c.get("duree_palier_s", 120)
    max_tokens = args.max_tokens or c.get("max_tokens", 256)
    modes = ["froid", "prefixe"] if args.mode == "les-deux" else [args.mode]
    resultats = {}

    for nom_role in roles:
        r = role(cfg, nom_role)
        base = cfg["passerelle"] if args.via_passerelle else r["direct"]
        cle_d = cle_directe(r, cle)
        cmax = contexte_max(r["direct"], r["modele"], cle_d) or r.get("contexte_max")
        vmin = slo["vitesse_min"].get(nom_role, 20.0)
        print(f"\n=== {nom_role} ({r['modele']} via {base}) — contexte max {cmax or 'inconnu'} ===")
        essais = []
        for mode in modes:
            for lng in longueurs:
                if cmax and lng + max_tokens + 200 > cmax:
                    print(f"  {mode} {lng} : ignoré (dépasse le contexte de {cmax})")
                    continue
                ttft_lim = (slo["ttft_chaud_s"] if mode == "prefixe"
                            else max(slo["ttft_chaud_s"], slo["ttft_froid_64k_s"] * lng / 65536))
                print(f"\n  mode {mode}, prompt {lng} tokens — objectif TTFT p95 ≤ {ttft_lim:.1f}s, "
                      f"vitesse p95 ≥ {vmin} tok/s")
                prefixes = {}

                def fabrique(i, k, mode=mode, lng=lng, prefixes=prefixes):
                    gen = Generateur()
                    if mode == "froid":
                        return [{"role": "user", "content": gen.unique() + "\n" + gen.texte(lng) + CONSIGNE}]
                    if i not in prefixes:
                        g = Generateur(graine=f"{lng}-{i}")
                        prefixes[i] = g.unique() + "\n" + g.texte(max(lng - SUFFIXE, 0))
                    return [{"role": "user", "content": prefixes[i] + "\n" + gen.texte(min(SUFFIXE, lng)) + CONSIGNE}]

                paliers, genou = [], None
                for n in niveaux:
                    mesures, ecoule, metriques = palier(base, cle if args.via_passerelle else cle_d, r["modele"], n, duree, fabrique,
                                                        max_tokens, cfg.get("metriques", []))
                    chaudes = [m for m in mesures if mode == "froid" or m.info["rang"] > 0]
                    res = resumer(chaudes or mesures, ecoule)
                    res["toutes"] = resumer(mesures, ecoule)
                    bons = [m for m in chaudes if m.ok and m.ttft is not None and m.ttft <= ttft_lim
                            and (m.tpot is None or 1 / m.tpot >= vmin)]
                    res["goodput_req_s"] = round(len(bons) / ecoule, 3)
                    res["concurrence"] = n
                    res["metriques"] = metriques
                    tient = (res["requetes"] > 0 and res["taux_erreur"] <= slo["taux_erreur"]
                             and res["vitesse_p95"] is not None and res["vitesse_p95"] >= vmin
                             and res["ttft_p95_s"] is not None and res["ttft_p95_s"] <= ttft_lim)
                    res["tient"] = tient
                    paliers.append(res)
                    afficher_palier(f"{n} client(s) {'✅' if tient else '❌'}", res)
                    if metriques.get("kv_max") is not None:
                        print(f"  {'':<22} KV max={metriques['kv_max']:.0%}  attente max={metriques['attente_max']}"
                              f"  préemptions={metriques['preemptions']}  cache préfixe={metriques['taux_cache_prefixe']}")
                    if not tient:
                        if genou is None:
                            genou = paliers[-2]["concurrence"] if len(paliers) > 1 and paliers[-2]["tient"] else 0
                        if not args.continuer:
                            break
                if genou is None:
                    genou = paliers[-1]["concurrence"] if paliers and paliers[-1]["tient"] else 0
                    plafond_atteint = False
                else:
                    plafond_atteint = True
                print(f"  → genou : {genou}{'' if plafond_atteint else ' (au moins : aucun palier n’a cassé)'}")
                essais.append({"mode": mode, "longueur": lng, "ttft_limite_s": round(ttft_lim, 2),
                               "vitesse_min": vmin, "paliers": paliers, "genou": genou,
                               "plafond_atteint": plafond_atteint})
        resultats[nom_role] = {"modele": r["modele"], "base": base, "contexte_max": cmax, "essais": essais}

    sauver("capacite", cfg, {"duree_palier_s": duree, "max_tokens": max_tokens, "roles": resultats})
