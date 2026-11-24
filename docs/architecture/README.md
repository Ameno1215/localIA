# Architecture

Schémas et explications de ce qui tourne, où, et comment les briques se parlent. Quand la stack change, on met le schéma à jour dans la même PR.

| Page | Contenu | À jour au |
|---|---|---|
| [vue-ensemble.md](vue-ensemble.md) | La stack en duo sur deux DGX Spark : machines, services, flux d'une requête, piste du serveur annexe | 2026-11-16 |
| [cible-serveur-annexe.md](cible-serveur-annexe.md) | **Architecture cible** : serveur applicatif x86 + DGX Spark réservé au modèle. Flux réseau, composants, sécurité, montée en charge. Version mise en page : [plateforme-cible.html](plateforme-cible.html) | 2026-11-16 |
| [agent-opencode.md](agent-opencode.md) | Comment l'agent de code OpenCode fonctionne avec notre modèle : boucle agentique, appels d'outils, choix des fichiers, contexte envoyé, parsers vLLM, limites de contexte et compaction | 2026-11-20 |

Idées de pages à venir : le RAG en détail (indexation, recherche, reranking), l'observabilité (journaux, Prometheus, ce qu'on mesure), le passage à 4 Spark.
