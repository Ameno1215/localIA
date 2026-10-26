# Premier démarrage sur le DGX Spark

Ce fichier suffit pour tester la branche `spark` de zéro à « ça répond ». Le détail du fonctionnement est dans [GUIDE.md](GUIDE.md) (section 8), l'écart avec les tutoriels NVIDIA dans [AUDIT-NVIDIA.md](AUDIT-NVIDIA.md).

**État honnête :** rien de ce qui suit n'a été exécuté sur un Spark. Le Compose, les scripts et les profils ont été vérifiés sur un PC (rendu de la configuration, tests des scripts), pas le démarrage réel des conteneurs. Le premier démarrage est le vrai test.

## 0. Avant de commencer

- DGX OS avec Docker, et le compte qui lance la stack dans le groupe `docker` (`groups | grep docker`). Ce groupe donne un accès équivalent à root : réserve-le au compte de service.
- Pilote : `nvidia-smi` doit répondre. Les recettes NVIDIA ont été validées avec un pilote 580.x et CUDA 13.x.
- Docker est déjà installé et configuré sur le Spark (stockage sur `/home/data/docker`) : **rien à installer**. Vérifie seulement `docker info --format '{{.DockerRootDir}}'`.
- Espace disque : compte **150 Go libres** pour le démarrage par défaut (Nemotron Super 76 Go avec son brouillon, embeddings et reranker 5 Go, images Docker ~30 Go), et **300 Go** si tu veux garder d'autres modèles à côté.
- Internet pour le premier lancement (modèles et images). Ensuite tout peut tourner hors ligne.
- Un nom DNS ou une adresse IP que les postes de l'équipe peuvent joindre (`spark1.lan` dans les exemples).

### Où vont les fichiers

Le projet sépare la **configuration** des **données des conteneurs**. Avec l'organisation `/home/data/` :

| Dossier | Contenu | Réglé par |
|---|---|---|
| `/home/data/ws/ia-locale` | Le projet cloné (git) : fichiers Docker Compose, `config/`, scripts, `.env` | Le `git clone` de l'étape 1 |
| `/home/data/ws` (le reste) | Les **données** : modèles (`models/`), caches de compilation, bases Postgres et Qdrant, comptes Open WebUI, certificats, documents des normes | `WS_DIR` dans le `.env` (étape 3) |
| `/home/data/docker` | Le stockage de Docker lui-même (images, couches, conteneurs) | Déjà configuré sur le Spark |

Tout est sous `/home/data/ws` : le projet dans `ia-locale/`, les données à côté (`models/`, `postgres/`, `qdrant/`…). Rien n'est stocké dans des « volumes Docker » cachés : tout ce qui est persistant est un dossier de `WS_DIR`, donc facile à sauvegarder ou à déplacer.

## 1. Copier le projet sur le Spark

Le dépôt est sur GitHub (`git@github.com:Ameno1215/localIA.git`, branche `spark`). Sur le Spark, avec le compte de service :
```bash
git clone -b spark git@github.com:Ameno1215/localIA.git /home/data/ws/ia-locale
cd /home/data/ws/ia-locale
```
Si le dépôt est privé, le Spark doit avoir une clé SSH autorisée sur GitHub (`Permission denied (publickey)` sinon).

Le compte de service doit pouvoir écrire dans `/home/data/ws` : sinon, un administrateur lance une fois `sudo mkdir -p /home/data/ws && sudo chown <compte> /home/data/ws`.

Sans accès à GitHub : `git bundle create ~/ia-locale-spark.bundle spark` sur le PC, copie du fichier (`scp`), puis `git clone -b spark ia-locale-spark.bundle /home/data/ws/ia-locale` sur le Spark.

**Les PDF des normes ne sont pas dans git** (licences ISO et GlobalPlatform). Copie-les dans le dossier de données, sinon la recherche dans les normes restera vide :
```bash
# Sur le PC :
rsync -av docs-internes/ utilisateur@spark1.lan:/home/data/ws/docs-internes/
```

## 2. Vérifier le Spark

```bash
./scripts/00-prerequis.sh      # sur le Spark : vérifie seulement, n'installe rien, pas de sudo
```
Il vérifie le pilote, Docker (sans sudo), `docker compose` et Python, affiche la version du pilote et l'emplacement du stockage Docker, puis prépare l'outil de téléchargement `hf` dans `.venv`. Si Python n'a pas le module `venv`, ce n'est pas bloquant : les téléchargements passent alors par un conteneur Python. Il ne touche ni au pilote, ni à Docker, ni au NVIDIA Container Toolkit.

## 3. Créer le `.env`

```bash
./scripts/preparer-env-spark.sh spark1.lan /home/data/ws      # ton nom DNS ou ton adresse IP, puis le dossier des données
```
Il copie `.env.spark.example`, génère les secrets (la clé maître LiteLLM commence bien par `sk-`), fixe le nom du serveur et `WS_DIR`, puis **crée les sous-dossiers de données** à ton nom (c'est important : s'ils étaient créés par Docker, ils appartiendraient à `root` et le téléchargement des modèles échouerait). Il ne remplace jamais un `.env` existant. Le jeton d'équipe pour les normes est `MCP_TOKEN` (à donner aux développeurs).

Le projet cloné (`/home/data/ws/ia-locale`) est à côté des données, dans le même dossier : c'est voulu, les deux ne se mélangent pas.

## 4. Télécharger les modèles (≈ 82 Go)

```bash
./scripts/01-telecharger-modeles.sh --spark
```
Embeddings (`bge-m3`), reranker et le modèle par défaut **Nemotron 3.5 Super VL 120B** (NVFP4, 75 Go, plus son brouillon LiLiCorr de 1,3 Go pour le décodage spéculatif), dans `WS_DIR/models`. Compte une heure ou plus selon la connexion. Les modèles sont publics, aucun jeton Hugging Face n'est nécessaire. Le script refuse de démarrer tant que le `.env` de l'étape 3 n'existe pas.

## 5. Démarrer

```bash
docker compose up -d
docker compose logs -f vllm        # Ctrl+C pour quitter les journaux ; vLLM continue
```
Le `.env` contient `COMPOSE_FILE`, donc `docker compose` utilise la configuration Spark sans option.

- **Première fois : 15 à 30 minutes, parfois plus** (téléchargement des images, chargement des 75 Go du modèle, compilation des noyaux). Les compilations sont conservées dans `WS_DIR/cache/`, les démarrages suivants sont plus courts.
- vLLM est prêt quand les journaux affichent que le serveur écoute, et que `docker compose ps` montre `vllm` en `healthy` (le contrôle de santé attend jusqu'à 60 minutes).

## 6. Vérifier

```bash
./scripts/02-verifier-spark.sh
```
Attendu : **5 lignes `[OK]`** (chat, appel d'outil, débit, embeddings, reranker).

Pour le débit, NVIDIA mesure **51 tok/s** pour ce modèle sur Spark (une requête de code, avec le brouillon LiLiCorr). Le script en mesure une aussi, avec le préremplissage compté : un peu moins est normal. Moins de la moitié, ou un texte incohérent : voir le tableau ci-dessous.

## 7. Tester Open WebUI et OpenCode (avec le modèle par défaut)

Une fois l'étape 6 réussie (5 lignes `[OK]`), tout est prêt pour les deux outils, avec Nemotron 3.5 Super VL : 120 Md de paramètres, dont 12 Md actifs par mot, contexte de 262 144 jetons. Il comprend aussi les images (une par message). Le nom côté clients reste `qwen-local` : c'est un nom fixe, pas le modèle réel.

**Open WebUI (le chat)** : depuis un navigateur du réseau.
1. Ouvre `https://<SPARK_HOSTNAME>:8443` (pour un test sans nom ni certificat : `OPEN_WEBUI_HTTP=0.0.0.0` dans `.env`, `docker compose up -d open-webui`, puis `http://<ip-du-spark>:3000`). Le navigateur signale un certificat inconnu (le certificat est interne) : accepte-le pour ce test.
2. **Crée tout de suite le compte administrateur** : le premier compte créé devient admin, et la page est joignable par tout le réseau.
3. Choisis le modèle **`qwen-local`** (c'est Nemotron derrière). Ignore `normes` : c'est le modèle entraîné sur les normes, qui n'existe pas encore (erreur si tu le choisis).
4. Pose une question. Pour la recherche dans les normes, copie d'abord les PDF (étape 1) et lance l'indexation ci-dessous. L'outil de recherche est déclaré d'office ; sinon, voir la section 6 du GUIDE.

**OpenCode (l'agent de code)** : depuis le poste d'un développeur, pas sur le Spark.
1. **Sur le Spark**, crée une clé personnelle. Elle ne s'affiche qu'une fois : copie-la.
   ```bash
   ./scripts/creer-cle-utilisateur.sh prenom.nom
   ```
2. **Sur le Spark**, récupère le certificat HTTPS interne, à copier sur le poste :
   ```bash
   docker compose cp caddy:/data/caddy/pki/authorities/local/root.crt ./spark-ca.crt
   ```
3. **Sur le poste** (Node.js 20 ou plus, une copie du dépôt, et un moyen de joindre le nom du Spark) :
   ```bash
   ./scripts/installer-opencode.sh --serveur https://spark1.lan --contexte 262144 --mcp https://spark1.lan/mcp --jeton-mcp <MCP_TOKEN> --ca ./spark-ca.crt
   ```
   Le script demande la clé de l'étape 1 et vérifie que le serveur répond. `MCP_TOKEN` se lit dans le `.env` du Spark.
4. Ouvre un nouveau terminal, puis `cd ton-projet` et `opencode`.

**Indexer les normes et finir :**
```bash
docker compose exec mcp-docs python ingest.py       # indexe docs-internes/ (texte, tableaux, images)
docker compose restart mcp-docs                     # pour que le modèle voie la liste des documents
```
- Panneau de contrôle (écoute seulement en local sur le Spark) : lance `./scripts/panneau.sh` sur le Spark, puis depuis ton poste `ssh -L 7000:localhost:7000 utilisateur@spark1.lan` et ouvre http://localhost:7000.

## 8. Si quelque chose ne va pas

| Ce que tu vois | Piste |
|---|---|
| `failed to fulfil mount request: open /usr/lib/aarch64-linux-gnu/libnvidia-….so…: no such file or directory` au `docker compose up` | Rien à voir avec le projet : la spécification CDI du NVIDIA Container Toolkit (`/etc/cdi/nvidia.yaml` ou `/var/run/cdi/nvidia.yaml`) liste des fichiers d'une ancienne version du pilote. Un administrateur la régénère : `sudo nvidia-ctk cdi generate --output=/etc/cdi/nvidia.yaml && sudo systemctl restart docker` (si le fichier est dans `/var/run/cdi`, régénérer celui-là ou `sudo systemctl restart nvidia-cdi-refresh`). Vérifier : `docker run --rm --gpus all ubuntu nvidia-smi`, puis `docker compose up -d` |
| `Free memory on device cuda:0 (92/121.69 GiB) on startup is less than desired GPU memory utilization` | Le reste de la pile occupe ~30 Gio de la mémoire unifiée. Baisser `VLLM_GPU_UTIL` dans `.env` (0.74 par défaut ; un `.env` créé avant le 23/10 contient 0.8), puis `docker compose up -d vllm` |
| `vllm` redémarre en boucle ou s'arrête | `docker compose logs --tail 100 vllm`. Mémoire : `sudo sh -c 'sync; echo 3 > /proc/sys/vm/drop_caches'` (mémoire unifiée, conseil NVIDIA) |
| `vllm` s'arrête pendant le chargement (mémoire) | Vider le cache disque (ligne du dessus), arrêter tout autre programme sur le GPU, puis `docker compose up -d vllm`. Sinon, baisser `VLLM_MAX_MODEL_LEN` à `131072` dans `.env` |
| `unrecognized arguments` ou option inconnue au démarrage de `vllm` | L'image ne correspond pas au profil : vérifier `VLLM_IMAGE=eugr/spark-vllm:nightly-20261015` dans `.env` (le profil Nemotron en a besoin) |
| Débit très bas, texte incohérent | Vider `VLLM_SPEC_ARGS=` dans `.env` (brouillon LiLiCorr), puis `docker compose up -d vllm` |
| Nemotron ne démarre vraiment pas | Repli sur le modèle plus léger et plus rapide : `./scripts/modeles.py utiliser qwen3.6-35b-a3b-nvfp4` (image NGC de NVIDIA) |
| Avec un profil Qwen (image `nvcr.io/...`) : `compatibility mode is UNAVAILABLE` ou `CUDA driver version is insufficient` | L'image NGC vise un pilote plus récent que le 580 du Spark. Dans `.env` : `VLLM_IMAGE=vllm/vllm-openai:v0.30.0` et `--load-format fastsafetensors` à la place de `--load-format safetensors` dans `VLLM_EXTRA_ARGS` |
| Embeddings ou reranker en erreur | `docker compose logs --tail 80 tei-embed tei-rerank` (ils tournent sous vLLM en mode « pooling », non validé sur Spark) |
| `[ECHEC] appel d'outil` | Le parser d'outils ne correspond pas au modèle : `VLLM_TOOL_PARSER` dans `.env` |
| `Crée d'abord le .env du Spark` alors que le `.env` existe, ou `grep: .env: Permission denied` | Le `.env` appartient à `root` (script lancé avec `sudo`). `sudo chown $USER .env` dans le dossier du projet, puis relance les scripts **sans** sudo |
| `Permission denied` en écrivant dans le dossier des données (ex. `/work/models/bge-reranker-v2-m3`) | Le dossier a été créé au nom de `root` (`preparer-env-spark.sh` lancé avec sudo, ou Docker). `sudo chown -R $USER /home/data/ws` règle tout d'un coup. `sudo chown -R $USER /home/data/ws/models` (et le dossier concerné), ou relance `preparer-env-spark.sh` sur un `.env` supprimé |
| Autre | Section 9 du GUIDE |

**Pour me transmettre un problème :**
```bash
./scripts/diagnostic-spark.sh
```
Il écrit un fichier `diagnostic-spark-….txt` (état du GPU, de la mémoire, des conteneurs, réglages vLLM, derniers journaux). Il ne contient pas les secrets, mais relis-le avant de l'envoyer.

## 9. Ce qui n'est pas validé (à surveiller)

1. Le lancement de Nemotron Super dans ce Compose. NVIDIA a validé la recette sur un Spark 128 Go avec le pilote 580.159 et CUDA 13.0, seule, sans les embeddings et le reranker qui tournent ici à côté (0.08 de la mémoire en plus).
2. Les embeddings et le reranker servis par vLLM sur la puce GB10 (la configuration n'a jamais tourné sur un Spark).
3. Le certificat HTTPS interne de Caddy avec ton nom de serveur.
4. Le panneau web, corrigé pour la mémoire unifiée mais jamais lancé sur un Spark.

## 10. Essayer d'autres modèles (après le premier succès)

```bash
./scripts/modeles.py lister                              # profils, poids téléchargés ou non, modèle actif
./scripts/modeles.py utiliser bolt-74b-a9b-24gb          # télécharge si besoin, met .env à jour, redémarre vLLM
./scripts/02-verifier-spark.sh                           # compare le débit à celui du tableau NVIDIA
```
Les débits mesurés par NVIDIA (une requête, code) : Lightning 144, Qwen3.6-35B 124, Bolt 89, Super VL 51, Flash Next 38, Qwen3.8 27B 37 tok/s. Le Super VL par défaut est le plus capable mais pas le plus rapide : les plus petits répondent plus vite, avec moins de qualité. Un seul modèle est chargé à la fois ; changer de modèle coupe le service pendant le chargement (GUIDE, section 8). Pour revenir au modèle par défaut : `./scripts/modeles.py utiliser nemotron-3.5-super-vl-120b-a12b-nvfp4`.

## 11. Arrêter ou revenir en arrière

```bash
docker compose stop        # arrête sans rien perdre
docker compose down        # supprime les conteneurs ; les données (comptes, documents indexés) sont conservées
```
