"""Outils partagés par toutes les phases : configuration, client HTTP en streaming,
statistiques, lecture des métriques Prometheus, prompts synthétiques, sauvegarde des résultats.

Uniquement la bibliothèque standard : la suite tourne sur une machine hors ligne,
et dans le conteneur mcp-docs (Python 3.12) pour la phase RAG.
"""
from __future__ import annotations

import http.client
import json
import os
import random
import re
import threading
import time
import tomllib
import urllib.parse
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from pathlib import Path

RACINE = Path(__file__).resolve().parent
DEPOT = RACINE.parent


# --------------------------------------------------------------------------- configuration

def _fusion(base: dict, dessus: dict) -> dict:
    out = dict(base)
    for k, v in dessus.items():
        out[k] = _fusion(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def charger_config(profil: str | None = None) -> dict:
    chemin = Path(os.environ.get("BENCH_CONFIG") or RACINE / "config.toml")
    with open(chemin, "rb") as f:
        brut = tomllib.load(f)
    nom = profil or os.environ.get("BENCH_PROFIL") or brut.get("profil_defaut", "pc-test")
    if nom not in brut.get("profils", {}):
        raise SystemExit(f"Profil inconnu : {nom} (profils : {', '.join(brut.get('profils', {}))})")
    p = dict(brut["profils"][nom])
    p["slo"] = _fusion(brut.get("slo", {}), p.get("slo", {}))
    p["nom_profil"] = nom
    return p


def lire_env_fichier(chemin: Path = DEPOT / ".env") -> dict:
    valeurs = {}
    if chemin.is_file():
        for ligne in chemin.read_text().splitlines():
            ligne = ligne.strip()
            if ligne and not ligne.startswith("#") and "=" in ligne:
                k, v = ligne.split("=", 1)
                valeurs[k.strip()] = v.strip().strip('"').strip("'")
    return valeurs


def cle_maitre() -> str:
    """Clé utilisée par défaut : BENCH_CLE, sinon LITELLM_MASTER_KEY (environnement puis .env)."""
    return (os.environ.get("BENCH_CLE") or os.environ.get("LITELLM_MASTER_KEY")
            or lire_env_fichier().get("LITELLM_MASTER_KEY") or "")


def role(cfg: dict, nom: str) -> dict:
    try:
        return cfg["roles"][nom]
    except KeyError:
        raise SystemExit(f"Rôle « {nom} » absent du profil {cfg['nom_profil']} "
                         f"(rôles : {', '.join(cfg.get('roles', {}))})") from None


# --------------------------------------------------------------------------- HTTP

def _connexion(url: str, timeout: float):
    u = urllib.parse.urlsplit(url)
    cls = http.client.HTTPSConnection if u.scheme == "https" else http.client.HTTPConnection
    return cls(u.hostname, u.port, timeout=timeout), u


def requete(methode: str, url: str, corps=None, cle: str | None = None, timeout: float = 60):
    """Requête simple. Rend (statut, données JSON ou texte, en-têtes en minuscules). Statut 0 = injoignable."""
    conn, u = _connexion(url, timeout)
    chemin = (u.path or "/") + (f"?{u.query}" if u.query else "")
    entetes = {"Content-Type": "application/json"}
    if cle:
        entetes["Authorization"] = f"Bearer {cle}"
    try:
        conn.request(methode, chemin, body=None if corps is None else json.dumps(corps), headers=entetes)
        r = conn.getresponse()
        brut = r.read().decode("utf-8", "replace")
        try:
            donnees = json.loads(brut)
        except ValueError:
            donnees = brut
        return r.status, donnees, {k.lower(): v for k, v in r.getheaders()}
    except (OSError, http.client.HTTPException) as e:
        return 0, f"{type(e).__name__}: {e}", {}
    finally:
        conn.close()


@dataclass
class Mesure:
    """Une requête de chat mesurée côté client."""
    modele: str
    debut: float                     # horodatage Unix
    statut: int = 0
    ttft: float | None = None        # secondes jusqu'au premier token (texte, raisonnement ou outil)
    duree: float = 0.0               # secondes jusqu'à la fin du flux
    tokens_entree: int = 0
    tokens_sortie: int = 0
    itl: list = field(default_factory=list)   # écarts entre morceaux successifs (s)
    erreur: str = ""
    surcout_ms: float | None = None  # en-tête x-litellm-overhead-duration-ms
    fin: str | None = None           # finish_reason
    modele_repondu: str = ""
    outils: list = field(default_factory=list)  # [{"nom", "arguments"}]
    texte: str = ""
    info: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.statut == 200 and not self.erreur

    @property
    def tpot(self) -> float | None:
        """Secondes par token de sortie après le premier."""
        if self.ttft is None or self.tokens_sortie < 2 or self.duree <= self.ttft:
            return None
        return (self.duree - self.ttft) / (self.tokens_sortie - 1)

    def resume(self) -> dict:
        d = asdict(self)
        d.pop("itl")
        d.pop("texte")
        d["tpot"] = self.tpot
        return d


def chat(base: str, cle: str, modele: str, messages: list, max_tokens: int | None = 256,
         timeout: float = 600, outils: list | None = None, extra: dict | None = None,
         garder_texte: bool = False) -> Mesure:
    """Envoie une requête /v1/chat/completions en streaming et mesure TTFT, ITL et débit."""
    corps = {"model": modele, "messages": messages, "stream": True,
             "stream_options": {"include_usage": True}}
    if max_tokens is not None:
        corps["max_tokens"] = max_tokens
    if outils:
        corps["tools"] = outils
    corps.update(extra or {})
    m = Mesure(modele=modele, debut=time.time())
    t0 = time.perf_counter()
    conn, u = _connexion(base, timeout)
    entetes = {"Content-Type": "application/json", "Accept": "text/event-stream"}
    if cle:
        entetes["Authorization"] = f"Bearer {cle}"
    morceaux_texte, appels = [], {}
    try:
        conn.request("POST", u.path.rstrip("/") + "/v1/chat/completions",
                     body=json.dumps(corps), headers=entetes)
        r = conn.getresponse()
        m.statut = r.status
        h = {k.lower(): v for k, v in r.getheaders()}
        if "x-litellm-overhead-duration-ms" in h:
            try:
                m.surcout_ms = float(h["x-litellm-overhead-duration-ms"])
            except ValueError:
                pass
        if r.status != 200:
            m.erreur = r.read(4000).decode("utf-8", "replace")[:500] or f"HTTP {r.status}"
            return m
        dernier, nb = None, 0
        while True:
            ligne = r.readline()
            if not ligne:
                break
            ligne = ligne.strip()
            if not ligne.startswith(b"data:"):
                continue
            data = ligne[5:].strip()
            if data == b"[DONE]":
                break
            evt = json.loads(data)
            if evt.get("error"):
                m.erreur = json.dumps(evt["error"], ensure_ascii=False)[:500]
                break
            if evt.get("model") and not m.modele_repondu:
                m.modele_repondu = evt["model"]
            if evt.get("usage"):
                m.tokens_entree = evt["usage"].get("prompt_tokens") or 0
                m.tokens_sortie = evt["usage"].get("completion_tokens") or 0
            for ch in evt.get("choices") or []:
                d = ch.get("delta") or {}
                morceau = (d.get("content") or "") + (d.get("reasoning_content") or d.get("reasoning") or "")
                tc = d.get("tool_calls") or []
                if morceau or tc:
                    t = time.perf_counter() - t0
                    if m.ttft is None:
                        m.ttft = t
                    elif dernier is not None:
                        m.itl.append(t - dernier)
                    dernier = t
                    nb += 1
                    if garder_texte and morceau:
                        morceaux_texte.append(morceau)
                for c in tc:
                    a = appels.setdefault(c.get("index", 0), {"nom": "", "arguments": ""})
                    f = c.get("function") or {}
                    a["nom"] += f.get("name") or ""
                    a["arguments"] += f.get("arguments") or ""
                if ch.get("finish_reason"):
                    m.fin = ch["finish_reason"]
        if not m.tokens_sortie:
            m.tokens_sortie = nb
        if m.ttft is None and not m.erreur and m.tokens_sortie == 0:
            m.erreur = "flux terminé sans aucun token"
    except (OSError, http.client.HTTPException, ValueError) as e:
        m.erreur = f"{type(e).__name__}: {e}"
    finally:
        conn.close()
        m.duree = time.perf_counter() - t0
        m.texte = "".join(morceaux_texte)
        m.outils = [appels[k] for k in sorted(appels)]
    return m


def contexte_max(base: str, modele: str, cle: str = "") -> int | None:
    """max_model_len annoncé par vLLM sur /v1/models (None si inconnu)."""
    statut, donnees, _ = requete("GET", base.rstrip("/") + "/v1/models", cle=cle, timeout=10)
    if statut != 200 or not isinstance(donnees, dict):
        return None
    for d in donnees.get("data", []):
        if d.get("id") == modele and d.get("max_model_len"):
            return int(d["max_model_len"])
    return None


# --------------------------------------------------------------------------- statistiques

def centile(valeurs, q: float):
    v = sorted(x for x in valeurs if x is not None)
    if not v:
        return None
    k = (len(v) - 1) * q / 100
    f = int(k)
    c = min(f + 1, len(v) - 1)
    return v[f] + (v[c] - v[f]) * (k - f)


def _arrondi(x, n=3):
    return None if x is None else round(x, n)


def resumer(mesures: list[Mesure], duree_s: float | None = None) -> dict:
    """Agrège des mesures : centiles de TTFT, TPOT, ITL, erreurs par statut, débit."""
    ok = [m for m in mesures if m.ok]
    tpot_p95 = centile([m.tpot for m in ok], 95)
    tpot_p50 = centile([m.tpot for m in ok], 50)
    erreurs = Counter(str(m.statut) for m in mesures if not m.ok)
    exemples = list(dict.fromkeys(m.erreur[:160] for m in mesures if m.erreur))[:3]
    sortie = sum(m.tokens_sortie for m in ok)
    return {
        "requetes": len(mesures),
        "ok": len(ok),
        "taux_erreur": _arrondi((len(mesures) - len(ok)) / len(mesures), 4) if mesures else 0.0,
        "erreurs": dict(erreurs),
        "exemples_erreurs": exemples,
        "ttft_p50_s": _arrondi(centile([m.ttft for m in ok], 50)),
        "ttft_p95_s": _arrondi(centile([m.ttft for m in ok], 95)),
        "tpot_p95_ms": _arrondi(tpot_p95 and tpot_p95 * 1000, 1),
        "vitesse_p50": _arrondi(tpot_p50 and 1 / tpot_p50, 1),   # tok/s
        "vitesse_p95": _arrondi(tpot_p95 and 1 / tpot_p95, 1),   # tok/s de la requête au 95e centile (lente)
        "itl_p99_ms": _arrondi(centile([x for m in ok for x in m.itl], 99) and
                               centile([x for m in ok for x in m.itl], 99) * 1000, 1),
        "e2e_p95_s": _arrondi(centile([m.duree for m in ok], 95)),
        "tokens_entree_moy": round(sum(m.tokens_entree for m in ok) / len(ok)) if ok else None,
        "tokens_sortie": sortie,
        "debit_sortie": _arrondi(sortie / duree_s, 1) if duree_s else None,
        "surcout_p95_ms": _arrondi(centile([m.surcout_ms for m in mesures], 95), 1),
    }


# --------------------------------------------------------------------------- métriques Prometheus

_LIGNE = re.compile(r'^([a-zA-Z_:][a-zA-Z0-9_:]*)(\{[^}]*\})?\s+(\S+)')

# Noms selon les versions de vLLM ; la première trouvée est utilisée.
ALIAS = {
    "kv": ["vllm:kv_cache_usage_perc", "vllm:gpu_cache_usage_perc"],
    "attente": ["vllm:num_requests_waiting"],
    "en_cours": ["vllm:num_requests_running"],
    "preemptions": ["vllm:num_preemptions_total", "vllm:num_preemptions"],
    "cache_hits": ["vllm:prefix_cache_hits_total", "vllm:gpu_prefix_cache_hits_total"],
    "cache_requetes": ["vllm:prefix_cache_queries_total", "vllm:gpu_prefix_cache_queries_total"],
    "spec_acceptes": ["vllm:spec_decode_num_accepted_tokens_total"],
    "spec_proposes": ["vllm:spec_decode_num_draft_tokens_total"],
}
_MAX = {"kv"}   # jauges en pourcentage : on garde le max des séries, pas la somme


def lire_metriques(url: str, timeout: float = 5) -> dict | None:
    statut, texte, _ = requete("GET", url, timeout=timeout)
    if statut != 200 or not isinstance(texte, str):
        return None
    series = defaultdict(list)
    for ligne in texte.splitlines():
        if ligne.startswith("#"):
            continue
        mt = _LIGNE.match(ligne)
        if mt:
            try:
                series[mt.group(1)].append(float(mt.group(3)))
            except ValueError:
                pass
    out = {}
    for cle_, noms in ALIAS.items():
        for n in noms:
            if n in series:
                out[cle_] = max(series[n]) if cle_ in _MAX else sum(series[n])
                break
    return out


class Surveillance(threading.Thread):
    """Relève les métriques vLLM pendant un essai : maxima des jauges, deltas des compteurs."""

    def __init__(self, urls: list[str], periode: float = 2.0):
        super().__init__(daemon=True)
        self.urls, self.periode = list(urls or []), periode
        self._stop = threading.Event()
        self.premier, self.dernier = {}, {}
        self.kv_max, self.attente_max, self.en_cours_max = {}, {}, {}
        self.injoignables = set()

    def _relever(self):
        for url in self.urls:
            m = lire_metriques(url)
            if m is None:
                self.injoignables.add(url)
                continue
            self.premier.setdefault(url, m)
            self.dernier[url] = m
            for nom, d in (("kv", self.kv_max), ("attente", self.attente_max), ("en_cours", self.en_cours_max)):
                if nom in m:
                    d[url] = max(d.get(url, 0.0), m[nom])

    def run(self):
        while not self._stop.is_set():
            self._relever()
            self._stop.wait(self.periode)

    def arreter(self) -> dict:
        self._stop.set()
        self.join(timeout=10)
        self._relever()
        return self.bilan()

    def bilan(self) -> dict:
        def delta(nom):
            tot, vu = 0.0, False
            for url, d in self.dernier.items():
                if nom in d and nom in self.premier.get(url, {}):
                    tot += d[nom] - self.premier[url][nom]
                    vu = True
            return tot if vu else None

        hits, req = delta("cache_hits"), delta("cache_requetes")
        acc, prop = delta("spec_acceptes"), delta("spec_proposes")
        return {
            "kv_max": _arrondi(max(self.kv_max.values()), 3) if self.kv_max else None,
            "attente_max": max(self.attente_max.values()) if self.attente_max else None,
            "en_cours_max": max(self.en_cours_max.values()) if self.en_cours_max else None,
            "preemptions": delta("preemptions"),
            "taux_cache_prefixe": _arrondi(hits / req, 3) if hits is not None and req else None,
            "acceptation_speculative": _arrondi(acc / prop, 3) if acc is not None and prop else None,
            "par_serveur": {u: {"kv_max": self.kv_max.get(u), "attente_max": self.attente_max.get(u)}
                            for u in self.urls},
            "injoignables": sorted(self.injoignables - set(self.dernier)),
        }


# --------------------------------------------------------------------------- prompts synthétiques

_MOTS = """
registre horloge interruption tampon pilote broche périphérique trame octet bit masque drapeau
timer compteur valeur adresse mémoire flash ram pile tas tâche file sémaphore mutex délai
uart spi i2c can dma adc gpio pwm watchdog reset boot vecteur handler priorité niveau état
machine transition événement erreur code retour test assertion module fonction structure
champ pointeur taille longueur index boucle condition lecture écriture configuration init
the of and to in is for on with that this from by as be are it at or an was not
read write config init enable disable status control data buffer size length offset
static const volatile uint8_t uint16_t uint32_t int void return if else while for switch
case break struct typedef define include ifdef endif sizeof NULL true false
""".split()


def charger_calibration() -> float:
    f = dossier_resultats() / "calibration.json"
    try:
        return float(json.loads(f.read_text())["tokens_par_mot"])
    except (OSError, ValueError, KeyError):
        return 1.5


class Generateur:
    """Texte pseudo-technique d'une longueur visée en tokens (ratio mots→tokens calibrable)."""

    def __init__(self, graine=None, tokens_par_mot: float | None = None):
        self.rnd = random.Random(graine)
        self.ratio = tokens_par_mot or charger_calibration()

    def texte(self, n_tokens: int) -> str:
        n = max(1, int(n_tokens / self.ratio))
        mots = self.rnd.choices(_MOTS, k=n)
        lignes = [" ".join(mots[i:i + 16]) for i in range(0, n, 16)]
        return "\n".join(lignes)

    def unique(self) -> str:
        return f"[session {self.rnd.getrandbits(64):016x}]"


# --------------------------------------------------------------------------- résultats

def dossier_resultats() -> Path:
    d = Path(os.environ.get("BENCH_RESULTATS") or RACINE / "resultats")
    d.mkdir(parents=True, exist_ok=True)
    return d


def sauver(phase: str, cfg: dict, donnees: dict) -> Path:
    horodatage = time.strftime("%Y%m%d-%H%M%S")
    chemin = dossier_resultats() / f"{phase}-{horodatage}.json"
    n = 1
    while chemin.exists():
        n += 1
        chemin = dossier_resultats() / f"{phase}-{horodatage}-{n}.json"
    contenu = {"phase": phase, "profil": cfg.get("nom_profil"), "date": time.strftime("%Y-%m-%d %H:%M:%S"),
               **donnees}
    chemin.write_text(json.dumps(contenu, ensure_ascii=False, indent=2))
    print(f"\n→ Résultats : {chemin}")
    return chemin


def dernier(phase: str) -> dict | None:
    fichiers = sorted(dossier_resultats().glob(f"{phase}-*.json"), key=lambda p: p.stat().st_mtime)
    if not fichiers:
        return None
    return json.loads(fichiers[-1].read_text())


def afficher_palier(etiquette: str, r: dict):
    def f(x, suffixe=""):
        return "—" if x is None else f"{x}{suffixe}"
    print(f"  {etiquette:<22} req={r['requetes']:<4} err={r['taux_erreur']:.1%}  "
          f"TTFT p95={f(r['ttft_p95_s'], 's'):<8} vitesse p95={f(r['vitesse_p95'], ' tok/s'):<12} "
          f"ITL p99={f(r['itl_p99_ms'], 'ms'):<9} débit={f(r['debit_sortie'], ' tok/s')}", flush=True)
