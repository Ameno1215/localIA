"""Point d'entrée : python3 -m bench <commande> [options]   (depuis la racine du dépôt)

Commandes (dans l'ordre du plan de test) :
  calibrer   mesure le ratio mots → tokens du modèle (à faire une fois par modèle)
  capacite   phase 1 : montée en charge de chaque modèle, en direct sur vLLM
  charge     phase 2 : charge mixte réaliste à travers LiteLLM (et phase 4 avec --endurance)
  rag        phase 3 : latences embeddings / rerank / recherche et rappel@5
  thermique  phase 4 : analyse des relevés de bench/sonde_noeud.py
  pannes     phase 5 : pannes et reprise, durée d'impact mesurée
  limites    phase 6 : prompts trop longs, clé gourmande, max_tokens absent, appels d'outils
  qualite    phase 7 : tickets internes résolus par OpenCode, questions vision
  rapport    tableau go / no-go à partir des derniers résultats
La phase 0 (réseau) est le script bench/reseau.sh.
"""
import argparse
import importlib
import sys

COMMANDES = {
    "calibrer": "bench.calibrer",
    "capacite": "bench.capacite",
    "charge": "bench.charge",
    "rag": "bench.rag",
    "thermique": "bench.thermique",
    "pannes": "bench.pannes",
    "limites": "bench.limites",
    "qualite": "bench.qualite",
    "rapport": "bench.rapport",
}


def main():
    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help") or sys.argv[1] not in COMMANDES:
        print(__doc__)
        sys.exit(0 if len(sys.argv) > 1 and sys.argv[1] in ("-h", "--help") else 2)
    nom = sys.argv[1]
    module = importlib.import_module(COMMANDES[nom])
    p = argparse.ArgumentParser(prog=f"python3 -m bench {nom}", description=module.__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--profil", help="profil de bench/config.toml (défaut : BENCH_PROFIL ou profil_defaut)")
    module.options(p)
    args = p.parse_args(sys.argv[2:])
    sys.exit(module.lancer(args) or 0)


if __name__ == "__main__":
    main()
