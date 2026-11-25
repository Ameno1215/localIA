"""Mesure combien de tokens fait un mot du générateur de prompts, pour viser des longueurs justes.

Envoie un texte de 2 000 mots avec max_tokens=1 et lit usage.prompt_tokens.
Le ratio est enregistré dans resultats/calibration.json et repris par toutes les phases.
"""
import json

from bench.commun import Generateur, chat, charger_config, cle_directe, cle_maitre, dossier_resultats, role


def options(p):
    p.add_argument("--role", default="code-agent")
    p.add_argument("--direct", action="store_true", help="interroger vLLM en direct plutôt que LiteLLM")


def lancer(args):
    cfg = charger_config(args.profil)
    r = role(cfg, args.role)
    base = r["direct"] if args.direct else cfg["passerelle"]
    gen = Generateur(graine=1, tokens_par_mot=1.0)
    n_mots = 2000
    texte = gen.texte(n_mots)
    reel = len(texte.split())
    m = chat(base, cle_directe(r, cle_maitre()) if args.direct else cle_maitre(), r["modele"], [{"role": "user", "content": texte}], max_tokens=1, timeout=120)
    if not m.ok or not m.tokens_entree:
        print(f"Échec de la calibration : statut {m.statut} {m.erreur}")
        return 1
    ratio = round(m.tokens_entree / reel, 3)
    (dossier_resultats() / "calibration.json").write_text(json.dumps(
        {"modele": r["modele"], "mots": reel, "tokens": m.tokens_entree, "tokens_par_mot": ratio}, indent=2))
    print(f"{reel} mots → {m.tokens_entree} tokens : {ratio} token/mot (enregistré)")
