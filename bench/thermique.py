"""Phase 4 — analyse des relevés de bench/sonde_noeud.py (un CSV par nœud).

Pour chaque nœud : fréquence SM minimale sous charge (relevés où la puissance dépasse la moitié
du maximum observé), température ACPI maximale, mémoire disponible minimale, swap, et trous dans
le journal (plus de 3 périodes sans relevé : extinction ou coupure probable).
"""
import csv
from pathlib import Path

from bench.commun import centile, charger_config, sauver


def options(p):
    p.add_argument("fichiers", nargs="+", help="CSV produits par sonde_noeud.py")


def analyser(chemin: Path, slo: dict) -> dict:
    lignes = list(csv.DictReader(open(chemin)))

    def col(nom):
        return [float(l[nom]) if l.get(nom) not in (None, "") else None for l in lignes]

    t, freq, puiss = col("horodatage"), col("freq_sm_mhz"), col("puissance_w")
    acpi, mem, swap = col("acpi_max_c"), col("mem_dispo_go"), col("swap_utilise_go")
    ecarts = [b - a for a, b in zip(t, t[1:]) if a is not None and b is not None]
    periode = centile(ecarts, 50) or 10
    trous = [round(e) for e in ecarts if e > 3 * periode]
    pmax = max((p for p in puiss if p is not None), default=None)
    sous_charge = [f for f, p in zip(freq, puiss) if f is not None and p is not None and pmax and p >= pmax / 2]
    f_min = min(sous_charge) if sous_charge else None
    a_max = max((a for a in acpi if a is not None), default=None)
    m_min = min((m for m in mem if m is not None), default=None)
    s_vals = [s for s in swap if s is not None]
    s_hausse = round(max(s_vals) - s_vals[0], 2) if s_vals else None
    reussi = ((f_min is None or f_min >= slo["frequence_min_mhz"])
              and (a_max is None or a_max <= slo["acpi_max_c"])
              and not trous and (m_min is None or m_min >= 5) and not s_hausse)
    return {"fichier": chemin.name, "hote": lignes[0]["hote"] if lignes else None, "releves": len(lignes),
            "duree_h": round((t[-1] - t[0]) / 3600, 2) if len(t) > 1 else 0,
            "freq_min_sous_charge_mhz": f_min, "acpi_max_c": a_max, "mem_dispo_min_go": m_min,
            "hausse_swap_go": s_hausse, "trous_s": trous, "reussi": reussi}


def lancer(args):
    cfg = charger_config(args.profil)
    noeuds = []
    for f in args.fichiers:
        r = analyser(Path(f), cfg["slo"])
        noeuds.append(r)
        print(f"{r['hote'] or r['fichier']:<14} {r['duree_h']} h  fréq min sous charge={r['freq_min_sous_charge_mhz']} MHz  "
              f"ACPI max={r['acpi_max_c']} °C  mém dispo min={r['mem_dispo_min_go']} Go  swap +{r['hausse_swap_go']} Go  "
              f"trous={r['trous_s'] or 'aucun'}  {'✅' if r['reussi'] else '❌'}")
    sauver("thermique", cfg, {"noeuds": noeuds, "reussi": all(n["reussi"] for n in noeuds)})
