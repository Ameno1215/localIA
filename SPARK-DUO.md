# Deux DGX Spark et un serveur annexe

> ⚠ **Rien de ce document n'a encore tourné sur les Spark.** Les réglages suivent le playbook NVIDIA « Connect two Sparks » et la documentation multi-nœud de vLLM (Ray). Vérifie chaque étape avant de passer à la suivante.

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

## 2. Brancher et configurer le câble (sur les deux Spark)

1. **Branche le câble QSFP** entre un port ConnectX-7 de chaque Spark (le même port des deux côtés, c'est plus simple).
2. **Repère l'interface active :**
   ```bash
   ibdev2netdev
   ```
   Note la ligne `(Up)`, par exemple `rocep1s0f0 port 1 ==> enp1s0f0np0 (Up)` : `rocep1s0f0` est la carte RDMA (`DUO_HCA`), `enp1s0f0np0` l'interface réseau (`DUO_INTERFACE`). Chaque port apparaît en double (`enp1s0f0np0` et `enP2p1s0f0np0`) : prends la première ligne `Up`.
3. **Donne une adresse fixe à chaque Spark sur ce lien** (adresses privées, hors du réseau de l'équipe) :
   ```bash
   sudo tee /etc/netplan/40-qsfp.yaml >/dev/null <<'NETPLAN'
   network:
     version: 2
     ethernets:
       enp1s0f0np0:                      # DUO_INTERFACE
         addresses: [192.168.100.10/24]  # .10 sur le Spark 1, .11 sur le Spark 2
   NETPLAN
   sudo chmod 600 /etc/netplan/40-qsfp.yaml && sudo netplan apply
   ```
4. **Vérifie :** depuis le Spark 1, `ping -c3 192.168.100.11`.

Pour mesurer le débit réel du lien (~200 Gb/s attendus), le playbook NVIDIA [NCCL for two Sparks](https://build.nvidia.com/spark) fournit un test `all_reduce_perf`. Facultatif, mais utile si le duo est lent.

## 3. Préparer les deux Spark

- **Spark 1 (tête) :** le projet tel qu'il est déjà installé. Dans son `.env`, décommente et remplis le bloc « Duo » (`DUO_INTERFACE`, `DUO_HCA`, `DUO_IP_TETE`, `DUO_IP_WORKER`), mets une clé dans `VLLM_API_KEY` (`openssl rand -hex 16`), et ajoute le fichier duo :
  ```bash
  COMPOSE_FILE=docker-compose.yml:docker-compose.spark.yml:docker-compose.spark-duo.yml
  ```
- **Spark 2 :** clone le projet (même branche), copie le `.env` **et** le `.env.vllm` du Spark 1, puis change une seule ligne du `.env` :
  ```bash
  COMPOSE_FILE=docker-compose.spark-worker.yml
  ```
- **Les mêmes poids aux mêmes chemins sur les deux :** `WS_DIR/models/<VLLM_MODEL>` doit exister des deux côtés. Télécharge sur le Spark 1 (`./scripts/modeles.py telecharger <profil>`), puis copie par le câble, qui va bien plus vite que le réseau de l'équipe :
  ```bash
  rsync -a --info=progress2 /home/data/ws/models/<VLLM_MODEL> <utilisateur>@192.168.100.11:/home/data/ws/models/
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

**Revenir à un seul Spark :** retire `docker-compose.spark-duo.yml` de `COMPOSE_FILE` sur le Spark 1, `docker compose down` sur le Spark 2, puis `docker compose up -d` sur le Spark 1 (avec un profil qui tient sur un Spark).

## 5. Sécurité réseau (Spark 1 surtout)

En duo, vLLM et Ray tournent sur le réseau de l'hôte : vLLM écoute le port 8000 sur toutes les interfaces, et Ray n'a **aucune authentification** (quiconque l'atteint peut exécuter du code). Donc :
- **`VLLM_API_KEY` obligatoire** : vLLM refuse alors toute requête `/v1` sans la clé (LiteLLM et l'indexation la reçoivent automatiquement).
- **Pare-feu** sur chaque Spark : tout est permis sur le câble, rien d'autre que le nécessaire depuis le réseau de l'équipe.
  ```bash
  sudo ufw allow ssh
  sudo ufw allow in on enp1s0f0np0                                      # le câble entre les deux Spark
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
1. **Sur le serveur :** clone le projet, `./scripts/preparer-env-spark.sh <nom-du-serveur> /home/data/ws` (secrets, dossiers), puis dans son `.env` :
   ```bash
   COMPOSE_FILE=docker-compose.yml:docker-compose.serveur.yml
   VLLM_API_BASE=http://spark1.lan:8000/v1   # le Spark 1 (seul ou tête du duo)
   VLLM_API_KEY=<la même clé que sur le Spark 1>
   ```
2. **Copie les embeddings** (`bge-m3`, `bge-reranker-v2-m3`) depuis le Spark : `rsync -a <spark1>:/home/data/ws/models/bge-* /home/data/ws/models/`.
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
