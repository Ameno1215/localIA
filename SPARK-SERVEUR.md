# Un Spark pour le modèle, un serveur pour le reste

> ⚠ **Pas encore testé.** Les fichiers sont vérifiés (rendu `docker compose config`), mais rien n'a tourné sur cette répartition.

Le DGX Spark ne fait plus tourner que **vLLM**. Tout le reste va sur un serveur x86 ordinaire du réseau, sans GPU : Caddy (HTTPS), LiteLLM et Postgres, Open WebUI, Qdrant, le serveur MCP des normes, embeddings et reranker (TEI sur CPU, comme sur le PC de test).

```
Postes ──HTTPS 443/8443──> Serveur : Caddy, LiteLLM, Postgres, Open WebUI, Qdrant, MCP, TEI (CPU)
                                │  HTTP :8000 + VLLM_API_KEY
                                ▼
                          DGX Spark : vLLM seul (modèle actuel, qwen-local)
```

**Pourquoi :** la mémoire du Spark est unifiée, donc tout ce qui tourne à côté du modèle lui prend de la place. Aujourd'hui, la pile complète occupe ~30 Gio au démarrage de vLLM (section 8 du GUIDE), d'où `VLLM_GPU_UTIL=0.74`. Les embeddings et le reranker, servis par deux vLLM sur le GPU, en prennent à eux seuls ~10 Go réservés plus leurs processus. Gain attendu : **15 à 20 Go** rendus au modèle (estimation, à mesurer avec `docker stats` et `free -g`).

Les clients ne voient qu'un changement d'adresse : ils visent le serveur au lieu du Spark. Le nom du modèle (`qwen-local`), les clés et les outils restent les mêmes.

## Le serveur

N'importe quelle machine x86_64 Linux du réseau avec Docker et Docker Compose. Il faut au moins 4 cœurs, 16 Go de RAM et de la place pour les documents et les bases. Pas de GPU : les embeddings sur CPU ralentissent l'indexation, pas la recherche. Le PC de test convient.

## 1. Préparer le Spark

1. **Arrête la pile actuelle :** `docker compose down`. Les données restent dans `WS_DIR`.
2. **Dans le `.env` du Spark :**
   ```bash
   COMPOSE_FILE=docker-compose.yml:docker-compose.spark.yml:docker-compose.spark-inference.yml
   VLLM_API_KEY=<openssl rand -hex 16>
   ```
   `docker-compose.spark-inference.yml` met tous les services sauf vLLM dans un profil jamais activé sur le Spark. `docker compose up -d`, le panneau web et `./scripts/modeles.py utiliser` ne démarrent donc plus que vLLM. Il publie aussi le port 8000 sur le réseau et **refuse de démarrer sans `VLLM_API_KEY`**.
3. **Restreins le port 8000 au serveur.** Docker publie ses ports en passant devant `ufw` : la règle se met donc dans la chaîne `DOCKER-USER`.
   ```bash
   sudo iptables -I DOCKER-USER -p tcp --dport 8000 ! -s <ip-du-serveur> -j DROP
   sudo apt install iptables-persistent && sudo netfilter-persistent save   # pour qu'elle survive au redémarrage
   ```
4. **Facultatif :** désactive le bureau graphique (1 à 3 Go de plus) : `sudo systemctl set-default multi-user.target`, puis redémarre.
5. **Démarre :** `docker compose up -d`, puis `docker compose logs -f vllm`.

Le modèle et son profil ne changent pas. Une fois tout validé, monte `VLLM_GPU_UTIL` dans le `.env` par paliers (0.80, puis 0.85) en vérifiant que le Spark reste réactif. Relance ensuite avec `docker compose up -d --force-recreate vllm`. Un `./scripts/modeles.py utiliser …` réécrit cette valeur avec celle du profil.

## 2. Préparer le serveur

1. **Clone le projet** (branche `spark`) et prépare la configuration :
   ```bash
   ./scripts/preparer-env-spark.sh <nom-du-serveur> /home/data/ws
   ```
2. **Dans le `.env` du serveur :**
   ```bash
   COMPOSE_FILE=docker-compose.yml:docker-compose.serveur.yml
   VLLM_API_BASE=http://<nom-ou-ip-du-spark>:8000/v1
   VLLM_API_KEY=<la même clé que sur le Spark>
   ```
   `SPARK_HOSTNAME` est le nom du **serveur** : c'est lui que les postes et les navigateurs joignent.
3. **Reprends les secrets du Spark** pour garder les clés et les comptes valides : copie `LITELLM_MASTER_KEY`, `POSTGRES_PASSWORD`, `WEBUI_SECRET_KEY` et `MCP_TOKEN` depuis le `.env` du Spark.
4. **Copie les données** depuis le `WS_DIR` du Spark (pile arrêtée) vers celui du serveur :
   ```bash
   rsync -a <spark>:/home/data/ws/{postgres,open-webui,qdrant,docs-internes,data} /home/data/ws/
   rsync -a <spark>:/home/data/ws/models/{bge-m3,bge-reranker-v2-m3} /home/data/ws/models/
   ```
   `postgres/` porte les clés LiteLLM de chacun, `open-webui/` les comptes et conversations.
5. **Démarre :** `docker compose up -d`. Le service `vllm` n'existe pas sur le serveur.
6. **Réindexe les normes** (mêmes modèles d'embeddings, mais servis par TEI au lieu de vLLM : on repart propre). Utilise le panneau web du serveur, « Indexer », ou `docker compose exec mcp-docs python ingest.py --reset`.
7. **Vérifie** depuis le serveur : `./scripts/02-verifier-spark.sh` (chat, outils, débit, embeddings, reranker).

## 3. Postes et navigateurs

- **Certificat :** Caddy génère une nouvelle autorité interne sur le serveur. Récupère-la (`docker compose cp caddy:/data/caddy/pki/authorities/local/root.crt ./serveur-ca.crt`) et distribue-la comme celle du Spark (section 8 du GUIDE).
- **OpenCode :** relance l'installation avec l'adresse du serveur. Les clés personnelles restent valides.
  ```bash
  ./scripts/installer-opencode.sh --serveur https://<serveur> --contexte 262144 --mcp https://<serveur>/mcp --jeton-mcp <jeton> --ca ./serveur-ca.crt
  ```
- **Chat web :** `https://<serveur>:8443`, avec les mêmes comptes.

## Revenir en arrière

Sur le Spark, retire `docker-compose.spark-inference.yml` de `COMPOSE_FILE`, puis lance `docker compose up -d` : la pile complète repart avec ses données, qui n'ont pas bougé. Arrête le serveur.

## Plus tard : deux Spark

Le serveur ne change pas. Soit LiteLLM reçoit une deuxième entrée `qwen-local` pointant vers le Spark 2 (deux copies du modèle, deux fois plus de requêtes servies). Soit les deux Spark, reliés par le câble QSFP, se partagent un seul modèle plus gros (vLLM + Ray). Ce second cas fera l'objet d'une étape à part.
