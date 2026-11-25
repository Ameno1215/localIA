#!/usr/bin/env bash
# Phase 0 — référence nœuds et réseau (grappe Spark). À lancer depuis une machine qui joint
# tous les nœuds en ssh sans mot de passe.
#
#   NOEUDS="spark1 spark2 spark3 spark4" IB_DEV=rocep1s0f1 IFACE=enp1s0f1np1 \
#   NCCL_TESTS=/opt/nccl-tests/build ./bench/reseau.sh
#
# Duo (SPARK-DUO.md) : les IP du câble, et le compte de chaque Spark dans ~/.ssh/config (Host 192.168.100.11 /
# User admin_spark19e5), pour que ssh, ib_write_bw et mpirun passent tous par le câble :
#   NOEUDS="192.168.100.10 192.168.100.11" IB_DEV=rocep1s0f0 IFACE=enp1s0f0np0 \
#   NCCL_HCA=rocep1s0f0,roceP2p1s0f0 ./bench/reseau.sh
#
# Mesure : relevé thermique/fréquence de chaque nœud, bande passante RoCE de chaque paire
# (ib_write_bw, paquet perftest), bande passante bus NCCL all_reduce sur 2 puis tous les nœuds
# (3 essais chacun, écart < 5 % attendu), et compteurs drop/discard/pause avant/après.
# Résultat : bench/resultats/reseau-<date>.json, lu par « python3 -m bench rapport ».
set -euo pipefail
cd "$(dirname "$0")/.."

NOEUDS=(${NOEUDS:-spark1 spark2 spark3 spark4})
IB_DEV=${IB_DEV:?IB_DEV requis (ex. rocep1s0f1, voir ibv_devices)}
IFACE=${IFACE:?IFACE requis (interface RoCE, ex. enp1s0f1np1)}
NCCL_TESTS=${NCCL_TESTS:-/opt/nccl-tests/build}
ESSAIS=${ESSAIS:-3}
NCCL_HCA=${NCCL_HCA:-$IB_DEV}   # cartes RDMA utilisées par NCCL (les deux moitiés du port en duo)
SORTIE=${BENCH_RESULTATS:-bench/resultats}
mkdir -p "$SORTIE"
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

compteurs() {  # somme des compteurs drop/discard/pause de l'interface RoCE d'un nœud
  ssh "$1" "ethtool -S $IFACE" 2>/dev/null | grep -Ei 'drop|discard|pause' \
    | awk -F: '{s+=$2} END {print s+0}'
}

echo "== Relevé des nœuds"
for n in "${NOEUDS[@]}"; do
  ssh "$n" python3 - --une-fois < bench/sonde_noeud.py > "$TMP/sonde-$n.json" || echo '{}' > "$TMP/sonde-$n.json"
  echo "  $n : $(cat "$TMP/sonde-$n.json")"
  compteurs "$n" > "$TMP/avant-$n"
done

echo "== Bande passante RoCE par paire (ib_write_bw)"
: > "$TMP/paires"
for ((i = 0; i < ${#NOEUDS[@]}; i++)); do
  for ((j = i + 1; j < ${#NOEUDS[@]}; j++)); do
    a=${NOEUDS[$i]} b=${NOEUDS[$j]}
    ssh "$a" "ib_write_bw -d $IB_DEV --report_gbits" > /dev/null 2>&1 &
    serveur=$!
    sleep 2
    bw=$(ssh "$b" "ib_write_bw -d $IB_DEV --report_gbits $a" 2>/dev/null \
      | awk '$1 ~ /^[0-9]+$/ && NF >= 4 {v=$4} END {print v+0}')
    wait $serveur || true
    echo "  $a ↔ $b : ${bw} Gb/s"
    echo "$a $b $bw" >> "$TMP/paires"
  done
done

echo "== NCCL all_reduce_perf"
: > "$TMP/nccl"
for nb in $(printf '%s\n' 2 ${#NOEUDS[@]} | sort -un); do   # 2 nœuds, puis tous (une fois en duo)
  hotes=$(IFS=,; echo "${NOEUDS[*]:0:$nb}")
  for ((e = 1; e <= ESSAIS; e++)); do
    bus=$(mpirun -np "$nb" -H "$hotes" \
      -x NCCL_IB_HCA="$NCCL_HCA" -x NCCL_SOCKET_IFNAME="$IFACE" -x NCCL_IB_GID_INDEX="${NCCL_GID_INDEX:-3}" \
      "$NCCL_TESTS/all_reduce_perf" -b 8 -e 4G -f 2 -g 1 2>&1 \
      | awk -F: '/Avg bus bandwidth/ {gsub(/ /, "", $2); print $2}')
    echo "  $nb nœuds, essai $e : ${bus:-échec} Go/s"
    echo "$nb $e ${bus:-0}" >> "$TMP/nccl"
  done
done

echo "== Compteurs drop/discard/pause après les essais"
for n in "${NOEUDS[@]}"; do
  compteurs "$n" > "$TMP/apres-$n"
  echo "  $n : +$(( $(cat "$TMP/apres-$n") - $(cat "$TMP/avant-$n") ))"
done

python3 - "$TMP" "$SORTIE" "${NOEUDS[@]}" <<'EOF'
import json, sys, time
from pathlib import Path
tmp, sortie, noeuds = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3:]
paires = [dict(zip(("a", "b", "gbits"), l.split())) for l in (tmp / "paires").read_text().split("\n") if l]
for p in paires: p["gbits"] = float(p["gbits"])
nccl = {}
for l in (tmp / "nccl").read_text().split("\n"):
    if l:
        nb, _, bus = l.split()
        nccl.setdefault(nb, []).append(float(bus))
ecarts = {nb: (max(v) - min(v)) / max(v) if max(v) else 1.0 for nb, v in nccl.items()}
pertes = {n: int((tmp / f"apres-{n}").read_text()) - int((tmp / f"avant-{n}").read_text()) for n in noeuds}
sondes = {n: json.loads((tmp / f"sonde-{n}.json").read_text() or "{}") for n in noeuds}
r = {"phase": "reseau", "date": time.strftime("%Y-%m-%d %H:%M:%S"), "paires": paires, "nccl_bus_go_s": nccl,
     "ecart_max": round(max(ecarts.values()), 4) if ecarts else None, "pertes": pertes, "sondes": sondes}
r["reussi"] = bool(nccl) and all(min(v) > 0 for v in nccl.values()) and r["ecart_max"] < 0.05 \
    and not any(pertes.values()) and all(p["gbits"] > 0 for p in paires)
f = sortie / f"reseau-{time.strftime('%Y%m%d-%H%M%S')}.json"
f.write_text(json.dumps(r, ensure_ascii=False, indent=2))
print(f"\n{'✅' if r['reussi'] else '❌'} écart NCCL max {r['ecart_max']}, pertes {pertes}\n→ Résultats : {f}")
EOF
