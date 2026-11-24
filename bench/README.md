# Suite de tests de charge et de limites

Cette suite automatise le plan de test du doc « Benchmark LLM — Cluster 4× DGX Spark », phases 0 à 7, jusqu'au tableau go / no-go. Elle tourne sur le PC de test (profil `pc-test`) comme sur la grappe Spark (profil `spark`).

Elle n'utilise que la bibliothèque standard de Python 3.11 ou plus récent : rien à installer, elle tourne hors ligne. Les commandes se lancent **depuis la racine du dépôt**.

```bash
python3 -m bench --help
```

## Avant de commencer

1. **Choisis le profil** dans [config.toml](config.toml). `pc-test` est le profil par défaut. Pour la grappe, passe `--profil spark` ou exporte `BENCH_PROFIL=spark`, après avoir adapté les noms d'hôtes et les noms de modèles LiteLLM (`code-agent`, `fast`, `vision`).
2. **La clé** est lue dans `BENCH_CLE`, sinon dans `LITELLM_MASTER_KEY` (environnement, puis `.env`).
3. **Mets en place Prometheus et Grafana** avant la phase 1, comme le demande le plan. La suite lit elle-même `/metrics` sur chaque vLLM listé dans `metriques`, ce qui donne l'usage du cache KV, les préemptions et le taux de cache de préfixe.
4. **Calibre le générateur de prompts**, une fois par modèle, pour que les longueurs visées (4K, 32K, 64K tokens) soient justes :
   ```bash
   python3 -m bench calibrer
   ```

## Les phases

| Phase | Commande | Durée (Spark) | Ce qui est mesuré |
|---|---|---|---|
| 0 Réseau | `NOEUDS="spark1 spark2 spark3 spark4" IB_DEV=… IFACE=… ./bench/reseau.sh` | ½ j | `ib_write_bw` par paire, NCCL `all_reduce_perf` sur 3 essais, pertes RoCE, relevé des nœuds |
| 1 Capacité | `python3 -m bench capacite` | 1 j / modèle | Genou de chaque modèle en direct sur vLLM, modes `froid` et `prefixe`, à 4K / 32K / 64K |
| 2 Charge mixte | `python3 -m bench charge --creer-cles` | 1–2 j | 10 → 20 → 30 → 45 développeurs virtuels à travers LiteLLM, contexte de 10K à 80K, temps de réflexion |
| 3 RAG | `python3 -m bench rag` (voir plus bas) | ½ j | Latence p95 des embeddings, du rerank sur 50 candidats et de Qdrant ; rappel@5 avec et sans reranker |
| 4 Endurance | `python3 -m bench charge --endurance 8 --devs 21` + sondes | 1 nuit | Dérive de latence par rapport à la 1re heure ; fréquence, ACPI, mémoire, extinctions |
| 5 Pannes | `python3 -m bench pannes` | ½ j | Durée d'impact vue par les clients, erreurs, modèle de repli |
| 6 Limites | `python3 -m bench limites` | ½ j | Prompt trop long, clé gourmande (429), `max_tokens` absent, appels d'outils mal formés |
| 7 Qualité | `python3 -m bench qualite [--vision]` | 2–3 j | Tickets internes résolus par OpenCode, contrôles C, questions vision |
| — | `python3 -m bench rapport` | — | Tableau go / no-go à partir du dernier résultat de chaque phase |

Les résultats (JSON) et les rapports (Markdown) vont dans `bench/resultats/`, qui n'est pas versionné. Chaque commande accepte `--help`. Les durées et les paliers viennent de la config et se surchargent en ligne de commande, par exemple `--niveaux 1,4,16 --duree 60`.

### Phase 1 : à quoi sert chaque mode

- **`froid`** : chaque requête envoie un prompt unique. C'est le pire cas, celui du premier tour sur un gros dépôt.
- **`prefixe`** : chaque client garde un historique fixe et n'ajoute qu'un suffixe de 2K tokens, comme une session OpenCode.

Pour comparer avec et sans cache de préfixe, relance vLLM sans `--enable-prefix-caching`, puis refais `--mode prefixe`.

Un palier « tient » quand trois conditions sont réunies :
- le taux d'erreur reste sous l'objectif ;
- la vitesse p95 de la requête la plus lente atteint `vitesse_min` du rôle ;
- le TTFT p95 respecte l'objectif (3 s en préfixe ; en froid, proportionnel à la longueur, jusqu'à 45 s à 64K).

### Phase 2 : les clés

`--creer-cles` crée une clé LiteLLM `bench-vdev-NN` par développeur virtuel, avec `max_parallel_requests` pris dans la config, puis les supprime à la fin. Sans cette option, tout le monde partage la clé maître et l'équité entre clés n'est pas testée. `--cles fichier.txt` permet de fournir ses propres clés.

### Phase 3 : la lancer dans le réseau docker

Sur le PC de test, TEI et le reranker n'ont pas de port publié. On lance donc la phase avec l'image du serveur MCP :

```bash
docker compose run --rm --no-deps -v ./bench:/b/bench -w /b mcp-docs python -m bench rag
```

Les questions du rappel@5 sont dans [donnees/questions_rag.jsonl](donnees/questions_rag.jsonl). Les exemples portent sur la norme fictive du dépôt ; il faut les remplacer par une trentaine de questions dont la réponse se trouve sur une page connue de vos datasheets.

### Phase 4 : sondes thermiques

Lance une sonde par nœud **depuis une autre machine**, pour que le journal survive à une extinction :

```bash
ssh spark1 python3 - --periode 10 < bench/sonde_noeud.py > bench/resultats/sonde-spark1.csv
```

Puis analyse les journaux :

```bash
python3 -m bench thermique bench/resultats/sonde-*.csv
```

### Phase 5 : pannes

Chaque scénario de la config affiche sa consigne : tu provoques la panne et tu appuies sur Entrée. Avec `--executer`, la suite lance elle-même la `commande` du scénario, quand il en a une (sur le PC de test : arrêt de vLLM, redémarrage de LiteLLM). Passe `--cle <clé utilisateur>` pour vérifier qu'une vraie clé survit au redémarrage de LiteLLM et de Postgres.

### Phase 7 : tickets internes

Chaque dossier de [donnees/taches/](donnees/taches/) contient :
- un `tache.toml` avec la consigne, la commande de vérification, les fichiers protégés et les contrôles ;
- un `depot/` avec le code de départ.

[tampon-circulaire](donnees/taches/tampon-circulaire/) est un exemple en C. Ajoute 15 à 20 tickets clos de votre historique : pilotes SPI ou I2C, ISR, portage, bug de timing.

Pour chaque ticket, la suite :
1. copie le dépôt dans un dossier temporaire ;
2. lance `opencode run --auto` ;
3. remet les fichiers protégés à leur état d'origine (un agent qui modifie les tests est signalé) ;
4. décide de la réussite avec `verifier`, puis avec les `controles` (absence de `malloc`, cppcheck…).

⚠ L'agent exécute ses commandes sans demander : ne pointe jamais `taches` vers un vrai dossier de travail.

## Tester la suite elle-même

Un faux serveur imite vLLM, LiteLLM, TEI et Qdrant, ce qui permet de valider toutes les phases sans GPU (environ 1 minute) :

```bash
python3 -m unittest bench.tests.test_bench -v
```
