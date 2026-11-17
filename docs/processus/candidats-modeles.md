# Candidats : modèles et configurations (étape 1)

> **Date** : 2026-11-17 · **Auteur** : Antonin · **Statut** : brouillon, à compléter

Tableau de travail de l'étape 1 du [processus de benchmark](benchmark-modeles.md). Il est prérempli avec les profils déjà présents dans `config/modeles-spark/` (chiffres repris de leurs commentaires) ; les colonnes de benchmarks publiés restent à remplir, avec la source et les conditions de chaque chiffre.

Légende du nombre de Spark : le minimum pour que le modèle tienne avec de la place pour le cache KV.

## Rôle « gros » : seulement à 4 Spark (famille A)

| Modèle | Total / actifs | Quantif. | Poids | Spark min. | Recette Spark | Licence | Débit publié | Benchmarks publiés (source, conditions) | Statut |
|---|---|---|---|---|---|---|---|---|---|
| Nemotron 3 Ultra | 550 Md / ? | ? | ? | 4 ? | ? | ? | ? | | à vérifier : taille quantifiée, recette multi-Spark |
| _à compléter_ | | | | | | | | | |

## Rôle « moyen » : 1 à 3 Spark (familles B, C, D)

| Modèle | Total / actifs | Quantif. | Poids | Spark min. | Recette Spark | Licence | Débit publié | Benchmarks publiés (source, conditions) | Statut |
|---|---|---|---|---|---|---|---|---|---|
| Qwen3.8 Flash Next | 125 Md / 6 Md | NVFP4 | ~133 Go | 2 | Communautaire 2 Spark (MiaAI-Lab) | ? | 45,7 tok/s (1 req.), 174 tok/s à 8 req. (auteur de la recette) | Aider polyglot 74,2 % (auteur de la recette) | Profil `qwen3.8-flash-next-nvfp4-duo`, à l'essai en duo |
| Qwen3.8 Flash Next FP4-Local | 125 Md / 6 Md | FP4 local | ~103 Go | 1 (tout juste) | NVIDIA, 15/10/2026 | ? | 37,8 tok/s | GPQA Diamond 91,5 % (BF16 : 92,2 %), ToolEvalBench 88,7 % (NVIDIA) | Profil `qwen3.8-flash-next-fp4-local`, non testé |
| Nemotron 3.5 Super VL | 120 Md / 12 Md | NVFP4 | 75 Go | 1, mieux à 2 | NVIDIA, 15/10/2026 | ? | 51 tok/s (1 Spark, NVIDIA) ; ~28 tok/s mesuré en duo sans spéculatif | | Profil actuel par défaut, validé en duo le 2026-11-09 |
| Ornith 1.5 122B | 122 Md / ? | NVFP4 | ~70 Go | 1 | Aucune | MIT | ? | SWE-bench Verified 84,5, TerminalBench 2.1 81,2 (éditeur, **non vérifiés**) | Expérimental |
| DeepSeek V4-Flash | ? | ? | ? | ? | ? | ? | ? | | Challenger cité, à documenter |
| _à compléter_ | | | | | | | | | |

## Rôle « petit et rapide » : 1 Spark, ou à côté d'un autre modèle

| Modèle | Total / actifs | Quantif. | Poids | Spark min. | Recette Spark | Licence | Débit publié | Benchmarks publiés (source, conditions) | Statut |
|---|---|---|---|---|---|---|---|---|---|
| Nemotron 3.5 Lightning | 30 Md / 3 Md | NVFP4 | ~18 Go | 1 (partageable) | NVIDIA, 20/10/2026 | ? | 144 tok/s (NVIDIA) | | Profil `nemotron-3.5-lightning-30b-a3b-nvfp4` |
| Qwen3.6-35B-A3B | 35 Md / 3 Md | NVFP4 | ? | 1 (partageable) | NVIDIA, 19/10/2026 | ? | 124 tok/s (NVIDIA) | | Ancien modèle par défaut |
| Qwen3.6-35B-A3B | 35 Md / 3 Md | FP8 | ? | 1 | recipes.vllm.ai (GB10 vérifié) | ? | ? | | Profil `qwen3.6-35b-a3b-fp8` |
| Nemotron 3.5 Bolt | 74 Md / 9 Md | ~2 bits (experts) | 24 Go | 1 (partageable) | NVIDIA, 15/10/2026 | ? | 89 tok/s (NVIDIA) | | Conçu pour les agents locaux |
| Qwen3.8-27B | 27 Md (dense) | NVFP4 | ? | 1 | NVIDIA, 19/10/2026 | ? | 37 tok/s (NVIDIA) | | Dense : plus lent que les MoE ; plutôt « moyen » en qualité |
| _à compléter_ | | | | | | | | | |

## Configurations à évaluer

Une ligne par combinaison qui a un sens sur 4 Spark. À remplir une fois la liste courte des modèles connue.

| Config. | Famille | Spark 1 | Spark 2 | Spark 3 | Spark 4 | Remarques |
|---|---|---|---|---|---|---|
| A1 | A | G (TP4) | G | G | G | |
| B1 | B | M (TP2) | M | P1 | P2 | |
| B2 | B | M (TP2) | M | P1 | P1 (réplique) | |
| C1 | C | M+ (PP3) | M+ | M+ | P | Vérifier que vLLM accepte la répartition sur 3 |
| D1 | D | M1 (TP2) | M1 | M2 (TP2) | M2 | |

## Sources à consulter

- Fiches des modèles sur Hugging Face (benchmarks de l'éditeur, conditions souvent en bas de page).
- Recettes NVIDIA pour le DGX Spark (github.com/NVIDIA/local-ai-inference-recipes) : débit mesuré et parfois précision quantifiée vs BF16.
- recipes.vllm.ai : statut de compatibilité GB10.
- Classements publics indépendants (agrégateurs de benchmarks, LMArena) : utiles pour croiser les chiffres des éditeurs.
- `./scripts/modeles.py nouveautes` et `chercher` : modèles récents publiés par NVIDIA et les autres éditeurs.
