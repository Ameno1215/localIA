"""Exporteur Prometheus minimal pour le GPU, via nvidia-smi (bibliothèque standard uniquement).

DCGM exporter ne gère pas forcément le GB10 (DGX Spark) : nvidia-smi, injecté par le NVIDIA Container
Toolkit (capacité « utility »), suffit pour l'utilisation, la mémoire, la puissance, la température et les
fréquences. Sur le GB10, la mémoire est unifiée : nvidia-smi peut répondre [N/A], la valeur est alors omise
(la mémoire du système est suivie par node-exporter).

  GET /metrics   sur le port EXPORTER_PORT (9835 par défaut)
"""

import os
import subprocess
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get("EXPORTER_PORT", "9835"))
# champ nvidia-smi -> (métrique, aide, facteur)
CHAMPS = {
    "utilization.gpu": ("gpu_utilisation_pourcent", "Utilisation du GPU (%)", 1),
    "utilization.memory": ("gpu_utilisation_memoire_pourcent", "Activité du contrôleur mémoire (%)", 1),
    "memory.used": ("gpu_memoire_utilisee_octets", "Mémoire GPU utilisée", 1024 * 1024),
    "memory.total": ("gpu_memoire_totale_octets", "Mémoire GPU totale", 1024 * 1024),
    "power.draw": ("gpu_puissance_watts", "Puissance consommée (W)", 1),
    "temperature.gpu": ("gpu_temperature_celsius", "Température du GPU (°C)", 1),
    "clocks.sm": ("gpu_frequence_sm_mhz", "Fréquence SM (MHz)", 1),
}


def nombre(texte: str) -> float | None:
    texte = texte.strip()
    try:
        return float(int(texte, 16)) if texte.startswith("0x") else float(texte)
    except ValueError:
        return None  # [N/A], [Not Supported]


def mesurer() -> str:
    champs = ["index", "name", *CHAMPS]
    r = subprocess.run(["nvidia-smi", f"--query-gpu={','.join(champs)}", "--format=csv,noheader,nounits"],
                       capture_output=True, text=True, timeout=10)
    lignes = []
    if r.returncode != 0:
        return "# nvidia-smi a échoué\ngpu_exporteur_ok 0\n"
    valeurs = [[v.strip() for v in l.split(",")] for l in r.stdout.strip().splitlines()]
    for champ, (metrique, aide, facteur) in CHAMPS.items():
        lignes += [f"# HELP {metrique} {aide}", f"# TYPE {metrique} gauge"]
        i = champs.index(champ)
        for v in valeurs:
            x = nombre(v[i])
            if x is not None:
                lignes.append(f'{metrique}{{gpu="{v[0]}",nom="{v[1]}"}} {x * facteur:g}')
    lignes.append("gpu_exporteur_ok 1")
    return "\n".join(lignes) + "\n"


class Gestionnaire(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path != "/metrics":
            self.send_response(404)
            self.end_headers()
            return
        try:
            corps = mesurer().encode()
        except Exception as e:  # nvidia-smi absent, délai dépassé…
            corps = f"# erreur : {e}\ngpu_exporteur_ok 0\n".encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; version=0.0.4")
        self.end_headers()
        self.wfile.write(corps)

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", PORT), Gestionnaire).serve_forever()
