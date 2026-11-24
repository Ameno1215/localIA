"""Phase 6 — limites et abus : les requêtes mauvaises ou gourmandes échouent proprement
sans dégrader les autres utilisateurs.

Tests :
  trop-long      un prompt au-delà du contexte du modèle reçoit une erreur 4xx propre et rapide ;
  gourmand       une clé limitée lance N requêtes en parallèle : elle doit recevoir des 429,
                 pendant qu'une clé témoin garde sa vitesse (≥ vitesse_min du rôle) sans erreur ;
  sans-max       une requête sans max_tokens est plafonnée côté serveur ;
  outils         sous charge, les appels d'outils sont bien formés (nom connu, JSON valide).

Le test « gourmand » crée deux clés de test (bench-gourmand avec max_parallel_requests=2,
bench-temoin) avec la clé maître et les supprime à la fin. Sinon : --cle-gourmande et --cle-temoin.
"""
import json
import threading
import time

from bench.charge import creer_cles, supprimer_cles
from bench.commun import Generateur, chat, charger_config, cle_maitre, contexte_max, resumer, role, sauver

OUTIL = [{
    "type": "function",
    "function": {
        "name": "lire_registre",
        "description": "Lit un registre matériel du microcontrôleur.",
        "parameters": {
            "type": "object",
            "properties": {"adresse": {"type": "string", "description": "adresse hexadécimale, ex. 0x40021000"},
                           "taille": {"type": "integer", "description": "taille en octets (1, 2 ou 4)"}},
            "required": ["adresse", "taille"],
        },
    },
}]


def options(p):
    p.add_argument("--tests", default="trop-long,gourmand,sans-max,outils")
    p.add_argument("--cle-gourmande")
    p.add_argument("--cle-temoin")


def test_trop_long(cfg, r, cle):
    cmax = contexte_max(r["direct"], r["modele"], cle) or r.get("contexte_max")
    if not cmax:
        return {"ignore": "contexte max inconnu (ajoute contexte_max au rôle dans la config)"}
    n = int(cmax * 1.2)
    print(f"\n[trop-long] prompt d'environ {n} tokens pour un contexte de {cmax}")
    m = chat(cfg["passerelle"], cle, r["modele"],
             [{"role": "user", "content": Generateur().texte(n)}], max_tokens=16, timeout=120)
    reussi = 400 <= m.statut < 500 and m.duree < 60
    print(f"  statut {m.statut} en {m.duree:.1f}s — {'✅' if reussi else '❌'} {m.erreur[:120]}")
    return {"contexte_max": cmax, "tokens_vises": n, "statut": m.statut, "duree_s": round(m.duree, 2),
            "erreur": m.erreur[:300], "reussi": reussi}


def test_gourmand(cfg, r, nom_role, maitre, cle_g, cle_t, n_req):
    creees = []
    if not (cle_g and cle_t):
        creees = creer_cles(cfg["passerelle"], maitre, ["bench-gourmand"], [r["modele"]], max_parallel=2)
        creees += creer_cles(cfg["passerelle"], maitre, ["bench-temoin"], [r["modele"]])
        cle_g, cle_t = creees
    try:
        print(f"\n[gourmand] {n_req} requêtes parallèles sur une clé limitée, clé témoin en parallèle")
        msgs = [{"role": "user", "content": "Écris un pilote UART complet en C, commenté."}]
        gourmandes, temoin, verrou = [], [], threading.Lock()
        fini = threading.Event()

        def g():
            m = chat(cfg["passerelle"], cle_g, r["modele"], msgs, max_tokens=256, timeout=600)
            with verrou:
                gourmandes.append(m)

        def t():
            while not fini.is_set():
                m = chat(cfg["passerelle"], cle_t, r["modele"], msgs, max_tokens=128, timeout=600)
                temoin.append(m)

        ft = threading.Thread(target=t)
        ft.start()
        time.sleep(0.5)
        fils = [threading.Thread(target=g) for _ in range(n_req)]
        for f in fils:
            f.start()
        for f in fils:
            f.join()
        fini.set()
        ft.join()
        n429 = sum(1 for m in gourmandes if m.statut == 429)
        rt = resumer(temoin)
        vmin = cfg["slo"]["vitesse_min"].get(nom_role, 20.0)
        reussi = n429 > 0 and rt["taux_erreur"] == 0 and (rt["vitesse_p95"] or 0) >= vmin
        print(f"  clé gourmande : {n429}/{len(gourmandes)} refus 429 ; témoin : vitesse p95 {rt['vitesse_p95']} tok/s "
              f"(min {vmin}), erreurs {rt['taux_erreur']:.0%} — {'✅' if reussi else '❌'}")
        return {"requetes_gourmandes": len(gourmandes), "refus_429": n429,
                "statuts_gourmands": sorted({m.statut for m in gourmandes}), "temoin": rt, "reussi": reussi}
    finally:
        supprimer_cles(cfg["passerelle"], maitre, creees)


def test_sans_max(cfg, r, cle, plafond):
    print(f"\n[sans-max] requête sans max_tokens (plafond attendu ≤ {plafond} tokens)")
    m = chat(cfg["passerelle"], cle, r["modele"],
             [{"role": "user", "content": "Écris la liste de tous les entiers de 1 à 1 000 000, un par ligne, sans t'arrêter."}],
             max_tokens=None, timeout=1800)
    reussi = m.ok and m.tokens_sortie <= plafond
    print(f"  {m.tokens_sortie} tokens en {m.duree:.0f}s, fin={m.fin} — {'✅' if reussi else '❌'} {m.erreur[:120]}")
    return {"tokens_sortie": m.tokens_sortie, "duree_s": round(m.duree, 1), "fin": m.fin, "statut": m.statut,
            "plafond_attendu": plafond, "reussi": reussi}


def _appel_valide(m):
    if not m.ok or len(m.outils) != 1 or m.outils[0]["nom"] != "lire_registre":
        return False
    try:
        a = json.loads(m.outils[0]["arguments"] or "{}")
    except ValueError:
        return False
    return isinstance(a, dict) and "adresse" in a and "taille" in a


def test_outils(cfg, r, cle, concurrence, essais):
    print(f"\n[outils] {essais} appels d'outils à {concurrence} en parallèle")
    mesures, verrou = [], threading.Lock()
    restant = iter(range(essais))

    def fil():
        while True:
            with verrou:
                if next(restant, None) is None:
                    return
            m = chat(cfg["passerelle"], cle, r["modele"],
                     [{"role": "user", "content": "Lis le registre RCC à l'adresse 0x40021000 sur 4 octets."}],
                     max_tokens=1024, timeout=600, outils=OUTIL)
            with verrou:
                mesures.append(m)

    fils = [threading.Thread(target=fil) for _ in range(concurrence)]
    for f in fils:
        f.start()
    for f in fils:
        f.join()
    erreurs_http = sum(1 for m in mesures if not m.ok)
    sans_appel = sum(1 for m in mesures if m.ok and not m.outils)
    invalides = sum(1 for m in mesures if m.ok and m.outils and not _appel_valide(m))
    taux = (sans_appel + invalides) / len(mesures) if mesures else 1.0
    reussi = erreurs_http == 0 and taux <= cfg["slo"]["taux_outils_invalides"]
    exemples = [m.outils for m in mesures if m.ok and m.outils and not _appel_valide(m)][:3]
    print(f"  sans appel : {sans_appel}, mal formés : {invalides}, erreurs HTTP : {erreurs_http} "
          f"→ {taux:.1%} — {'✅' if reussi else '❌'}")
    return {"essais": len(mesures), "sans_appel": sans_appel, "mal_formes": invalides, "erreurs_http": erreurs_http,
            "taux_invalides": round(taux, 4), "exemples": exemples, "reussi": reussi}


def lancer(args):
    cfg = charger_config(args.profil)
    lim = cfg.get("limites", {})
    nom_role = lim.get("role", "code-agent")
    r = role(cfg, nom_role)
    maitre = cle_maitre()
    tests = args.tests.split(",")
    out = {"role": nom_role, "modele": r["modele"]}
    if "trop-long" in tests:
        out["trop_long"] = test_trop_long(cfg, r, maitre)
    if "gourmand" in tests:
        out["gourmand"] = test_gourmand(cfg, r, nom_role, maitre, args.cle_gourmande, args.cle_temoin,
                                        lim.get("requetes_gourmandes", 20))
    if "sans-max" in tests:
        out["sans_max"] = test_sans_max(cfg, r, maitre, lim.get("plafond_sans_max_tokens", 8192))
    if "outils" in tests:
        out["outils"] = test_outils(cfg, r, maitre, lim.get("concurrence_outils", 8), lim.get("essais_outils", 50))
    sauver("limites", cfg, out)
