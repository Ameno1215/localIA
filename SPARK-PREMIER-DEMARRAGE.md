# Premier démarrage sur le DGX Spark

Ce fichier suffit pour tester la branche `spark` de zéro à « ça répond ». Le détail du fonctionnement est dans [GUIDE.md](GUIDE.md) (section 8), l'écart avec les tutoriels NVIDIA dans [AUDIT-NVIDIA.md](AUDIT-NVIDIA.md).

**État honnête :** rien de ce qui suit n'a été exécuté sur un Spark. Le Compose, les scripts et les profils ont été vérifiés sur un PC (rendu de la configuration, tests des scripts), pas le démarrage réel des conteneurs. Le premier démarrage est le vrai test.

## 0. Avant de commencer

- DGX OS avec Docker, et le compte qui lance la stack dans le groupe `docker` (`groups | grep docker`). Ce groupe donne un accès équivalent à root : réserve-le au compte de service.
- Pilote : `nvidia-smi` doit répondre. Les recettes NVIDIA ont été validées avec un pilote 580.x et CUDA 13.x.
- Espace disque : compte **100 Go libres** pour le démarrage par défaut (modèle 23 Go, embeddings et reranker 5 Go, images Docker), et **250 Go** si tu veux essayer les gros modèles (75 à 103 Go de poids chacun).
- Internet pour le premier lancement (modèles et images). Ensuite tout peut tourner hors ligne.
- Un nom DNS ou une adresse IP que les postes de l'équipe peuvent joindre (`spark1.lan` dans les exemples).

### Où vont les fichiers

Le projet sépare la **configuration** des **données des conteneurs**. Avec l'organisation `/home/data/` :

| Dossier | Contenu | Réglé par |
|---|---|---|
| `/home/data/compose/ia-locale` | Le projet cloné : fichiers Docker Compose, `config/`, scripts, `.env` | Le `git clone` de l'étape 1 |
| `/home/data/ws` | Les **données** : modèles (`models/`), caches de compilation, bases Postgres et Qdrant, comptes Open WebUI, certificats, documents des normes | `WS_DIR` dans le `.env` (étape 3) |
| `/home/data/docker` | Le stockage de Docker lui-même (images, couches, conteneurs) | `data-root` de Docker (ci-dessous) |

Rien n'est stocké dans des « volumes Docker » cachés : tout ce qui est persistant est un dossier de `WS_DIR`, donc facile à sauvegarder ou à déplacer.

**Pour mettre le stockage de Docker sur `/home/data/docker`** (une seule fois, par un administrateur, **avant** de télécharger la moindre image). Ajoute cette ligne dans `/etc/docker/daemon.json` sans effacer les lignes existantes (DGX OS y déclare le moteur NVIDIA) :
```json
{ "data-root": "/home/data/docker" }
```
Puis :
```bash
sudo systemctl restart docker
```
Vérifie avec `docker info --format '{{.DockerRootDir}}'`.

## 1. Copier le projet sur le Spark

Le dépôt est sur GitHub (`git@github.com:Ameno1215/localIA.git`, branche `spark`). Sur le Spark, avec le compte de service :
```bash
git clone -b spark git@github.com:Ameno1215/localIA.git /home/data/compose/ia-locale
cd /home/data/compose/ia-locale
```
Si le dépôt est privé, le Spark doit avoir une clé SSH autorisée sur GitHub (`Permission denied (publickey)` sinon).

Sans accès à GitHub : `git bundle create ~/ia-locale-spark.bundle spark` sur le PC, copie du fichier (`scp`), puis `git clone -b spark ia-locale-spark.bundle ia-locale` sur le Spark.

**Les PDF des normes ne sont pas dans git** (licences ISO et GlobalPlatform). Copie-les dans le dossier de données, sinon la recherche dans les normes restera vide :
```bash
# Sur le PC (remplace WS par le dossier des données du Spark) :
rsync -av docs-internes/ utilisateur@spark1.lan:/home/data/ws/docs-internes/
```

## 2. Préparer le Spark

```bash
./scripts/00-prerequis.sh      # sur DGX OS : ne touche ni au pilote ni au NVIDIA Container Toolkit
```
Il installe seulement Python et l'outil de téléchargement `hf`. Il demande `sudo` : lance-le avec un compte administrateur si ton compte de service n'a pas `sudo` (il n'y a rien d'autre à faire avec les droits administrateur).

## 3. Créer le `.env`

```bash
./scripts/preparer-env-spark.sh spark1.lan /home/data/ws      # ton nom DNS ou ton adresse IP, puis le dossier des données
```
Il copie `.env.spark.example`, génère les secrets (la clé maître LiteLLM commence bien par `sk-`), fixe le nom du serveur et `WS_DIR`, puis **crée les sous-dossiers de données** à ton nom (c'est important : s'ils étaient créés par Docker, ils appartiendraient à `root` et le téléchargement des modèles échouerait). Il ne remplace jamais un `.env` existant. Le jeton d'équipe pour les normes est `MCP_TOKEN` (à donner aux développeurs).

Si `/home/data/ws` n'existe pas ou n'est pas à ton nom, le script te donne la commande pour le créer (`sudo mkdir -p /home/data/ws && sudo chown $USER /home/data/ws`).

## 4. Télécharger les modèles (≈ 28 Go)

```bash
./scripts/01-telecharger-modeles.sh --spark
```
Embeddings (`bge-m3`), reranker et le modèle par défaut (Qwen3.6-35B NVFP4), dans `WS_DIR/models`. Les modèles sont publics, aucun jeton Hugging Face n'est nécessaire. Le script refuse de démarrer tant que le `.env` de l'étape 3 n'existe pas.

## 5. Démarrer

```bash
docker compose up -d
docker compose logs -f vllm        # Ctrl+C pour quitter les journaux ; vLLM continue
```
Le `.env` contient `COMPOSE_FILE`, donc `docker compose` utilise la configuration Spark sans option.

- **Première fois : plusieurs minutes, souvent plus** (téléchargement des images, chargement du modèle, compilation des noyaux). Les compilations sont conservées dans `cache/`, les démarrages suivants sont plus courts.
- vLLM est prêt quand les journaux affichent que le serveur écoute, et que `docker compose ps` montre `vllm` en `healthy` (le contrôle de santé attend jusqu'à 60 minutes).

## 6. Vérifier

```bash
./scripts/02-verifier-spark.sh
```
Attendu : **5 lignes `[OK]`** (chat, appel d'outil, débit, embeddings, reranker).

Pour le débit, NVIDIA mesure **124 tok/s** pour ce modèle sur Spark (une requête de code). Le script en mesure une aussi, avec le préremplissage compté : un peu moins est normal. Moins de la moitié, ou un texte incohérent : voir le tableau ci-dessous.

## 7. Tester Open WebUI et OpenCode (avec le modèle par défaut)

Une fois l'étape 6 réussie (5 lignes `[OK]`), tout est prêt pour les deux outils, avec Qwen3.6-35B. Ce n'est pas un petit modèle (35 Md de paramètres), mais il n'en active que 3 Md par mot, d'où sa vitesse.

**Open WebUI (le chat)** : depuis un navigateur du réseau.
1. Ouvre `https://<SPARK_HOSTNAME>:8443`. Le navigateur signale un certificat inconnu (le certificat est interne) : accepte-le pour ce test.
2. **Crée tout de suite le compte administrateur** : le premier compte créé devient admin, et la page est joignable par tout le réseau.
3. Choisis le modèle **`qwen-local`**. Ignore `normes` : c'est le modèle entraîné sur les normes, qui n'existe pas encore (erreur si tu le choisis).
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
   ./scripts/installer-opencode.sh --serveur https://spark1.lan --contexte 131072 --mcp https://spark1.lan/mcp --jeton-mcp <MCP_TOKEN> --ca ./spark-ca.crt
   ```
   Le script demande la clé de l'étape 1 et vérifie que le serveur répond. `MCP_TOKEN` se lit dans le `.env` du Spark.
4. Ouvre un nouveau terminal, puis `cd ton-projet` et `opencode`.

**Indexer les normes et finir :**
```bash
docker compose exec mcp-docs python ingest.py       # indexe les PDF de docs-internes/
```
- Panneau de contrôle (écoute seulement en local sur le Spark) : lance `./scripts/panneau.sh` sur le Spark, puis depuis ton poste `ssh -L 7000:localhost:7000 utilisateur@spark1.lan` et ouvre http://localhost:7000.

## 8. Si quelque chose ne va pas

| Ce que tu vois | Piste |
|---|---|
| `vllm` redémarre en boucle ou s'arrête | `docker compose logs --tail 100 vllm`. Mémoire : `sudo sh -c 'sync; echo 3 > /proc/sys/vm/drop_caches'` (mémoire unifiée, conseil NVIDIA) |
| Erreur à l'ouverture de l'image `nvcr.io/...` | `docker logout nvcr.io` (l'image est publique, une ancienne connexion peut gêner) |
| La commande `vllm` est introuvable dans l'image NGC | Repli : dans `.env`, `VLLM_IMAGE=vllm/vllm-openai:v0.30.0` et remplacer `--load-format safetensors` par `--load-format fastsafetensors` dans `VLLM_EXTRA_ARGS` |
| Débit très bas, texte incohérent | Vider `VLLM_SPEC_ARGS=` dans `.env`, puis `docker compose up -d vllm` |
| Embeddings ou reranker en erreur | `docker compose logs --tail 80 tei-embed tei-rerank` (ils tournent sous vLLM en mode « pooling », non validé sur Spark) |
| `[ECHEC] appel d'outil` | Le parser d'outils ne correspond pas au modèle : `VLLM_TOOL_PARSER` dans `.env` |
| `Permission denied` en écrivant dans le dossier des données | Un sous-dossier a été créé par Docker au nom de `root`. `sudo chown -R $USER /home/data/ws/models` (et le dossier concerné), ou relance `preparer-env-spark.sh` sur un `.env` supprimé |
| Autre | Section 9 du GUIDE |

**Pour me transmettre un problème :**
```bash
./scripts/diagnostic-spark.sh
```
Il écrit un fichier `diagnostic-spark-….txt` (état du GPU, de la mémoire, des conteneurs, réglages vLLM, derniers journaux). Il ne contient pas les secrets, mais relis-le avant de l'envoyer.

## 9. Ce qui n'est pas validé (à surveiller)

1. Le lancement de `vllm` dans le conteneur NGC (sa présence en ARM64 est vérifiée, pas son exécution).
2. Les embeddings et le reranker servis par vLLM sur la puce GB10 (la configuration n'a jamais tourné sur un Spark).
3. Le certificat HTTPS interne de Caddy avec ton nom de serveur.
4. Le panneau web, corrigé pour la mémoire unifiée mais jamais lancé sur un Spark.

## 10. Essayer d'autres modèles (après le premier succès)

```bash
./scripts/modeles.py lister                              # profils, poids téléchargés ou non, modèle actif
./scripts/modeles.py utiliser bolt-74b-a9b-24gb          # télécharge si besoin, met .env à jour, redémarre vLLM
./scripts/02-verifier-spark.sh                           # compare le débit à celui du tableau NVIDIA
```
Les débits mesurés par NVIDIA (une requête, code) : Lightning 144, Qwen3.6-35B 124, Bolt 89, Super VL 51, Flash Next 38, Qwen3.8 27B 37 tok/s. Un seul modèle est chargé à la fois ; changer de modèle coupe le service pendant le chargement (GUIDE, section 8). Pour revenir au modèle par défaut : `./scripts/modeles.py utiliser qwen3.6-35b-a3b-nvfp4`.

## 11. Arrêter ou revenir en arrière

```bash
docker compose stop        # arrête sans rien perdre
docker compose down        # supprime les conteneurs ; les données (comptes, documents indexés) sont conservées
```
