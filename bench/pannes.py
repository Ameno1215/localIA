"""Phase 5 — pannes et reprise : casser une chose à la fois et chronométrer l'impact.

Pour chaque scénario de la config, une sonde envoie une petite requête par seconde au modèle du
scénario, à travers LiteLLM, comme le ferait OpenCode. On fait la panne (à la main, ou avec
--executer si le scénario a une `commande`), et la sonde mesure :
  - le moment de la première erreur et le temps jusqu'au retour (10 succès d'affilée) ;
  - les erreurs vues par les clients, par statut ;
  - le modèle qui a répondu pendant la panne (repli LiteLLM ou non).
Une panne sans aucune erreur visible (repli parfait) compte comme réussie.

Les scénarios sans `commande` attendent que tu appuies sur Entrée quand la panne est en place,
puis quand tu as remis le service en route.
"""
import subprocess
import threading
import time
from collections import Counter

from bench.commun import DEPOT, chat, charger_config, cle_maitre, sauver

SUCCES_RETOUR = 10


def options(p):
    p.add_argument("--scenarios", help="noms des scénarios à jouer, séparés par des virgules")
    p.add_argument("--executer", action="store_true", help="lancer la `commande` du scénario au lieu d'attendre")
    p.add_argument("--delai-max", type=float, default=1800, help="abandon si pas de retour après N secondes")
    p.add_argument("--cle", help="clé à utiliser pour la sonde (une clé utilisateur prouve qu'elle survit "
                                 "au redémarrage de LiteLLM ; défaut : clé maître)")


class Sonde(threading.Thread):
    def __init__(self, cfg, nom_role, cle):
        super().__init__(daemon=True)
        self.base = cfg["passerelle"]
        self.modele = cfg["roles"][nom_role]["modele"]
        self.cle = cle
        self.points = []          # (t, ok, statut, modele_repondu, duree)
        self.stop = threading.Event()

    def run(self):
        while not self.stop.is_set():
            t = time.time()
            m = chat(self.base, self.cle, self.modele, [{"role": "user", "content": "Réponds juste OK."}],
                     max_tokens=8, timeout=30)
            self.points.append((t, m.ok, m.statut, m.modele_repondu, m.duree))
            self.stop.wait(max(0.0, 1.0 - (time.time() - t)))

    def retour_depuis(self, t0):
        """Instant du début de la première série de SUCCES_RETOUR succès après t0 (None sinon)."""
        serie = []
        for p in self.points:
            if p[0] < t0:
                continue
            serie = serie + [p] if p[1] else []
            if len(serie) >= SUCCES_RETOUR:
                return serie[0][0]
        return None


def jouer(cfg, sc, executer, delai_max, cle):
    print(f"\n=== {sc['nom']} ===\n{sc['consigne']}")
    sonde = Sonde(cfg, sc.get("role", "code-agent"), cle)
    sonde.start()
    time.sleep(10)
    base_ok = sum(p[1] for p in sonde.points)
    if base_ok == 0:
        sonde.stop.set()
        print("⚠ Le service ne répond pas AVANT la panne : scénario ignoré.")
        return {"nom": sc["nom"], "ignore": "service indisponible avant la panne"}
    t_panne = time.time()
    if executer and sc.get("commande"):
        print(f"→ exécution : {sc['commande']}")
        proc = subprocess.run(sc["commande"], shell=True, cwd=DEPOT)
        print(f"  (code retour {proc.returncode})")
    else:
        input("Appuie sur Entrée une fois la panne provoquée… ")
        t_panne = time.time()
        input("Remets le service en route si besoin, puis appuie sur Entrée… ")
    print("Attente du retour (10 succès d'affilée)…", flush=True)
    fin_max = time.time() + delai_max
    while time.time() < fin_max:
        echecs = [p for p in sonde.points if p[0] >= t_panne and not p[1]]
        retour = sonde.retour_depuis(echecs[-1][0]) if echecs else sonde.retour_depuis(t_panne)
        if retour and time.time() - retour > SUCCES_RETOUR:
            break
        time.sleep(2)
    sonde.stop.set()
    sonde.join(timeout=35)

    apres = [p for p in sonde.points if p[0] >= t_panne]
    echecs = [p for p in apres if not p[1]]
    premier_echec = echecs[0][0] if echecs else None
    retour = sonde.retour_depuis(echecs[-1][0]) if echecs else (apres[0][0] if apres else None)
    impact = (retour - premier_echec) if echecs and retour else (0.0 if not echecs else None)
    modeles = Counter(p[3] for p in apres if p[1] and p[3])
    res = {
        "nom": sc["nom"], "role": sc.get("role"),
        "sondes": len(apres), "echecs": len(echecs),
        "erreurs": dict(Counter(str(p[2]) for p in echecs)),
        "impact_s": None if impact is None else round(impact, 1),
        "revenu": impact is not None,
        "modeles_ayant_repondu": dict(modeles),
        "reussi": impact is not None and impact <= cfg["slo"]["reprise_max_s"],
    }
    print(f"→ échecs vus : {len(echecs)} {res['erreurs']}  durée d'impact : "
          f"{'pas de retour !' if impact is None else f'{impact:.0f} s'}  modèles : {res['modeles_ayant_repondu']}")
    return res


def lancer(args):
    cfg = charger_config(args.profil)
    scenarios = cfg.get("pannes", [])
    if args.scenarios:
        voulus = args.scenarios.split(",")
        scenarios = [s for s in scenarios if s["nom"] in voulus]
    if not scenarios:
        raise SystemExit("Aucun scénario de panne dans la config pour ce profil.")
    cle = args.cle or cle_maitre()
    resultats = [jouer(cfg, sc, args.executer, args.delai_max, cle) for sc in scenarios]
    sauver("pannes", cfg, {"scenarios": resultats})
