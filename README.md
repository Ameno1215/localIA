# IA locale

Une plateforme d'IA qui tourne entièrement sur nos machines :
- **un agent de code** façon Claude Code (OpenCode) ;
- **une recherche dans les normes internes** (RAG) ;
- **un chat web** avec des comptes utilisateurs.

Elle tourne aujourd'hui sur un PC de test (RTX 8 Go) et est prête à passer sur le DGX Spark.

📖 **Toute la documentation est dans [GUIDE.md](GUIDE.md)** : fonctionnement, utilisation, passage au Spark, dépannage.

| Fichier | Pour quoi faire |
|---|---|
| [GUIDE.md](GUIDE.md) | Comprendre et utiliser la plateforme |
| [SPARK-PREMIER-DEMARRAGE.md](SPARK-PREMIER-DEMARRAGE.md) | **Tester sur le DGX Spark**, pas à pas (branche `spark`) |
| [SPARK-DUO.md](SPARK-DUO.md) | Relier deux Spark pour un modèle plus gros (validé le 09/11/2026). Voir aussi la section ci-dessous |
| [AUDIT-NVIDIA.md](AUDIT-NVIDIA.md) | Écarts avec les tutoriels et recettes officiels NVIDIA, et corrections faites |

## Démarrage rapide

```bash
./scripts/panneau.sh
```

1. **Ouvre le panneau** sur http://localhost:7000, puis clique sur « Mode agent de code ». Attends environ 2 minutes que `vllm` soit « En marche ».
2. **Code avec l'agent**, dans ton projet :
   ```bash
   cd mon-projet
   opencode
   ```
3. **Chatte dans le navigateur** sur http://localhost:3000.

## Première installation d'un poste

```bash
./scripts/creer-cle-utilisateur.sh prenom.nom
```

Crée la clé personnelle, côté serveur. Elle ne s'affiche qu'une fois : garde-la.

```bash
./scripts/installer-opencode.sh
```

Installe OpenCode et le configure. Le script demande la clé. Le détail est dans la section 4 du guide.

## Installation du serveur (une fois)

```bash
./scripts/00-prerequis.sh
```

Sur le PC de test : installe le driver NVIDIA, Docker compose, le NVIDIA Container Toolkit et Python (sudo, redémarrage ensuite). Sur le DGX Spark : n'installe rien (pilote et Docker sont déjà en place), il vérifie seulement et prépare l'outil de téléchargement.

```bash
./scripts/01-telecharger-modeles.sh
```

Télécharge les modèles (ajoute `--spark` sur le DGX Spark).

```bash
cp .env.example .env
docker compose up -d
```

Crée la configuration à partir du modèle (pense à changer les secrets), puis démarre la stack.

## État (PC de test au 12/10/2026 ; configuration Spark au 22/10/2026)

| Brique | État |
|---|---|
| Modèle local `qwen-local` (Qwen3.5-4B AWQ, 64K de contexte) | ✅ testé |
| Agent de code OpenCode (fichiers locaux, diffs, commandes, conversation suivie) | ✅ testé |
| Recherche dans les normes (RAG + MCP) | ✅ testé, avec la norme d'exemple seulement |
| Clés et quotas par personne (LiteLLM) | ✅ testé |
| Panneau web de contrôle | ✅ testé |
| Open WebUI | ⚠️ prêt, compte admin à créer |
| Fine-tuning LoRA `normes` | ⏳ scripts prêts, pas encore lancé |
| Configuration DGX Spark (branche `spark`, modèle Nemotron 3.5 Super VL 120B) | ⏳ prête et vérifiée sur PC (rendu Docker, scripts), **jamais lancée sur un Spark** : voir [SPARK-PREMIER-DEMARRAGE.md](SPARK-PREMIER-DEMARRAGE.md) |

## Tester le duo (2 Spark) à côté de l'installation 1 Spark

Garder le dossier actuel (branche `spark`) intact, et cloner le dépôt une seconde fois sur la branche `spark-duo`. Le pas-à-pas complet (câble, pare-feu, copies, démarrage, dépannage) est dans [SPARK-DUO.md](SPARK-DUO.md), validé sur les deux Spark le 09/11/2026. En bref, dans le dossier duo, seuls `.env` et `.env.vllm` diffèrent :

| Réglage | Dossier 1 Spark | Dossier duo, Spark 1 | Dossier duo, Spark 2 |
|---|---|---|---|
| Branche | `spark` | `spark-duo` | `spark-duo` |
| `COMPOSE_FILE` | `docker-compose.yml:docker-compose.spark.yml` | `…:docker-compose.spark-duo.yml` ajouté | `docker-compose.spark-worker.yml` |
| `WS_DIR` | chemin absolu des données | **le même chemin absolu** | celui du Spark 2 (son compte de service, son chemin) |
| `VLLM_IMAGE` | celle du profil | une image **avec Ray** (`eugr/spark-vllm`) | la même |
| `VLLM_GPU_UTIL` | celle du profil (0.74) | **0.68** | la même |
| `VLLM_API_KEY` | vide | une clé | la même clé |
| `DUO_*` (interface, carte, IP **du câble**) | absents | remplis | identiques au Spark 1 |
| Secrets, `.env.vllm` | les vôtres | copiés à l'identique | copiés à l'identique |

Un seul des deux dossiers tourne à la fois : `docker compose down` dans l'un avant `docker compose up -d` dans l'autre (mêmes ports, mêmes bases de données).
