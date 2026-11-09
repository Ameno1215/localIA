# Deux DGX Spark et un serveur annexe

> ⚠ **Rien de ce document n'a encore tourné sur les Spark.** Les réglages suivent le playbook NVIDIA « Connect two Sparks » et la documentation multi-nœud de vLLM (Ray). Vérifie chaque étape avant de passer à la suivante.

**Pour tester le duo sans casser l'installation actuelle à 1 Spark : section « Tester le duo sans toucher à l'installation 1 Spark », juste après le tableau ci-dessous.**

Deux évolutions indépendantes, combinables :

1. **Relier les deux Spark** par le câble QSFP pour servir **un seul modèle réparti sur les deux** (environ 2 × 110 Go utilisables).
2. **Sortir des Spark tout ce qui n'est pas le modèle** (LiteLLM, Open WebUI, embeddings, bases, MCP, Caddy) vers un serveur x86 ordinaire, pour rendre leur mémoire au modèle.

## 1. Choisir : un gros modèle, ou deux modèles

| | Un modèle sur les deux Spark (ce document) | Un modèle complet sur chaque Spark |
|---|---|---|
| Taille de modèle | jusqu'à ~200 Go de poids (ex. DeepSeek V4, 170 Go) | ~85 Go max, comme aujourd'hui |
| Vitesse d'une requête | au mieux celle d'un seul Spark, souvent un peu moins : chaque couche échange des données par le câble (200 Gb/s, soit ~10 fois moins que la mémoire) | celle d'un seul Spark |
| Requêtes en parallèle | comme un Spark (plus de place pour le cache) | **deux fois plus** |
| Câble | indispensable | inutile |
| Mise en place | Ray, NCCL, réseau : ce document | décommenter la 2e entrée `qwen-local` de `config/litellm.yaml` |

Le duo sert à **faire tenir un modèle plus capable**, pas à aller plus vite. Si le modèle actuel suffit et que l'équipe attend son tour, deux modèles indépendants derrière LiteLLM valent mieux.

## Tester le duo sans toucher à l'installation « 1 Spark » (deux dossiers)

Façon la plus sûre de tester : on ne change pas de branche dans le dossier qui marche. On clone le dépôt une deuxième fois, sur la branche `spark-duo`, dans un autre dossier. Les deux dossiers partagent les **mêmes données** (modèles, comptes, clés, index), mais **un seul tourne à la fois**.

Dans la suite, les chemins sont des exemples à adapter :

| | Spark 1 | Spark 2 |
|---|---|---|
| Dossier actuel (1 Spark, on n'y touche pas) | `/home/admin_spark4c7c/data/ws/ia-locale` | (aucun) |
| Nouveau dossier (duo) | `/home/admin_spark4c7c/data/ws/ia-locale-duo` | `/home/admin_spark4c7c/data/ws/ia-locale-duo` |
| Données (`WS_DIR`) | `/home/admin_spark4c7c/data/ws` (**le même** pour les deux dossiers) | `/home/admin_sparkXXXX/data/ws` (le sien : nom du compte de service du Spark 2) |
| IP sur le câble | `192.168.100.10` | `192.168.100.11` |

### Pourquoi deux dossiers ne se gênent pas (et la seule règle à respecter)

- Docker Compose nomme le projet d'après le **nom du dossier** : `ia-locale` et `ia-locale-duo` sont deux projets distincts, avec leurs propres conteneurs (`ia-locale-vllm-1`, `ia-locale-duo-vllm-1`…). Un `docker compose down` dans un dossier n'arrête **pas** l'autre.
- Les deux projets publient les mêmes ports (443, 8443, 3000, 4000, 8000…) et écrivent dans les mêmes bases (Postgres, Qdrant, Open WebUI). **Règle : arrêter l'un avant de démarrer l'autre.** Deux Postgres sur le même dossier de données l'abîmeraient.
- Les données ne sont pas dans le dossier du projet mais dans `WS_DIR` : les deux dossiers retrouvent les mêmes modèles, comptes Open WebUI, clés LiteLLM, index des normes et certificat Caddy (les postes n'ont rien à changer).

### Ce qui diffère entre les deux dossiers

Seuls deux fichiers non suivis par git sont à recopier puis modifier : `.env` et `.env.vllm`.

| Réglage | Dossier 1 Spark (`ia-locale`) | Dossier duo, Spark 1 | Dossier duo, Spark 2 |
|---|---|---|---|
| Branche | `spark` | `spark-duo` | `spark-duo` |
| `COMPOSE_FILE` | `docker-compose.yml:docker-compose.spark.yml` | `docker-compose.yml:docker-compose.spark.yml:docker-compose.spark-duo.yml` | `docker-compose.spark-worker.yml` |
| `WS_DIR` | `/home/admin_spark4c7c/data/ws` | `/home/admin_spark4c7c/data/ws` (**chemin absolu**, le même) | `/home/admin_sparkXXXX/data/ws` (**à changer** après la copie du `.env`) |
| `VLLM_API_KEY` | vide | une clé (`openssl rand -hex 16`) | la même clé |
| `DUO_INTERFACE`, `DUO_HCA`, `DUO_IP_TETE`, `DUO_IP_WORKER` | absents | remplis | remplis, **identiques** au Spark 1 |
| `VLLM_GPU_UTIL` | valeur du profil (0.74) | la même pour commencer | (lue sur le Spark 1) |
| `.env.vllm` | celui du profil | copie de celui du dossier 1 Spark | copie de celui du Spark 1 |
| Secrets (`LITELLM_MASTER_KEY`, `POSTGRES_PASSWORD`, `WEBUI_SECRET_KEY`, `MCP_TOKEN`), `SPARK_HOSTNAME` | les vôtres | **identiques** (sinon les clés et comptes existants ne marchent plus) | sans effet (rien ne les utilise) |

⚠ **`WS_DIR` doit être un chemin absolu.** Avec `WS_DIR=./ws` (relatif), le dossier duo chercherait ses données dans `ia-locale-duo/ws` : vide, donc pas de modèle, pas de comptes, pas de clés. Vérifier d'abord la valeur actuelle : `grep WS_DIR /home/admin_spark4c7c/data/ws/ia-locale/.env`. Si elle est relative, mettre dans le dossier duo le chemin absolu qui lui correspond (ex. `/home/admin_spark4c7c/data/ws/ia-locale/ws`).

### Pas à pas

**Spark 1 : préparer le dossier duo** (l'installation actuelle continue de tourner pendant ce temps)

```bash
cd /home/admin_spark4c7c/data/ws
git clone -b spark-duo https://github.com/Ameno1215/localIA.git ia-locale-duo
cd ia-locale-duo
cp ../ia-locale/.env ../ia-locale/.env.vllm .
chmod 600 .env
nano .env
```

Dans ce `.env`, modifier ou ajouter (le bloc « Duo » est en bas de `.env.spark.example`, à recopier s'il manque) :

```bash
WS_DIR=/home/admin_spark4c7c/data/ws
COMPOSE_FILE=docker-compose.yml:docker-compose.spark.yml:docker-compose.spark-duo.yml
VLLM_API_KEY=<résultat de : openssl rand -hex 16>
DUO_INTERFACE=enp1s0f0np0      # d'après ibdev2netdev, étape 2
DUO_HCA=rocep1s0f0,roceP2p1s0f0  # idem (les deux moitiés Up du port, étape 2.5)
DUO_IP_TETE=192.168.100.10
DUO_IP_WORKER=192.168.100.11
DUO_NCCL_DEBUG=INFO            # pour le premier test : montre si NCCL passe par le câble (RoCE)
```

Pour le **premier test**, vider aussi `VLLM_SPEC_ARGS=` (décodage spéculatif) : une option de moins qui pourrait ne pas aimer le mode deux GPU. On la remettra une fois le duo validé.

Vérifier que la configuration se lit sans erreur : `docker compose config --services` (doit lister vllm, litellm, open-webui, caddy…).

**Spark 2 : préparer le dossier duo**

```bash
# en tant que le compte de service du Spark 2 (admin_sparkXXXX ci-dessous : remplacer par son vrai nom), membre du groupe docker
mkdir -p /home/admin_sparkXXXX/data/ws
cd /home/admin_sparkXXXX/data/ws
git clone -b spark-duo https://github.com/Ameno1215/localIA.git ia-locale-duo
mkdir -p /home/admin_sparkXXXX/data/ws/{models,loras,cache/vllm,cache/flashinfer,cache/triton}
```

Une fois le câble configuré (étape 2), recopier depuis le Spark 1 le `.env`, le `.env.vllm` et les modèles :

```bash
# depuis le Spark 1, dans /home/admin_spark4c7c/data/ws/ia-locale-duo
scp .env .env.vllm admin_sparkXXXX@192.168.100.11:/home/admin_sparkXXXX/data/ws/ia-locale-duo/
rsync -a --info=progress2 /home/admin_spark4c7c/data/ws/models/ admin_sparkXXXX@192.168.100.11:/home/admin_sparkXXXX/data/ws/models/
```

Le dossier `models` entier est recopié exprès : le profil actif peut utiliser plusieurs dossiers (le modèle et son brouillon de décodage spéculatif, par exemple `…-LiLiCorr`). Par le câble, ~80 Go passent en quelques minutes.

Sur le Spark 2, dans `/home/admin_sparkXXXX/data/ws/ia-locale-duo/.env`, changer **deux lignes** :

```bash
COMPOSE_FILE=docker-compose.spark-worker.yml
WS_DIR=/home/admin_sparkXXXX/data/ws
```

Le chemin des données peut différer d'un Spark à l'autre : chaque `.env` donne le sien, et dans les conteneurs les modèles sont toujours vus sous `/models/…`. Seuls comptent le **nom du dossier du modèle** (identique des deux côtés grâce au `rsync`) et `WS_DIR` qui pointe au bon endroit sur chaque machine.

puis télécharger l'image : `docker compose pull`.

**Basculer** (coupure du service pendant le chargement du modèle)

```bash
# Spark 1 : arrêter l'installation 1 Spark (et le panneau web s'il tourne : Ctrl+C dans son terminal)
cd /home/admin_spark4c7c/data/ws/ia-locale && docker compose down

# Spark 2
cd /home/admin_sparkXXXX/data/ws/ia-locale-duo && docker compose up -d

# Spark 1
cd /home/admin_spark4c7c/data/ws/ia-locale-duo && docker compose up -d
docker compose logs -f vllm
```

Attendu dans les journaux, dans l'ordre : `Attente du Spark 2…` (tant que le Spark 2 n'a pas rejoint), `Cluster prêt : 2 GPU`, puis le chargement du modèle. Ensuite (section 4) : `docker compose exec vllm ray status`, la ligne `NET/IB` de NCCL, puis `./scripts/02-verifier-spark.sh`.

**Revenir à l'installation 1 Spark** (à tout moment, rien n'a été modifié dans l'ancien dossier)

```bash
# Spark 1
cd /home/admin_spark4c7c/data/ws/ia-locale-duo && docker compose down
# Spark 2
cd /home/admin_sparkXXXX/data/ws/ia-locale-duo && docker compose down
# Spark 1
cd /home/admin_spark4c7c/data/ws/ia-locale && docker compose up -d
```

Pendant le test, lancer **toutes** les commandes (`docker compose …`, `./scripts/…`, le panneau web) depuis le dossier duo. Lancées depuis l'ancien dossier, elles agiraient sur l'installation 1 Spark.

## 2. Brancher et configurer le câble (sur les deux Spark)

### 2.1 Un câble suffit

Chaque Spark a une carte ConnectX-7 avec **deux ports QSFP** à l'arrière. La carte est limitée à ~200 Gb/s au total (son lien PCIe), quel que soit le nombre de câbles. Un câble entre les deux Spark donne déjà ce maximum, et c'est le montage du playbook NVIDIA. Avec deux câbles, le second n'apporte pas de débit en plus et complique la configuration : **pour le premier test, un seul câble**, le second débranché (gardé en secours).

Branche le câble sur le **même port des deux côtés** (par exemple le port de gauche sur chaque Spark) : les noms d'interface seront alors identiques sur les deux machines, et un seul `.env` sert aux deux.

### 2.2 Repérer les interfaces (sur chaque Spark)

```bash
ibdev2netdev
```

Exemple de sortie, câble sur le premier port :

```
rocep1s0f0 port 1 ==> enp1s0f0np0 (Up)
rocep1s0f1 port 1 ==> enp1s0f1np1 (Down)
roceP2p1s0f0 port 1 ==> enP2p1s0f0np0 (Up)
roceP2p1s0f1 port 1 ==> enP2p1s0f1np1 (Down)
```

Le port branché apparaît **deux fois** en `(Up)` : la carte est reliée au processeur par deux liens PCIe de ~100 Gb/s, et chaque moitié a son interface. À gauche, la carte RDMA (pour `DUO_HCA`) ; à droite, l'interface réseau (pour `DUO_INTERFACE` et le netplan). Note les deux lignes `(Up)` : utiliser les deux moitiés donne les ~200 Gb/s, une seule ~100 Gb/s.

Si aucune ligne n'est `(Up)` : câble mal enfoncé, ou mauvais type de câble (il faut un câble QSFP direct, DAC ou optique, compatible 200 Gb/s).

### 2.3 Donner des adresses fixes (sur chaque Spark)

Une adresse par moitié, dans deux petits réseaux privés distincts (hors du réseau de l'équipe). Remplacer les noms d'interface par ceux notés en 2.2.

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

Spark 2 : le même fichier, avec `192.168.100.11/24` et `192.168.101.11/24`.

Vérifier sur chaque Spark : `ip -br addr | grep 192.168.10` doit montrer les deux adresses, interfaces `UP`.

### 2.4 Tester le lien (depuis le Spark 1)

```bash
ping -c3 192.168.100.11
ping -c3 192.168.101.11
ssh <compte-du-spark-2>@192.168.100.11 hostname     # servira au rsync des modèles
```

Facultatif, débit RDMA réel avec `ib_write_bw` (paquet `perftest`, s'il est installé) :

```bash
# Spark 2 (attend la connexion)
ib_write_bw -d rocep1s0f0 --report_gbits
# Spark 1
ib_write_bw -d rocep1s0f0 --report_gbits 192.168.100.11
```

Attendu : de l'ordre de 90 à 100 Gb/s pour une moitié. Le playbook NVIDIA [NCCL for two Sparks](https://build.nvidia.com/spark) propose aussi un test `all_reduce_perf` sur les deux moitiés.

### 2.5 Reporter dans le `.env` (Spark 1, puis copié sur le Spark 2)

```bash
DUO_INTERFACE=enp1s0f0np0                  # première interface (Up) : Ray, Gloo, démarrage de NCCL
DUO_HCA=rocep1s0f0,roceP2p1s0f0            # les deux cartes RDMA (Up) : NCCL répartit le trafic sur les deux moitiés
DUO_IP_TETE=192.168.100.10
DUO_IP_WORKER=192.168.100.11
```

Si une seule moitié est `(Up)`, ne mettre qu'elle dans `DUO_HCA`.

NCCL utilise l'index GID 3 (RoCE v2 sur IPv4, valeur habituelle sur le GB10). Pour le vérifier : `show_gids | grep 192.168.100` doit montrer `v2` à l'index 3 ; sinon, mettre le bon index dans `DUO_GID_INDEX`.

## 3. Préparer les deux Spark

- **Spark 1 (tête) :** le projet tel qu'il est déjà installé. Dans son `.env`, décommente et remplis le bloc « Duo » (`DUO_INTERFACE`, `DUO_HCA`, `DUO_IP_TETE`, `DUO_IP_WORKER`), mets une clé dans `VLLM_API_KEY` (`openssl rand -hex 16`), et ajoute le fichier duo :
  ```bash
  COMPOSE_FILE=docker-compose.yml:docker-compose.spark.yml:docker-compose.spark-duo.yml
  ```
- **Spark 2 :** clone le projet (même branche), copie le `.env` **et** le `.env.vllm` du Spark 1, puis change deux lignes du `.env` :
  ```bash
  COMPOSE_FILE=docker-compose.spark-worker.yml
  WS_DIR=/home/admin_sparkXXXX/data/ws   # son propre dossier de données
  ```
- **Les mêmes poids sur les deux :** `WS_DIR/models/<VLLM_MODEL>` doit exister des deux côtés (chacun avec son `WS_DIR`). Télécharge sur le Spark 1 (`./scripts/modeles.py telecharger <profil>`), puis copie par le câble, qui va bien plus vite que le réseau de l'équipe :
  ```bash
  rsync -a --info=progress2 /home/admin_spark4c7c/data/ws/models/<VLLM_MODEL> admin_sparkXXXX@192.168.100.11:/home/admin_sparkXXXX/data/ws/models/
  ```
- **La même image** (`VLLM_IMAGE`) sur les deux : elle est dans le `.env` copié, `docker compose pull` sur chaque Spark.

À chaque changement de modèle (`./scripts/modeles.py utiliser …` sur le Spark 1), recopie le `.env`, le `.env.vllm` et les poids sur le Spark 2, puis `docker compose up -d --force-recreate` sur le Spark 2.

## 4. Démarrer

```bash
# Spark 2
docker compose up -d
# Spark 1
docker compose up -d
docker compose logs -f vllm
```

L'ordre n'a pas d'importance : la tête affiche « Attente du Spark 2… » tant que le deuxième GPU n'a pas rejoint le cluster Ray, puis lance vLLM avec `--tensor-parallel-size 2`. Le chargement prend plus longtemps qu'avec un seul Spark.

Vérifications :
- `docker compose exec vllm ray status` (Spark 1) : 2 nœuds, 2 GPU.
- Avec `DUO_NCCL_DEBUG=INFO`, les journaux doivent montrer `NET/IB : Using [0]rocep…`. `NET/Socket` signifie que NCCL est passé par TCP : ça marche, mais nettement plus lentement (vérifier `DUO_HCA` et `/dev/infiniband`).
- `./scripts/02-verifier-spark.sh` sur la machine où tourne LiteLLM.

**Mémoire :** chaque Spark ne porte que la moitié des poids. `VLLM_GPU_UTIL` s'applique aux deux. Sur le Spark 2, rien d'autre ne tourne ; sur le Spark 1, tant que les autres services y restent, garde la valeur du profil. Une fois les services déplacés (étape 6), on peut viser ~0.85.

**Quel modèle :** `./scripts/modeles.py chercher` signale les recettes qui « ne tiennent PAS sur un seul Spark » : ce sont les candidats. `ajouter` écrit le profil comme d'habitude ; le passage sur deux GPU est ajouté par `scripts/spark-duo/tete.sh`, pas par le profil.

**« Free memory on device cuda:0 (… GiB) on startup is less than desired GPU memory utilization » :** la mémoire est unifiée, et le cache disque laissé par un gros téléchargement ou un `rsync` de modèles compte comme occupé. Vider le cache sur **les deux** Spark, puis relancer (Spark 2 d'abord) :
```bash
sudo sh -c 'sync; echo 3 > /proc/sys/vm/drop_caches'
docker compose up -d --force-recreate     # Spark 2, puis Spark 1
```
Si l'erreur revient avec le même chiffre, ce n'est pas le cache : le Spark 1 porte aussi Open WebUI, le RAG, Ray… (mesuré : ~86 Go libres sur 121). Baisser alors `VLLM_GPU_UTIL` dans le `.env` des deux Spark, sous « Go libres ÷ 121.7 » (0.68 pour 86 Go). En duo chaque Spark ne porte que la moitié des poids : une valeur plus basse qu'en mode 1 Spark suffit.

**Revenir à un seul Spark :** retire `docker-compose.spark-duo.yml` de `COMPOSE_FILE` sur le Spark 1, `docker compose down` sur le Spark 2, puis `docker compose up -d` sur le Spark 1 (avec un profil qui tient sur un Spark).

## 5. Sécurité réseau (Spark 1 surtout)

En duo, vLLM et Ray tournent sur le réseau de l'hôte : vLLM écoute le port 8000 sur toutes les interfaces, et Ray n'a **aucune authentification** (quiconque l'atteint peut exécuter du code). Donc :
- **`VLLM_API_KEY` obligatoire** : vLLM refuse alors toute requête `/v1` sans la clé (LiteLLM et l'indexation la reçoivent automatiquement).
- **Pare-feu** sur chaque Spark : tout est permis sur le câble, rien d'autre que le nécessaire depuis le réseau de l'équipe.
  ```bash
  sudo ufw allow ssh
  sudo ufw allow in on enp1s0f0np0                                      # le câble entre les deux Spark (1re moitié)
  sudo ufw allow in on enP2p1s0f0np0                                    # le câble (2e moitié)
  sudo ufw allow from 172.16.0.0/12 to any port 8000 proto tcp          # conteneurs du Spark 1 (LiteLLM, MCP) vers vLLM
  sudo ufw allow from <ip-du-serveur-annexe> to any port 8000 proto tcp # si l'étape 6 est faite
  sudo ufw default deny incoming && sudo ufw enable
  ```
  Les ports publiés par Docker (Caddy : 443 et 8443) restent ouverts : Docker passe avant `ufw`, c'est voulu.

## 6. Déplacer les services sur un serveur annexe

Sur un Spark, la mémoire est **unifiée** : tout ce qui tourne à côté du modèle lui prend de la place. Au démarrage de vLLM, la pile complète occupe déjà ~30 Gio (section 8 du GUIDE), d'où `VLLM_GPU_UTIL=0.74` au lieu de 0.8–0.9. Estimation de ce que chaque service occupe (à confirmer avec `docker stats` et `free -g` sur le Spark) :

| Service | Mémoire (ordre de grandeur) | Peut partir ? |
|---|---|---|
| Embeddings + reranker (2 vLLM « pooling ») | **~10 Go réservés + ~2–4 Go de processus** | Oui : sur le serveur, TEI sur CPU (image x86, la config du PC de test) |
| Open WebUI | 1–2 Go | Oui |
| LiteLLM + Postgres | 1–2 Go | Oui |
| Qdrant, MCP docs, Caddy | < 1 Go chacun | Oui |
| Bureau graphique d'Ubuntu (DGX OS) | 1–3 Go | Le désactiver : `sudo systemctl set-default multi-user.target` puis redémarrer |
| vLLM | le reste | Non : c'est le modèle |

Gain attendu : **15 à 20 Go** rendus au modèle sur le Spark 1, soit `VLLM_GPU_UTIL` de 0.74 à ~0.85.

**Le serveur :** n'importe quelle machine x86 Linux du réseau avec Docker, sans GPU, 16 Go de RAM et 4 cœurs suffisent (les embeddings sur CPU rendent l'indexation plus lente, pas la recherche). Le PC de test fait l'affaire.

Mise en place :
1. **Sur le serveur :** clone le projet, `./scripts/preparer-env-spark.sh <nom-du-serveur> /home/admin_spark4c7c/data/ws` (secrets, dossiers), puis dans son `.env` :
   ```bash
   COMPOSE_FILE=docker-compose.yml:docker-compose.serveur.yml
   VLLM_API_BASE=http://spark1.lan:8000/v1   # le Spark 1 (seul ou tête du duo)
   VLLM_API_KEY=<la même clé que sur le Spark 1>
   ```
2. **Copie les embeddings** (`bge-m3`, `bge-reranker-v2-m3`) depuis le Spark : `rsync -a <spark1>:/home/admin_spark4c7c/data/ws/models/bge-* /home/admin_spark4c7c/data/ws/models/`.
3. **Reprends les données** pour garder comptes, clés et conversations : arrête la pile du Spark 1 (`docker compose down`), puis copie `postgres/`, `open-webui/`, `qdrant/` et `docs-internes/` de son `WS_DIR` vers celui du serveur. Relance l'indexation une fois le serveur démarré (même modèle d'embeddings, mais servi autrement : mieux vaut repartir propre).
4. **Démarre le serveur :** `docker compose up -d`. Le service `vllm` n'y est jamais lancé.
5. **Sur le Spark 1 :** mets `VLLM_ECOUTE=0.0.0.0` dans le `.env` (inutile en duo), puis ne démarre plus que le modèle :
   ```bash
   docker compose down && docker compose up -d vllm
   ```
   Monte `VLLM_GPU_UTIL` par paliers (0.80, puis 0.85) en vérifiant que le Spark reste réactif. Attention : le panneau web du Spark et `docker compose up -d` sans nom de service relanceraient tout ; sur le Spark 1, démarre uniquement `vllm`.
6. **Postes et navigateurs :** ils visent désormais le serveur (`SPARK_HOSTNAME` du serveur). Distribue le nouveau certificat de Caddy (section 8 du GUIDE) et relance `installer-opencode.sh --serveur https://<serveur> …` sur les postes. Les clés LiteLLM suivent avec la base Postgres copiée.

Répartition finale, duo + serveur :

```
Postes ──HTTPS──> Serveur annexe : Caddy, LiteLLM, Postgres, Open WebUI, Qdrant, MCP, TEI (CPU)
                        │  http://spark1:8000 + VLLM_API_KEY
                        ▼
                  Spark 1 : vLLM (tête Ray) ══ câble QSFP ══ Spark 2 : Ray worker
```
