# Deux DGX Spark reliés par le câble (duo)

> ✅ **Validé le 09/11/2026 sur les deux Spark** (Spark 1 `spark-4c7c`, Spark 2 `spark-19e5`), sans serveur annexe :
> - Ray voit 2 GPU, NCCL passe par le câble en RDMA (`NET/IB : Using [0]rocep1s0f0`) ;
> - **Nemotron 3.5 Super 120B** : les 5 tests de `02-verifier-spark.sh` OK, 32 tok/s pour une requête, 34 Gio de cache KV par Spark ;
> - **Qwen3.8-Flash-Next NVFP4** (profil `qwen3.8-flash-next-nvfp4-duo`) : démarre, 21,5 Gio de cache KV par Spark. Tests de qualité et de débit à finir.

Ce qui tourne où :

```
Postes ──HTTPS──> Spark 1 : Caddy, Open WebUI, LiteLLM, Postgres, Qdrant, MCP, TEI (embeddings + reranker)
                           + vLLM (tête Ray, moitié du modèle)
                              ║ câble QSFP, 192.168.100.10 ⇄ 192.168.100.11
                  Spark 2 : ray-worker (autre moitié du modèle), rien d'autre
```

Un serveur annexe pour décharger le Spark 1 reste possible plus tard (annexe en fin de document), mais il n'est pas nécessaire.

## 1. Pourquoi le duo

| | Un modèle sur les deux Spark (ce document) | Un modèle complet sur chaque Spark |
|---|---|---|
| Taille de modèle | jusqu'à ~150 Go de poids, les autres services restant sur le Spark 1 | ~85 Go max, comme aujourd'hui |
| Vitesse d'une requête | proche d'un seul Spark : chaque couche échange des données par le câble | celle d'un seul Spark |
| Requêtes en parallèle | comme un Spark, avec plus de place pour le contexte | **deux fois plus** |
| Câble | indispensable | inutile |
| Mise en place | Ray, NCCL, réseau : ce document | décommenter la 2e entrée `qwen-local` de `config/litellm.yaml` |

Le duo sert à **faire tenir un modèle plus capable**, pas à aller plus vite.

## 2. Deux dossiers : le duo à côté de l'installation « 1 Spark »

On ne change pas de branche dans le dossier qui marche : on clone le dépôt une deuxième fois, sur la branche `spark-duo`. Les deux dossiers partagent les **mêmes données** (modèles, comptes, clés, index), mais **un seul tourne à la fois**.

| | Spark 1 | Spark 2 |
|---|---|---|
| Compte de service | `admin_spark4c7c` | `admin_spark19e5` (un autre Spark : `whoami`) |
| IP sur le réseau de l'équipe | `192.168.8.106` | `192.168.8.194` |
| **IP sur le câble** (celles du `.env`) | `192.168.100.10` et `192.168.101.10` | `192.168.100.11` et `192.168.101.11` |
| Dossier 1 Spark (on n'y touche pas) | `/home/admin_spark4c7c/data/ws/ia-locale` | (aucun) |
| Dossier duo | `/home/admin_spark4c7c/data/ws/ia-locale-duo` | `/home/admin_spark19e5/data/ws/ia-locale-duo` |
| Données (`WS_DIR`) | `/home/admin_spark4c7c/data/ws` (**le même** pour les deux dossiers) | `/home/admin_spark19e5/data/ws` |

**Pourquoi deux dossiers ne se gênent pas.** Docker Compose nomme le projet d'après le dossier : `ia-locale` et `ia-locale-duo` ont des conteneurs distincts, et un `docker compose down` dans l'un n'arrête pas l'autre. Mais ils publient les mêmes ports et écrivent dans les mêmes bases : **arrêter l'un avant de démarrer l'autre** (deux Postgres sur le même dossier l'abîmeraient). Les données sont dans `WS_DIR`, pas dans le dossier du projet : comptes Open WebUI, clés LiteLLM, index et certificat Caddy sont retrouvés tels quels.

**Ce qui diffère** : seulement `.env` et `.env.vllm` (non suivis par git).

| Réglage | Dossier 1 Spark | Dossier duo, Spark 1 | Dossier duo, Spark 2 |
|---|---|---|---|
| Branche | `spark` | `spark-duo` | `spark-duo` |
| `COMPOSE_FILE` | `docker-compose.yml:docker-compose.spark.yml` | `docker-compose.yml:docker-compose.spark.yml:docker-compose.spark-duo.yml` | `docker-compose.spark-worker.yml` |
| `WS_DIR` | `/home/admin_spark4c7c/data/ws` | le même (**chemin absolu**) | `/home/admin_spark19e5/data/ws` |
| `VLLM_IMAGE` | celle du profil | **une image avec Ray** : `eugr/spark-vllm:nightly-20261015` | la même |
| `VLLM_GPU_UTIL` | celle du profil (0.74) | **0.68** (section 8) | la même |
| `VLLM_API_KEY` | vide | une clé (`openssl rand -hex 16`) | la même |
| `DUO_*` | absents | remplis (section 3.5) | **identiques** au Spark 1 |
| Secrets (`LITELLM_MASTER_KEY`, `POSTGRES_PASSWORD`, `WEBUI_SECRET_KEY`, `MCP_TOKEN`), `SPARK_HOSTNAME` | les vôtres | **identiques** | sans effet |

⚠ **`vllm/vllm-openai` (l'image officielle) ne contient pas Ray** : le duo s'arrête alors sur « Ray absent de l'image » (ou `ray: command not found`). Garder `eugr/spark-vllm`, ou toute image qui fournit la commande `ray`.

## 3. Brancher et configurer le câble (les deux Spark)

### 3.1 Un câble suffit

Chaque Spark a une carte ConnectX-7 avec deux ports QSFP. La carte plafonne à ~200 Gb/s au total, quel que soit le nombre de câbles : **un seul câble**, branché sur le **même port des deux côtés** (les noms d'interface sont alors identiques, et un seul `.env` sert aux deux).

### 3.2 Repérer les interfaces (chaque Spark)

```bash
ibdev2netdev
```

Sortie sur nos Spark (câble sur le premier port) :

```
rocep1s0f0 port 1 ==> enp1s0f0np0 (Up)
rocep1s0f1 port 1 ==> enp1s0f1np1 (Down)
roceP2p1s0f0 port 1 ==> enP2p1s0f0np0 (Up)
roceP2p1s0f1 port 1 ==> enP2p1s0f1np1 (Down)
```

Le port branché apparaît **deux fois** en `(Up)` : la carte est reliée au processeur par deux liens PCIe, chacun avec son interface. À gauche la carte RDMA (`DUO_HCA`), à droite l'interface réseau (`DUO_INTERFACE`, netplan). Aucune ligne `(Up)` : câble mal enfoncé ou mauvais type (il faut un câble QSFP direct, 200 Gb/s).

### 3.3 Adresses fixes sur le câble (chaque Spark)

Deux petits réseaux privés, **séparés du réseau de l'équipe** (`192.168.8.x`). Le `/24` veut dire « les 254 adresses `192.168.100.1` à `.254` sont joignables directement par ce câble ».

Spark 1 :

```bash
sudo tee /etc/netplan/40-qsfp.yaml >/dev/null <<'NETPLAN'
network:
  version: 2
  ethernets:
    enp1s0f0np0:
      addresses: [192.168.100.10/24]
    enP2p1s0f0np0:
      addresses: [192.168.101.10/24]
NETPLAN
sudo chmod 600 /etc/netplan/40-qsfp.yaml && sudo netplan apply
```

Spark 2 : le même fichier avec `192.168.100.11/24` et `192.168.101.11/24`.

Vérifier sur chaque Spark : `ip -br addr | grep 192.168.10` montre les deux adresses, interfaces `UP`.

### 3.4 Tester le lien (depuis le Spark 1)

```bash
ping -c3 192.168.100.11
ping -c3 192.168.101.11
ssh admin_spark19e5@192.168.100.11 hostname     # servira aux copies
```

Facultatif, débit RDMA réel (paquet `perftest`) : `ib_write_bw -d rocep1s0f0 --report_gbits` sur le Spark 2, puis la même commande suivie de `192.168.100.11` sur le Spark 1. Attendu : 90 à 100 Gb/s pour une moitié.

### 3.5 Valeurs du `.env` (Spark 1, puis copiées sur le Spark 2)

```bash
DUO_INTERFACE=enp1s0f0np0                  # première interface (Up) : Ray, Gloo, démarrage de NCCL
DUO_HCA=rocep1s0f0,roceP2p1s0f0            # les deux cartes RDMA (Up) : NCCL répartit le trafic sur les deux
DUO_IP_TETE=192.168.100.10                 # ⚠ IP du CÂBLE, pas 192.168.8.106
DUO_IP_WORKER=192.168.100.11               # ⚠ IP du CÂBLE, pas 192.168.8.194
DUO_NCCL_DEBUG=INFO                        # premier test : montre si NCCL passe par le câble ; WARN ensuite
```

Avec l'IP du réseau de l'équipe dans `DUO_IP_TETE`, le Spark 1 affiche `Local node IP: 192.168.8.106` et attend le Spark 2 indéfiniment (le pare-feu bloque, et le trafic ne passerait pas par le câble).

NCCL utilise l'index GID 3 (RoCE v2 sur IPv4, la valeur du GB10). Vérification : `show_gids | grep 192.168.100` montre `v2` à l'index 3 ; sinon, mettre le bon index dans `DUO_GID_INDEX`.

## 4. Pare-feu (les deux Spark)

En duo, vLLM et Ray sont sur le réseau de l'hôte, et **Ray n'a aucune authentification** : quiconque atteint son port peut exécuter du code. On autorise tout sur le câble, et rien d'autre que le nécessaire depuis le réseau de l'équipe. Autoriser SSH **avant** d'activer, sinon on perd la session.

Les deux Spark :

```bash
sudo ufw allow ssh
sudo ufw allow in on enp1s0f0np0           # le câble, 1re moitié
sudo ufw allow in on enP2p1s0f0np0         # le câble, 2e moitié
```

Spark 1 seulement (LiteLLM et le MCP, dans Docker, joignent vLLM sur l'hôte) :

```bash
sudo ufw allow from 172.16.0.0/12 to any port 8000 proto tcp
```

Puis les deux Spark :

```bash
sudo ufw default deny incoming && sudo ufw enable
sudo ufw status
```

Open WebUI (8443) et l'API (443) restent joignables : Docker ouvre lui-même les ports qu'il publie. Le panneau web (port 7000) n'écoute que sur `127.0.0.1`, il n'est pas concerné. `VLLM_API_KEY` reste obligatoire : vLLM refuse toute requête `/v1` sans la clé (LiteLLM et l'indexation la reçoivent automatiquement).

## 5. Préparer les dossiers

### 5.1 Spark 1 : le dossier duo

L'installation actuelle peut continuer de tourner pendant cette étape.

```bash
cd /home/admin_spark4c7c/data/ws
git clone -b spark-duo https://github.com/Ameno1215/localIA.git ia-locale-duo
cd ia-locale-duo
cp ../ia-locale/.env ../ia-locale/.env.vllm .
chmod 600 .env
nano .env
```

Dans ce `.env`, régler (le bloc « Duo » est en bas de `.env.spark.example`) :

```bash
WS_DIR=/home/admin_spark4c7c/data/ws
COMPOSE_FILE=docker-compose.yml:docker-compose.spark.yml:docker-compose.spark-duo.yml
VLLM_IMAGE=eugr/spark-vllm:nightly-20261015
VLLM_GPU_UTIL=0.68
VLLM_SPEC_ARGS=                            # premier test sans décodage spéculatif ; à remettre une fois le duo validé
VLLM_API_KEY=<résultat de : openssl rand -hex 16>
# + le bloc DUO_* de la section 3.5
```

Vérifier : `docker compose config --services` liste `vllm`, `litellm`, `open-webui`, `caddy`…

### 5.2 Les droits sur `WS_DIR` (Spark 1)

`modeles.py telecharger` tourne sous le compte de service : `models/` doit lui appartenir, sinon le téléchargement échoue (`Permission denied: '/work/models/…'`). Ne donner au compte que les dossiers remplis à la main :

```bash
cd /home/admin_spark4c7c/data/ws
sudo chown -R admin_spark4c7c: models loras docs-internes
```

**Jamais** `chown -R` sur tout `WS_DIR` : `postgres/` doit rester à l'utilisateur 70 (sinon la base ne démarre plus) ; `cache`, `caddy`, `open-webui`, `qdrant` sont gérés par les conteneurs.

### 5.3 Spark 2 : le dossier duo

En tant que `admin_spark19e5` (membre du groupe `docker`) :

```bash
mkdir -p ~/data/ws/{models,loras,cache/vllm,cache/flashinfer,cache/triton}
cd ~/data/ws
git clone -b spark-duo https://github.com/Ameno1215/localIA.git ia-locale-duo
```

Ces dossiers sont créés à la main parce que le conteneur `ray-worker` les monte : sans eux, Docker les créerait au nom de root.

### 5.4 Copier configuration et modèles par le câble (depuis le Spark 1)

```bash
cd /home/admin_spark4c7c/data/ws/ia-locale-duo
scp .env .env.vllm admin_spark19e5@192.168.100.11:/home/admin_spark19e5/data/ws/ia-locale-duo/
rsync -a --info=progress2 /home/admin_spark4c7c/data/ws/models/ admin_spark19e5@192.168.100.11:/home/admin_spark19e5/data/ws/models/
```

Le dossier `models` entier est copié exprès : un profil peut utiliser plusieurs dossiers (modèle + brouillon de décodage spéculatif). Par le câble : Nemotron + brouillon (~80 Go) et Qwen3.8-Flash-Next (~133 Go) passent en quelques minutes chacun. Lancer le `rsync` **après** la fin des téléchargements.

### 5.5 Spark 2 : rétablir ses deux lignes

Le `.env` copié porte les valeurs du Spark 1 : **à refaire après chaque copie**.

```bash
cd ~/data/ws/ia-locale-duo
sed -i 's|^COMPOSE_FILE=.*|COMPOSE_FILE=docker-compose.spark-worker.yml|; s|^WS_DIR=.*|WS_DIR=/home/admin_spark19e5/data/ws|' .env
grep -E "^(COMPOSE_FILE|WS_DIR|VLLM_IMAGE|VLLM_MODEL|DUO_IP)" .env
docker compose config --services           # doit afficher seulement : ray-worker
docker compose pull
```

Seuls comptent, sur le Spark 2 : le **nom du dossier du modèle** (identique grâce au `rsync`) et `WS_DIR`. Dans les conteneurs, les modèles sont toujours vus sous `/models/…`.

## 6. Démarrer (bascule)

Le service est coupé pendant le chargement du modèle (2 à 4 minutes).

```bash
# Spark 1 : arrêter l'installation 1 Spark (et le panneau web s'il tourne : Ctrl+C dans son terminal)
cd /home/admin_spark4c7c/data/ws/ia-locale && docker compose down

# Spark 2
cd ~/data/ws/ia-locale-duo && docker compose up -d

# Spark 1
cd /home/admin_spark4c7c/data/ws/ia-locale-duo && docker compose up -d
docker compose logs -f vllm
```

Attendu dans les journaux, dans l'ordre :

1. `Local node IP: 192.168.100.10` (sinon : `DUO_IP_TETE`, section 3.5) ;
2. `Attente du Spark 2…` quelques secondes, puis `Cluster prêt : 2 GPU` ;
3. avec `DUO_NCCL_DEBUG=INFO`, des lignes `NET/IB : Using [0]rocep1s0f0` et `via NET/IB` : NCCL passe par le câble en RDMA. `NET/Socket` signifierait TCP : ça marche, mais bien plus lentement (vérifier `DUO_HCA`) ;
4. `Available KV cache memory: … GiB` (la place pour le contexte, par Spark) ;
5. `Application startup complete` : **c'est prêt**. Ctrl+C ne quitte que les journaux.

Si une `Traceback` est suivie de nouveau de `Attente du Spark 2…`, vLLM a planté et redémarre : voir la section 9.

Vérifications (Spark 1, dossier duo) :

```bash
docker compose exec vllm ray status        # 2 nœuds, 2.0 GPU
./scripts/02-verifier-spark.sh             # 5 lignes [OK] : chat, outil, débit, embeddings, reranker
docker compose logs vllm | grep -E "KV cache size|Maximum concurrency"
```

Puis une question dans Open WebUI (`https://<SPARK_HOSTNAME>:8443`). `nvidia-smi` sur le Spark 2 pendant la réponse : le GPU travaille.

Une fois validé, remettre `DUO_NCCL_DEBUG=WARN` (journaux plus courts) sur les deux Spark.

## 7. Changer de modèle

Exemple avec Qwen3.8-Flash-Next (déjà téléchargé et copié sur le Spark 2, section 5.4).

Spark 1 :

```bash
cd /home/admin_spark4c7c/data/ws/ia-locale-duo
git pull
./scripts/modeles.py utiliser qwen3.8-flash-next-nvfp4-duo --sans-redemarrer
grep -E "^(VLLM_MODEL|VLLM_IMAGE|VLLM_GPU_UTIL)=" .env    # image avec Ray, 0.68
scp .env .env.vllm admin_spark19e5@192.168.100.11:/home/admin_spark19e5/data/ws/ia-locale-duo/
```

Spark 2 : rétablir ses deux lignes (section 5.5), puis :

```bash
docker compose pull && docker compose up -d --force-recreate
```

Spark 1 :

```bash
docker compose pull vllm && docker compose up -d --force-recreate vllm
docker compose logs -f vllm
```

**Un nouveau modèle** : `./scripts/modeles.py telecharger <profil>` sur le Spark 1, puis le `rsync` de la section 5.4. Pour écrire un profil duo, partir de `config/modeles-spark/qwen3.8-flash-next-nvfp4-duo.env` : image avec Ray, `VLLM_GPU_UTIL=0.68`. Le passage sur deux GPU (`--tensor-parallel-size 2`) est ajouté par `scripts/spark-duo/tete.sh`, pas par le profil.

## 8. Mémoire et fenêtre de contexte

- **Chaque Spark porte la moitié des poids et la moitié du cache KV** (le contexte). Chaque couche est coupée en deux : pour chaque jeton, une moitié de son cache est sur le Spark 1 et l'autre sur le Spark 2. Le contexte ne va pas « là où il y a de la place ».
- **`VLLM_GPU_UTIL` est un budget par Spark, le même pour les deux.** Au démarrage, vLLM refuse de partir si ce budget dépasse la mémoire libre d'un des deux Spark (`Free memory on device … is less than desired GPU memory utilization`).
- **Le Spark 1 décide** : il porte aussi Open WebUI, le RAG, Postgres et la tête Ray. Mesuré : 86 à 89 Gio libres sur 121,7, contre 110 sur le Spark 2. D'où **0.68** (≈ 83 Gio), au lieu de 0.74 en mode 1 Spark. Chaque Spark ne portant que la moitié des poids, c'est largement suffisant.

Mesures du 09/11/2026, à 0.68, par Spark :

| Modèle | Poids + mémoire hors PyTorch | Cache KV (contexte) |
|---|---|---|
| Nemotron 3.5 Super 120B NVFP4 | 44 Gio | 34 Gio |
| Qwen3.8-Flash-Next NVFP4 | 59 Gio | 21,5 Gio |

Pour gagner de la place : déplacer TEI (embeddings + reranker) et Qdrant sur le Spark 2 (la mémoire libre s'équilibre, et `VLLM_GPU_UTIL` peut monter sur les deux), ou passer le cache KV en fp8 (`--kv-cache-dtype fp8`, environ deux fois plus de contexte, à tester sur la qualité).

## 9. Dépannage

| Symptôme | Cause et solution |
|---|---|
| `Attente du Spark 2…` sans fin, et `Local node IP: 192.168.8.x` | `DUO_IP_TETE` / `DUO_IP_WORKER` sur les IP du réseau de l'équipe : mettre celles du câble (3.5) dans le `.env` **des deux** Spark, puis relancer (Spark 2 d'abord) |
| `Attente du Spark 2…` sans fin, IP du câble correcte | Sur le Spark 2 : `docker compose logs --tail 50 ray-worker`. Vérifier `COMPOSE_FILE` et `WS_DIR` (5.5), le pare-feu (section 4), `ping 192.168.100.10` |
| `Ray absent de l'image` ou `ray: command not found` | Image sans Ray (`vllm/vllm-openai`) : `VLLM_IMAGE=eugr/spark-vllm:nightly-20261015` sur les deux Spark |
| `Free memory on device cuda:0 (… GiB) on startup is less than desired GPU memory utilization` | D'abord vider le cache disque (laissé par un gros téléchargement ou un `rsync`) sur les deux Spark : `sudo sh -c 'sync; echo 3 > /proc/sys/vm/drop_caches'`. Si le chiffre ne bouge pas : baisser `VLLM_GPU_UTIL` sous « Gio libres ÷ 121,7 » (0.68 pour 86 Gio) sur les deux Spark |
| `NET/Socket` au lieu de `NET/IB` dans les journaux | `DUO_HCA` faux ou `/dev/infiniband` absent : `ibdev2netdev` (3.2) |
| `modeles.py telecharger` : `Permission denied: '/work/models/…'` | `models/` appartient à root : section 5.2 |
| `-bash: c2: No such file or directory` (ou tout `<…>`) | Un exemple de commande a été collé sans remplacer `<…>` |

## 10. Revenir à l'installation 1 Spark

À tout moment, rien n'a été modifié dans l'ancien dossier :

```bash
# Spark 1
cd /home/admin_spark4c7c/data/ws/ia-locale-duo && docker compose down
# Spark 2
cd ~/data/ws/ia-locale-duo && docker compose down
# Spark 1
cd /home/admin_spark4c7c/data/ws/ia-locale && docker compose up -d
```

Pendant le duo, lancer **toutes** les commandes (`docker compose …`, `./scripts/…`, le panneau web) depuis le dossier duo : lancées depuis l'ancien dossier, elles agiraient sur l'installation 1 Spark.

---

## Annexe : serveur annexe (mis de côté, non testé)

Idée initiale, mise en attente : sortir des Spark tout ce qui n'est pas le modèle (LiteLLM, Open WebUI, embeddings, bases, MCP, Caddy) vers une machine x86 ordinaire, pour rendre leur mémoire au modèle. **Rien de cette annexe n'a tourné.**

Ce que chaque service occupe sur le Spark 1 (ordre de grandeur, à confirmer avec `docker stats` et `free -g`) :

| Service | Mémoire | Peut partir ? |
|---|---|---|
| Embeddings + reranker (TEI) | ~10 Go réservés + 2–4 Go de processus | Oui : sur le serveur, TEI sur CPU |
| Open WebUI | 1–2 Go | Oui |
| LiteLLM + Postgres | 1–2 Go | Oui |
| Qdrant, MCP docs, Caddy | < 1 Go chacun | Oui |
| Bureau graphique d'Ubuntu (DGX OS) | 1–3 Go | Le désactiver : `sudo systemctl set-default multi-user.target` puis redémarrer |
| vLLM | le reste | Non : c'est le modèle |

Gain attendu : 15 à 20 Go rendus au modèle sur le Spark 1.

**Le serveur :** une machine x86 Linux avec Docker, sans GPU, 16 Go de RAM et 4 cœurs (les embeddings sur CPU ralentissent l'indexation, pas la recherche).

Mise en place :
1. **Sur le serveur :** cloner le projet, `./scripts/preparer-env-spark.sh <nom-du-serveur> <dossier-de-données>`, puis dans son `.env` :
   ```bash
   COMPOSE_FILE=docker-compose.yml:docker-compose.serveur.yml
   VLLM_API_BASE=http://spark1.lan:8000/v1   # le Spark 1 (seul ou tête du duo)
   VLLM_API_KEY=<la même clé que sur le Spark 1>
   ```
2. **Copier les embeddings** (`bge-m3`, `bge-reranker-v2-m3`) depuis `WS_DIR/models` du Spark 1.
3. **Reprendre les données** : arrêter la pile du Spark 1, copier `postgres/`, `open-webui/`, `qdrant/` et `docs-internes/` de son `WS_DIR` vers celui du serveur, puis relancer l'indexation.
4. **Démarrer le serveur :** `docker compose up -d` (le service `vllm` n'y est jamais lancé).
5. **Sur le Spark 1 :** `VLLM_ECOUTE=0.0.0.0` dans le `.env` (inutile en duo), puis ne démarrer que le modèle : `docker compose down && docker compose up -d vllm`. Monter `VLLM_GPU_UTIL` par paliers. Pare-feu : `sudo ufw allow from <ip-du-serveur> to any port 8000 proto tcp`.
6. **Postes :** ils visent désormais le serveur ; distribuer le nouveau certificat de Caddy (section 8 du GUIDE) et relancer `installer-opencode.sh --serveur https://<serveur> …`.

```
Postes ──HTTPS──> Serveur annexe : Caddy, LiteLLM, Postgres, Open WebUI, Qdrant, MCP, TEI (CPU)
                        │  http://spark1:8000 + VLLM_API_KEY
                        ▼
                  Spark 1 : vLLM (tête Ray) ══ câble QSFP ══ Spark 2 : Ray worker
```
