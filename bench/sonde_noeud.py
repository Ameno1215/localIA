#!/usr/bin/env python3
"""Sonde de nœud (phases 0 et 4) : fréquence SM, puissance, températures ACPI, mémoire, swap.

Écrit une ligne CSV toutes les --periode secondes sur la sortie standard. À lancer DEPUIS UNE
AUTRE MACHINE par ssh, pour que le journal survive à une extinction thermique du nœud :
  ssh spark1 python3 - --periode 10 < bench/sonde_noeud.py > bench/resultats/sonde-spark1.csv
Puis : python3 -m bench thermique bench/resultats/sonde-*.csv

--une-fois affiche un seul relevé en JSON (utilisé par reseau.sh).
Sur GB10, nvidia-smi peut lire la température 8 à 15 °C trop bas : les zones ACPI font foi.
"""
import argparse
import glob
import json
import socket
import subprocess
import sys
import time

CHAMPS = ["horodatage", "hote", "freq_sm_mhz", "puissance_w", "temp_gpu_c", "acpi_max_c",
          "mem_dispo_go", "swap_utilise_go", "charge1"]


def _nombre(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def releve():
    d = {"horodatage": round(time.time(), 1), "hote": socket.gethostname()}
    try:
        sortie = subprocess.run(["nvidia-smi", "--query-gpu=clocks.sm,power.draw,temperature.gpu",
                                 "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=10).stdout
        vals = [v.strip() for v in sortie.splitlines()[0].split(",")] if sortie.strip() else []
    except (OSError, subprocess.TimeoutExpired):
        vals = []
    d["freq_sm_mhz"], d["puissance_w"], d["temp_gpu_c"] = [_nombre(v) for v in (vals + [None] * 3)[:3]]
    temps = []
    for f in glob.glob("/sys/class/thermal/thermal_zone*/temp"):
        try:
            temps.append(int(open(f).read()) / 1000)
        except (OSError, ValueError):
            pass
    d["acpi_max_c"] = max(temps) if temps else None
    mem = {}
    try:
        for ligne in open("/proc/meminfo"):
            k, v = ligne.split(":")
            mem[k] = int(v.split()[0]) / 1024 / 1024
    except OSError:
        pass
    d["mem_dispo_go"] = round(mem["MemAvailable"], 2) if "MemAvailable" in mem else None
    d["swap_utilise_go"] = (round(mem["SwapTotal"] - mem["SwapFree"], 2)
                            if "SwapTotal" in mem and "SwapFree" in mem else None)
    try:
        d["charge1"] = float(open("/proc/loadavg").read().split()[0])
    except OSError:
        d["charge1"] = None
    return d


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--periode", type=float, default=10)
    p.add_argument("--une-fois", action="store_true")
    a = p.parse_args()
    if a.une_fois:
        print(json.dumps(releve()))
        return
    print(",".join(CHAMPS), flush=True)
    while True:
        d = releve()
        print(",".join("" if d[c] is None else str(d[c]) for c in CHAMPS), flush=True)
        try:
            time.sleep(a.periode)
        except KeyboardInterrupt:
            sys.exit(0)


if __name__ == "__main__":
    main()
