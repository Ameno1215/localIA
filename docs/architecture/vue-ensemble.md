# Vue d'ensemble de la stack

> **Date** : 2026-11-16 · **Statut** : à jour (branche `spark-duo`)

## En une phrase

Les postes parlent à **Caddy** en HTTPS sur le Spark 1. Caddy envoie le chat et l'agent de code vers **LiteLLM** (clés, quotas, journal), qui appelle le modèle servi par **vLLM**, réparti sur les deux Spark reliés par un câble QSFP. La recherche dans les normes passe par le serveur **MCP docs** (Qdrant + embeddings + reranker).

## Machines et services

```mermaid
flowchart LR
    subgraph postes["Postes de l'équipe"]
        nav["Navigateur"]
        oc["OpenCode<br/>(agent de code)"]
    end

    subgraph s1["Spark 1 · tête"]
        caddy["Caddy<br/>443 : API + MCP<br/>8443 : Open WebUI"]
        owui["Open WebUI<br/>(chat web, comptes)"]
        litellm["LiteLLM<br/>(clés, quotas, routage)"]
        pg[("Postgres<br/>clés, dépenses, journaux")]
        mcp["MCP docs<br/>(recherche dans les normes)"]
        qdrant[("Qdrant<br/>index vectoriel")]
        emb["bge-m3<br/>embeddings"]
        rer["bge-reranker"]
        vllm1["vLLM · tête Ray<br/>moitié du modèle"]
        prom["Prometheus<br/>+ exporteurs GPU et machine"]
    end

    subgraph s2["Spark 2 · worker"]
        vllm2["ray-worker<br/>autre moitié du modèle"]
        exp2["Exporteurs GPU et machine"]
    end

    nav -->|"HTTPS :8443"| caddy
    oc -->|"HTTPS :443"| caddy
    caddy --> owui
    caddy -->|"/"| litellm
    caddy -->|"/mcp (jeton)"| mcp
    owui --> litellm
    owui --> mcp
    owui --> qdrant
    litellm --> pg
    litellm -->|"qwen-local, normes"| vllm1
    litellm -->|"bge-m3"| emb
    mcp --> qdrant
    mcp --> emb
    mcp --> rer
    vllm1 <==>|"câble QSFP<br/>NCCL en RDMA (TP=2)"| vllm2
    prom -.-> vllm1
    prom -.-> exp2
```

| Brique | Rôle | Où regarder |
|---|---|---|
| Caddy | Seule porte ouverte sur le réseau, HTTPS avec certificat interne | `config/Caddyfile.spark` |
| Open WebUI | Chat web avec comptes utilisateurs, outils MCP | `docker-compose.yml`, `services/open-webui/` |
| LiteLLM | API compatible OpenAI pour tous les clients : une clé par personne, quotas, journal des requêtes | `config/litellm.yaml` |
| Postgres | Base de LiteLLM | `docker-compose.yml` |
| MCP docs | Outil `rechercher_docs` : recherche hybride + reranking dans `docs-internes/` | `services/mcp-docs/` |
| Qdrant | Index vectoriel des normes | `docker-compose.yml` |
| bge-m3, bge-reranker | Embeddings et reranker, servis par vLLM en mode pooling sur le GPU (TEI n'existe pas en ARM64) | `docker-compose.spark.yml` |
| vLLM | Le modèle (profil actif dans `config/modeles-spark/`), réparti en tensor parallel sur les deux Spark avec Ray | `docker-compose.spark-duo.yml`, [SPARK-DUO.md](../../SPARK-DUO.md) |
| Prometheus | Métriques vLLM, GPU, machine, des deux Spark | `config/prometheus/` |

Point à retenir : en duo, vLLM tourne sur le **réseau de l'hôte** (obligatoire pour Ray et NCCL sur le câble). Les autres conteneurs le joignent par `host-gateway`.

## Une requête de l'agent de code

```mermaid
sequenceDiagram
    autonumber
    participant OC as OpenCode (poste)
    participant C as Caddy
    participant L as LiteLLM
    participant V as vLLM (Spark 1 + 2)
    participant M as MCP docs
    participant Q as Qdrant + embeddings + reranker

    OC->>C: POST /v1/chat/completions (clé perso)
    C->>L: relaie
    L->>L: vérifie clé et quota
    L->>V: requête vers qwen-local
    V-->>L: réponse (appel d'outil rechercher_docs)
    L-->>OC: réponse en streaming
    OC->>C: POST /mcp (jeton MCP)
    C->>M: rechercher_docs("…")
    M->>Q: recherche hybride puis reranking
    Q-->>M: extraits classés
    M-->>OC: extraits + liens vers les sources
    OC->>C: nouvelle requête avec les extraits
    C->>L: relaie
    L->>V: génération de la réponse finale
    V-->>OC: réponse (via LiteLLM et Caddy)
```

## Piste : le serveur annexe (non testé)

Idée : sortir tout ce qui n'est pas le modèle vers un PC ou serveur x86 sans GPU, pour laisser la mémoire des Spark à vLLM. Embeddings et reranker y repassent sur CPU (TEI). Voir `docker-compose.serveur.yml`, l'annexe de [SPARK-DUO.md](../../SPARK-DUO.md) et la page détaillée [cible-serveur-annexe.md](cible-serveur-annexe.md).

```mermaid
flowchart LR
    postes["Postes"] -->|HTTPS| srv

    subgraph srv["Serveur annexe (x86, CPU)"]
        caddy["Caddy"] --> litellm["LiteLLM + Postgres"]
        caddy --> owui["Open WebUI"]
        caddy --> mcp["MCP docs"]
        mcp --> rag["Qdrant + TEI CPU<br/>embeddings, reranker"]
    end

    subgraph sparks["Spark 1 + Spark 2"]
        vllm["vLLM seul<br/>(TP=2 sur le câble)"]
    end

    litellm -->|"réseau de l'équipe<br/>VLLM_API_BASE + clé"| vllm
```

## Questions ouvertes

- Passage à 4 Spark : un gros modèle et un ou deux petits, avec un indicateur de charge par modèle (#17) et un routage automatique selon la difficulté (#18).
- Le serveur annexe vaut-il le coup, ou le Spark 1 suffit-il avec `VLLM_GPU_UTIL=0.68` ?
