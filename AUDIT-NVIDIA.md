# Audit : ma stack face aux tutoriels NVIDIA pour le DGX Spark

Date : 21/10/2026. Branche `spark`. Aucun test n'a été fait sur un vrai Spark : cet audit compare des fichiers, pas des mesures sur ta machine.

## 1. Ce que NVIDIA fournit, et ses limites

| Source | Contenu | Remarque |
|---|---|---|
| [Playbook « vLLM on One Device »](https://github.com/NVIDIA/dgx-spark-playbooks/tree/main/nvidia/playbook-vllm) | Lancer vLLM sur un Spark, tester l'API et les appels d'outils | Un seul utilisateur, accès par tunnel SSH ou NVIDIA Sync |
| [Playbook « Open WebUI »](https://github.com/NVIDIA/dgx-spark-playbooks/tree/main/nvidia/playbook-open-webui) | Le chat web | Repose sur **Ollama**, pas sur vLLM |
| [Playbook « CLI Coding Agent »](https://github.com/NVIDIA/dgx-spark-playbooks/tree/main/nvidia/playbook-cli-coding-agent) | OpenCode, Claude Code, Codex | Repose aussi sur **Ollama** (`ollama launch opencode`) |
| [Recettes « Local AI Inference »](https://github.com/NVIDIA/local-ai-inference-recipes) | Une recette par modèle et par machine, **validée et mesurée** (débit inclus) | La référence la plus précise pour les réglages |

Aucun tutoriel NVIDIA ne décrit une installation **pour une équipe** (clés par personne, HTTPS, vLLM derrière LiteLLM). Les écarts d'architecture ci-dessous sont donc voulus. Les écarts de **réglages vLLM** sont, eux, à corriger.

NVIDIA précise que ses recettes ont été « créées avec l'aide d'une IA » et doivent être validées avant usage. Elles restent la meilleure base disponible.

## 2. Bilan

| Domaine | Verdict |
|---|---|
| Réglages vLLM du modèle par défaut | 5 écarts avec la recette validée par NVIDIA |
| Image Docker | Écart : NVIDIA valide son conteneur NGC, pas l'image `vllm/vllm-openai` |
| Isolation, mémoire partagée, caches | Il manque 3 réglages que NVIDIA met dans toutes ses recettes |
| Profils des autres modèles | Les 5 profils qui ont une recette NVIDIA s'en écartent (dont 1 seulement par un tag d'image) |
| Panneau web | **Bug probable sur le Spark** |
| OpenCode | Limite de sortie trop basse |
| Sécurité, multi-utilisateurs, hors ligne | En avance sur les tutoriels NVIDIA |

## 3. Écarts à corriger

### 3.1 Bug probable du panneau sur le Spark
NVIDIA précise que sur le Spark `nvidia-smi --query-gpu` renvoie `N/A` pour les champs mémoire (mémoire unifiée). `panneau/server.py` convertit ces champs avec `int(...)` : d'après la lecture du code, cela lève une erreur et la page d'état du panneau ne s'affiche plus (non vérifié sur un Spark).
**Correction :** accepter `N/A` et afficher la mémoire unifiée depuis `/proc/meminfo`.

### 3.2 Réglages absents de la base Spark
| Réglage | NVIDIA (toutes les recettes) | Ma stack |
|---|---|---|
| `--shm-size=16g` | oui | non |
| `--ulimit memlock=-1` et `stack=67108864` | oui | non |
| Caches persistants `~/.cache/vllm`, `flashinfer`, `~/.triton` | oui | seulement dans le profil Flash Next |

Sans les caches, chaque recréation du conteneur recompile les noyaux, ce qui rallonge le démarrage. NVIDIA indique que le premier démarrage peut inclure téléchargements, compilation et réglage des noyaux.
**Correction :** les ajouter à `docker-compose.spark.yml` pour tous les modèles.

### 3.3 Modèle par défaut : Qwen3.6-35B-A3B NVFP4
Recette NVIDIA validée le 19/10/2026 (mesurée à **124 tok/s** en génération de code).

| Paramètre | NVIDIA | Ma stack |
|---|---|---|
| Image | `nvcr.io/nvidia/vllm:26.10.pre1-py3` (vLLM 0.31, build NVIDIA) | `vllm/vllm-openai:v0.30.0` |
| `--tool-call-parser` | `qwen3_xml` | `qwen3_coder` |
| `--gpu-memory-utilization` | 0.8 | 0.5 |
| Jetons spéculatifs MTP | 4 | 3 |
| `--load-format` | `safetensors` | `fastsafetensors` |
| Identique | `--kv-cache-dtype fp8`, FlashInfer, Marlin, 262144, 8 séquences, 8192 jetons par lot, chunked prefill, prefix caching, parser de raisonnement `qwen3` | idem |

Le conteneur NGC est public et existe en ARM64 (vérifié). Le parser d'outils compte : une erreur de format peut casser les appels d'outils d'OpenCode.
À propos du 0.8 : avec les embeddings (0.08), l'ensemble donne 0.88, ce qui correspond à ton objectif de ~90 %.

### 3.4 Autres profils
| Profil | Écart avec la recette NVIDIA |
|---|---|
| `bolt-74b-a9b-24gb` | **Trop compliqué.** NVIDIA utilise `eugr/spark-vllm:nightly-20261015`, sans `b12x` ni plugin, avec `--load-format instanttensor`, parser `nemotron_v3` et brouillon `LiLiCorr` (DFlash, 7 jetons). Mon Dockerfile et mon image figée sont inutiles. NVIDIA mesure **89 tok/s** |
| `qwen3.8-flash-next-fp4-local` | NVIDIA fige l'image en `nightly-20261015` ; j'ai mis `latest`. Le reste est identique |
| `nemotron-3.5-super-vl-120b-a12b-nvfp4` | NVIDIA : image `eugr/spark-vllm:latest`, brouillon `LiLiCorr` (DFlash), options Mamba, `instanttensor`. Mon profil : vLLM officiel, sans brouillon |
| `qwen3.8-27b-nvfp4` | NVIDIA : brouillon DFlash `z-lab/Qwen3.8-27B-DFlash2`, `--gpu-memory-utilization 0.60`, réglages de noyaux. Mon profil : MTP à 5 jetons, 0.7, image `qwen38` |
| `nemotron-3.5-lightning-30b-a3b-nvfp4` | NVIDIA : image `eugr` figée, brouillon DSpark (7 jetons), parser `qwen3_xml`. **144 tok/s** mesurés. Mon profil : MTP, parser `qwen3_coder` |

### 3.5 OpenCode : limite de sortie à 4096
`clients/opencode.json` limite la sortie à 4096 jetons. Les modèles à raisonnement dépensent leur budget à réfléchir : NVIDIA signale des réponses vides (`content: null` avec `finish_reason: length`) quand `max_tokens` est épuisé. Ses tests de débit génèrent 8192 jetons.
**Correction :** relever la limite à 16384 ou 32768.

## 4. Vitesse mesurée par NVIDIA (génération de code, 1 requête)

| Modèle | Débit | Mémoire utilisée |
|---|---|---|
| Nemotron 3.5 Lightning 30B-A3B | 144 tok/s | 18 Go |
| Qwen3.6-35B-A3B | 124 tok/s | 21 Go |
| Bolt 74B-A9B | 89 tok/s | 24 Go |
| Nemotron 3.5 Super VL 120B-A12B | 51 tok/s | 91 Go |
| Qwen3.8 Flash Next | 38 tok/s | ~100 Go |
| Qwen3.8 27B (dense) | 37 tok/s | 32 Go |

**Conséquence :** utiliser 90 % de la mémoire n'est pas un gain de vitesse. Les modèles de 90 à 100 Go sont trois fois plus lents que le modèle par défaut. Bolt est le meilleur compromis annoncé : un grand modèle (74 Md) pour 24 Go, à 89 tok/s. Ces chiffres mesurent une seule requête ; à 8 utilisateurs, le débit par personne baisse.

## 5. Là où la stack va plus loin que les tutoriels

| Sujet | Tutoriels NVIDIA | Ma stack |
|---|---|---|
| Accès | `--network host`, API sans authentification, tunnel SSH | Réseau isolé, HTTPS (Caddy), clé par personne (LiteLLM) |
| Utilisateurs | Un seul | Plusieurs, avec quotas et suivi |
| Hors ligne | Téléchargement depuis Hugging Face | Modèles locaux, `HF_HUB_OFFLINE=1` |
| Versions | Tags `latest` fréquents | Images figées (sauf Flash Next) |
| Recherche dans les normes | Absente | Qdrant, reranker, MCP |
| Vérification | Commandes `curl` à la main | `02-verifier-spark.sh` |

NVIDIA le dit lui-même : une API exposée en `0.0.0.0` sans contrôle est une configuration d'évaluation.

## 6. Écarts volontaires, à garder
- **vLLM au lieu d'Ollama** : les playbooks Open WebUI et OpenCode utilisent Ollama. vLLM gère mieux plusieurs utilisateurs en parallèle.
- **Open WebUI sans Ollama intégré** : NVIDIA utilise l'image `open-webui:ollama`. La mienne est l'image standard, branchée sur LiteLLM.
- **Pas de NVIDIA Sync** : l'accès passe par Caddy pour toute l'équipe.
- **Contexte** : NVIDIA recommande au moins 32K, idéalement 64K ou plus, pour les agents. Mes profils sont à 131K ou 262K.

## 7. Plan d'action par priorité
1. Corriger le panneau (3.1).
2. Ajouter `shm_size`, `ulimits` et les caches à la base Spark (3.2).
3. Aligner le modèle par défaut sur la recette NVIDIA : image NGC, `qwen3_xml`, 0.8, MTP 4 (3.3).
4. Remplacer le profil Bolt par la recette NVIDIA et supprimer `services/vllm-bolt/` (3.4).
5. Fixer les images de Flash Next, Super VL, Qwen3.8 27B et Lightning (3.4).
6. Relever la limite de sortie d'OpenCode (3.5).
7. Au premier démarrage sur le Spark : `./scripts/02-verifier-spark.sh`, puis comparer le débit à la colonne « Débit » du tableau 4.

## 8. Ce que cet audit ne couvre pas
- Aucune mesure sur un vrai Spark.
- Le conteneur NGC n'a pas été lancé : seule sa présence en ARM64 est vérifiée.
- Les recettes NVIDIA n'ont pas été relues ligne à ligne pour les options non listées ici.
- Les pilotes : les recettes ont été validées avec le pilote 580.x et CUDA 13.x sous Ubuntu 24.04 (DGX OS). À comparer avec `nvidia-smi` sur le Spark.
