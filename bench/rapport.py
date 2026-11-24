"""Tableau go / no-go à partir des derniers résultats de chaque phase.

Reprend la liste « Go / no-go avant d'ouvrir aux 30 développeurs » du benchmark : chaque ligne est
✅ (passe), ❌ (échoue) ou ⏳ (pas encore mesurée). Écrit resultats/rapport-<date>.md et l'affiche.
N'ouvrir à toute l'équipe que lorsque toutes les lignes sont ✅.
"""
import time

from bench.commun import charger_config, dernier, dossier_resultats


def options(p):
    pass


def _essai(cap, nom_role, longueur_visee, mode_prefere="prefixe"):
    if not cap or nom_role not in cap.get("roles", {}):
        return None
    essais = cap["roles"][nom_role]["essais"]
    if not essais:
        return None
    return min(essais, key=lambda e: (abs(e["longueur"] - longueur_visee), e["mode"] != mode_prefere))


def _palier_cible(charge):
    if not charge or not charge.get("paliers"):
        return None
    cible = charge.get("palier_cible") or charge["paliers"][-1]["devs"]
    ok = [p for p in charge["paliers"] if p["devs"] >= cible]
    return min(ok, key=lambda p: p["devs"]) if ok else None


def lignes(cfg):
    slo = cfg["slo"]
    res, cap, charge = dernier("reseau"), dernier("capacite"), dernier("charge")
    rag, endu, therm = dernier("rag"), dernier("endurance"), dernier("thermique")
    pannes, lim, qual = dernier("pannes"), dernier("limites"), dernier("qualite")
    pc = _palier_cible(charge)
    L = []

    def ajout(controle, critere, phase, reussi, mesure):
        L.append((controle, critere, phase, "⏳" if reussi is None else ("✅" if reussi else "❌"), mesure))

    # Phase 0
    if res:
        ajout("Bande passante NCCL stable, zéro perte RoCE", "écart < 5 % sur 3 essais", "0", res.get("reussi"),
              f"écart {res.get('ecart_max')}, pertes {sum(res.get('pertes', {}).values())}")
    else:
        ajout("Bande passante NCCL stable, zéro perte RoCE", "écart < 5 % sur 3 essais", "0", None, "—")

    # Phase 1
    for nom_role, seuil, lng, critere in (
            ("code-agent", slo["genou_code_agent"], 32000, "sessions simultanées à 32K, TPOT tenu"),
            ("fast", slo["genou_fast"], 4000, "requêtes simultanées, TPOT tenu")):
        if nom_role not in cfg.get("roles", {}):
            continue
        e = _essai(cap, nom_role, lng)
        vmin = slo["vitesse_min"].get(nom_role)
        ajout(f"Genou de {nom_role}", f"≥ {seuil} {critere} (≥ {vmin} tok/s)", "1",
              None if e is None else e["genou"] >= seuil,
              "—" if e is None else f"{e['genou']}{'+' if not e.get('plafond_atteint') else ''} "
                                    f"({e['mode']}, {e['longueur']} tokens)")

    # Phase 2
    if pc:
        ttft = pc.get("ttft_chaud_p95_s")
        ajout("TTFT p95 en cache (tour suivant)", f"< {slo['ttft_chaud_s']} s", "1–2",
              None if ttft is None else ttft < slo["ttft_chaud_s"], f"{ttft} s à {pc['devs']} devs")
        pre = pc.get("metriques", {}).get("preemptions")
        ajout(f"Préemptions à {pc['devs']} développeurs virtuels", "0", "2",
              None if pre is None else pre == 0, "non relevé (métriques injoignables)" if pre is None else f"{pre:.0f}")
        sc = pc.get("surcout_p95_ms")
        ajout("Surcoût LiteLLM p95", f"< {slo['surcout_litellm_p95_ms']} ms", "2",
              None if sc is None else sc < slo["surcout_litellm_p95_ms"], "en-tête absent" if sc is None else f"{sc} ms")
        ajout("Taux d'erreur (toutes causes)", f"< {slo['taux_erreur']:.1%}", "2",
              pc["taux_erreur"] < slo["taux_erreur"], f"{pc['taux_erreur']:.2%} ({pc['requetes']} requêtes)")
    else:
        for c, k, ph in (("TTFT p95 en cache (tour suivant)", f"< {slo['ttft_chaud_s']} s", "1–2"),
                         ("Préemptions à la charge cible", "0", "2"),
                         ("Surcoût LiteLLM p95", f"< {slo['surcout_litellm_p95_ms']} ms", "2"),
                         ("Taux d'erreur (toutes causes)", f"< {slo['taux_erreur']:.1%}", "2")):
            ajout(c, k, ph, None, "—" if not charge else "palier cible non atteint")

    # Phase 3
    lat = (rag or {}).get("latences")
    if lat and lat.get("rerank"):
        rr = max(x["p95_ms"] or 0 for x in lat["rerank"])
        vs = max((x["p95_ms"] or 0 for x in lat.get("recherche", [])), default=None)
        ok = rr < slo["rerank_p95_ms"] and (vs is None or vs < slo["recherche_p95_ms"])
        ajout("Rerank p95 (50 candidats) / recherche vectorielle p95",
              f"< {slo['rerank_p95_ms']:.0f} ms / < {slo['recherche_p95_ms']:.0f} ms", "3", ok, f"{rr} ms / {vs} ms")
    else:
        ajout("Rerank p95 (50 candidats) / recherche vectorielle p95",
              f"< {slo['rerank_p95_ms']:.0f} ms / < {slo['recherche_p95_ms']:.0f} ms", "3", None, "—")
    rp = (rag or {}).get("rappel")
    ajout("Rappel@5 RAG avec reranker", f"≥ {slo['rappel5']:.0%}", "3, 7",
          None if not rp else rp["rappel5_avec_reranker"] >= slo["rappel5"],
          "—" if not rp else f"{rp['rappel5_avec_reranker']:.0%} sur {rp['questions']} questions "
                             f"(sans reranker : {rp['rappel5_sans_reranker']:.0%})")

    # Phase 4
    e = (endu or {}).get("endurance")
    if e:
        d = e.get("derive_max")
        ok_d = None if d is None else d < slo["derive_endurance"]
        ok_t = therm.get("reussi") if therm else None
        etats = [ok_d, ok_t]
        ok = False if False in etats else (True if all(etats) else None)
        ajout("Endurance 8 heures", f"pas d'extinction, fréquence ≥ {slo['frequence_min_mhz']} MHz, dérive < "
              f"{slo['derive_endurance']:.0%}", "4", ok,
              f"dérive {'non calculée (essai < 1 h)' if d is None else f'{d:+.1%}'}, "
              f"erreurs {e['global']['taux_erreur']:.2%}, "
              f"thermique {'non analysée' if ok_t is None else ('OK' if ok_t else 'KO')}")
    else:
        ajout("Endurance 8 heures", f"pas d'extinction, fréquence ≥ {slo['frequence_min_mhz']} MHz", "4", None, "—")

    # Phase 5
    if pannes:
        sc = [s for s in pannes["scenarios"] if "ignore" not in s]
        ok = bool(sc) and all(s["reussi"] for s in sc)
        ajout("Reprise après panne (nœud, vLLM, LiteLLM)", f"repli OK ; retour < {slo['reprise_max_s'] / 60:.0f} min",
              "5", ok, ", ".join(f"{s['nom']} {s['impact_s']}s" for s in sc) or "aucun scénario joué")
    else:
        ajout("Reprise après panne (nœud, vLLM, LiteLLM)", f"retour < {slo['reprise_max_s'] / 60:.0f} min", "5", None, "—")

    # Phase 6
    g = (lim or {}).get("gourmand")
    ajout("Clé gourmande", "reçoit des 429 ; les autres restent dans l'objectif", "6",
          None if not g else g["reussi"], "—" if not g else f"{g['refus_429']} refus 429, témoin "
                                                             f"{g['temoin']['vitesse_p95']} tok/s")
    for cle_, nom, crit in (("trop_long", "Prompt trop long", "erreur 4xx propre"),
                            ("sans_max", "Requête sans max_tokens", "plafonnée côté serveur"),
                            ("outils", "Appels d'outils sous charge", f"≤ {slo['taux_outils_invalides']:.0%} mal formés")):
        t = (lim or {}).get(cle_)
        ajout(nom, crit, "6", None if not t or "ignore" in t else t["reussi"],
              "—" if not t else t.get("ignore") or {"trop_long": lambda: f"statut {t['statut']}",
                                                     "sans_max": lambda: f"{t['tokens_sortie']} tokens",
                                                     "outils": lambda: f"{t['taux_invalides']:.1%}"}[cle_]())

    # Phase 7
    tq = (qual or {}).get("taux_reussite")
    ajout("Qualité embarquée", f"tickets internes résolus ≥ {slo['qualite_min']:.0%}", "7",
          None if tq is None else tq >= slo["qualite_min"],
          "—" if tq is None else f"{tq:.0%} sur {len(qual['tickets'])} essais")
    return L


def lancer(args):
    cfg = charger_config(args.profil)
    L = lignes(cfg)
    ok = sum(1 for l in L if l[3] == "✅")
    ko = sum(1 for l in L if l[3] == "❌")
    att = sum(1 for l in L if l[3] == "⏳")
    verdict = ("GO" if ko == 0 and att == 0 else "NO-GO" if ko else "EN COURS")
    md = [f"# Go / no-go — profil {cfg['nom_profil']} — {time.strftime('%Y-%m-%d %H:%M')}", "",
          f"**Verdict : {verdict}** — {ok} ✅, {ko} ❌, {att} ⏳ (non mesuré)", "",
          "| Contrôle | Critère | Phase | | Mesure |", "|---|---|---|---|---|"]
    md += [f"| {c} | {k} | {p} | {s} | {m} |" for c, k, p, s, m in L]
    texte = "\n".join(md) + "\n"
    f = dossier_resultats() / f"rapport-{time.strftime('%Y%m%d-%H%M%S')}.md"
    f.write_text(texte)
    print(texte)
    print(f"→ {f}")
