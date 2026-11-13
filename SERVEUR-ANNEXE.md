# Serveur annexe : seul vLLM sur les Spark

> ⚠ **Non testé.** Rien de ce document n'a encore tourné. Les fichiers Compose ont été vérifiés avec `docker compose config`,
> la copie de l'historique et des cibles Prometheus avec un faux SSH. À valider étape par étape, avec un retour arrière
> possible à tout moment (section 11).

But : sortir des Spark tout ce qui n'est pas le modèle, pour que leur mémoire aille au cache KV (contexte plus long,
plus de requêtes en parallèle, ou un modèle plus gros). `VLLM_GPU_UTIL` passe de **0.68 à environ 0.85** sur les deux
Spark : à peu près **+20 Gio par Spark**, donc +40 Gio de cache KV pour le duo.

```
Postes ──HTTPS──> Serveur annexe (x86, sans GPU) : Caddy, LiteLLM, Postgres, Open WebUI, Qdrant, MCP docs,
                  TEI sur CPU (embeddings + reranker), Prometheus, rapport, évaluation
                        │  http://IP_SPARK1:8000 + VLLM_API_KEY      ▲ métriques : IP_SPARK1/2 ports 9835, 9100
                        ▼                                            │
                  Spark 1 : vLLM (tête Ray) + exporteurs ══ câble QSFP ══ Spark 2 : Ray worker + exporteurs
```

**Vitesse des réponses.** Le débit de génération ne change pas (il dépend de la bande passante mémoire du Spark, pas de
la mémoire libre). Le réseau ajoute moins d'une milliseconde par requête. Le seul poste qui ralentit est le **reranker**,
qui tourne aujourd'hui sur le GPU du Spark (vLLM en mode pooling : TEI n'existe pas en ARM64) et passe sur le CPU du
serveur. Voir la section 8 pour le mesurer et le régler.

## 1. Le serveur

- Machine x86-64 Linux avec Docker et le plugin Compose, **IP fixe** sur le réseau de l'équipe (`192.168.8.x`).
- 16 Go de RAM minimum (Open WebUI, Postgres, Qdrant, deux TEI), 8 cœurs conseillés (le reranker sur CPU en profite).
- Disque : la taille de `WS_DIR` du Spark 1 **sans** `models/` et `cache/`, plus 25 Go (Prometheus, journaux, embeddings).
- Pas de GPU nécessaire.

## 2. Réglages

Valeurs d'exemple : Spark 1 `192.168.8.106`, Spark 2 `192.168.8.194` (réseau de l'équipe, **pas** le câble),
serveur `<IP_SERVEUR>` et compte `admin` (à remplacer).

| Où | Variable | Valeur |
|---|---|---|
| Spark 1 | `COMPOSE_FILE` | celle d'aujourd'hui **+** `:docker-compose.spark-annexe.yml` à la fin |
| Spark 1 | `ANNEXE_SSH` | `admin@<IP_SERVEUR>` : copie de l'historique et des cibles Prometheus, lancement de l'évaluation |
| Spark 1 | `ANNEXE_DOSSIER` | dossier du projet sur le serveur, ex. `/home/admin/data/ws/ia-locale-annexe` |
| Spark 1 | `IP_SPARK1`, `IP_SPARK2` | `192.168.8.106`, `192.168.8.194` |
| Spark 1 | `VLLM_GPU_UTIL_FIXE` | `0.80` pour le premier démarrage, puis `0.85` (section 7) ; remplace la valeur de chaque profil |
| Spark 1 et 2 | `VLLM_GPU_UTIL` | écrit par `modeles.py`, ne pas toucher à la main |
| Serveur | `COMPOSE_FILE` | `docker-compose.yml:docker-compose.serveur.yml` |
| Serveur | `WS_DIR` | son dossier de données (absolu) |
| Serveur | `VLLM_API_BASE` | `http://192.168.8.106:8000/v1` |
| Serveur | `VLLM_API_KEY`, `LITELLM_MASTER_KEY`, `POSTGRES_PASSWORD`, `WEBUI_SECRET_KEY`, `MCP_TOKEN` | **identiques** au Spark 1 (on copie son `.env`) |
| Serveur | `SPARK_HOSTNAME` | le nom que les postes utilisent (section 9) |
| Serveur | `TEI_THREADS` | cœurs par instance TEI, défaut 4 ; la moitié des cœurs du serveur est un bon départ |

Le Spark 2 ne change pas (`docker-compose.spark-worker.yml`), sauf `VLLM_GPU_UTIL`, recopié depuis le Spark 1 comme
à chaque changement de modèle.

## 3. Ordre de déploiement

Les étapes 4 à 6 se font **sans couper le service**. La coupure (étape 7) dure le temps de copier les données et de
recharger le modèle : compter 15 à 30 minutes.

1. Pare-feu des deux Spark : ouvrir au serveur (section 4).
2. SSH du Spark 1 vers le serveur (section 5).
3. Serveur : projet, `.env`, embeddings (section 6).
4. Bascule : sauvegarde, arrêt, copie des données, démarrage du serveur puis des Spark (section 7).
5. Vérifications, puis montée de `VLLM_GPU_UTIL_FIXE` (section 7.5).
6. Postes : rien à faire si le nom ne change pas, sinon nouvelle adresse (section 9).

## 4. Pare-feu

Le serveur joint vLLM (port 8000) et les exporteurs (9835 GPU, 9100 machine) des Spark par le réseau de l'équipe.
En duo, vLLM et les exporteurs sont sur le réseau de l'hôte : ufw les protège, **rien d'autre ne doit les joindre**.

Spark 1 :

```bash
sudo ufw allow from <IP_SERVEUR> to any port 8000,9835,9100 proto tcp
# La règle Docker locale de SPARK-DUO.md (section 4) ne sert plus : LiteLLM et le MCP ont quitté le Spark
sudo ufw delete allow from 172.16.0.0/12 to any port 8000 proto tcp
sudo ufw status
```

Spark 2 :

```bash
sudo ufw allow from <IP_SERVEUR> to any port 9835,9100 proto tcp
```

Les règles du câble (`allow in on enp1s0f0np0`…) ne bougent pas. Ray reste fermé au réseau de l'équipe.

Un seul Spark (sans duo) : vLLM n'est pas sur le réseau de l'hôte mais publié par Docker (`VLLM_ECOUTE=0.0.0.0`), et
**Docker contourne ufw** : le port 8000 est alors joignable par tout le réseau, protégé seulement par `VLLM_API_KEY`.

Serveur : seuls 443 (API, MCP) et 8443 (Open WebUI) sont publiés sur le réseau ; LiteLLM (4000), Prometheus (9090) et
Open WebUI en HTTP (3000) n'écoutent que sur `127.0.0.1`. Avec ufw sur le serveur, autoriser SSH avant de l'activer
(Docker ouvre lui-même 443 et 8443).

⚠ Entre le serveur et le Spark 1, le trafic vers vLLM est en HTTP simple : prompts, réponses et `VLLM_API_KEY`
circulent en clair sur le réseau de l'équipe. ufw limite qui peut parler à vLLM, pas qui peut écouter le réseau.
Mettre le serveur sur le même commutateur que les Spark, ou ajouter plus tard un tunnel (WireGuard) si le réseau
n'est pas de confiance.

## 5. SSH du Spark 1 vers le serveur

`modeles.py` (sur le Spark 1) copie à chaque changement de modèle l'historique des profils et les cibles Prometheus
dans `WS_DIR` du serveur, puis y lance l'évaluation fixe. Il lui faut une clé SSH **sans mot de passe** :

```bash
# Spark 1, compte admin_spark4c7c
ssh-keygen -t ed25519 -N "" -f ~/.ssh/id_annexe -C "spark1 -> serveur annexe"
ssh-copy-id -i ~/.ssh/id_annexe.pub admin@<IP_SERVEUR>
cat >> ~/.ssh/config <<'EOF'
Host <IP_SERVEUR>
  IdentityFile ~/.ssh/id_annexe
EOF
ssh -o BatchMode=yes admin@<IP_SERVEUR> true && echo SSH OK
```

Le compte du serveur doit appartenir au groupe `docker` (l'évaluation lance `docker compose exec` et un conteneur de tests).

## 6. Préparer le serveur (sans couper le service)

```bash
# Serveur
sudo mkdir -p /home/admin/data/ws && sudo chown admin: /home/admin/data/ws
cd /home/admin/data/ws
git clone -b spark-duo https://github.com/Ameno1215/localIA.git ia-locale-annexe
cd ia-locale-annexe
```

Copier le `.env` du Spark 1 (mêmes secrets : comptes Open WebUI, clés LiteLLM, jeton MCP et clé vLLM restent valables) :

```bash
# Spark 1
scp /home/admin_spark4c7c/data/ws/ia-locale-duo/.env admin@<IP_SERVEUR>:/home/admin/data/ws/ia-locale-annexe/.env
```

```bash
# Serveur
cd /home/admin/data/ws/ia-locale-annexe
chmod 600 .env
sed -i 's|^COMPOSE_FILE=.*|COMPOSE_FILE=docker-compose.yml:docker-compose.serveur.yml|; s|^WS_DIR=.*|WS_DIR=/home/admin/data/ws|' .env
cat >> .env <<'EOF'
VLLM_API_BASE=http://192.168.8.106:8000/v1
TEI_THREADS=4
EOF
grep -E "^(COMPOSE_FILE|WS_DIR|VLLM_API_BASE|VLLM_API_KEY|SPARK_HOSTNAME)=" .env    # VLLM_API_KEY non vide
mkdir -p ../{models,data,workspace,docs-internes,logs/rag,logs/caddy,prometheus/cibles,prometheus/data,evals}
```

Les lignes `VLLM_*` du profil et `DUO_*` venues du Spark 1 sont sans effet ici (le service `vllm` n'y démarre jamais).

Embeddings et reranker pour TEI sur CPU (version ONNX de bge-m3, ~5 Go, internet requis ; sinon les copier depuis le PC
de test, qui a les mêmes) :

```bash
./scripts/01-telecharger-modeles.sh --serveur
docker compose config --services     # postgres litellm tei-embed tei-rerank qdrant mcp-docs open-webui caddy node-exporter prometheus
docker compose pull && docker compose build mcp-docs
```

Les poids PyTorch de bge-m3 du Spark ne suffisent pas à TEI sur CPU : ne pas les recopier.

## 7. Bascule

### 7.1 Sauvegarder la base LiteLLM (Spark 1, pile encore en marche)

Postgres passe d'ARM à x86 : on transporte une sauvegarde logique plutôt que les fichiers de la base.

```bash
cd /home/admin_spark4c7c/data/ws/ia-locale-duo
docker compose exec -T postgres pg_dump -U litellm -Fc litellm > ~/litellm.dump
ls -lh ~/litellm.dump
```

### 7.2 Arrêter et copier (début de la coupure)

```bash
# Spark 1
cd /home/admin_spark4c7c/data/ws/ia-locale-duo && docker compose down
cd /home/admin_spark4c7c/data/ws
sudo tar -czf ~/annexe-donnees.tgz --numeric-owner open-webui qdrant caddy prometheus logs evals data workspace docs-internes
scp ~/annexe-donnees.tgz ~/litellm.dump admin@<IP_SERVEUR>:
```

Ce qui part et pourquoi : `open-webui` (comptes, conversations, avis), `qdrant` (index des normes et documents déposés
dans Open WebUI), `caddy` (**la même autorité de certification** : les postes n'ont rien à réinstaller), `prometheus`
(un an de métriques), `logs` (historique des profils, journaux RAG et Caddy), `evals`, `data`, `workspace`,
`docs-internes`. Pas `postgres` (sauvegarde ci-dessus), ni `models` et `cache` (restent pour vLLM).

Le Spark 2 peut rester démarré : son `ray-worker` attend la tête.

### 7.3 Démarrer le serveur

```bash
# Serveur
cd /home/admin/data/ws
sudo tar -xzpf ~/annexe-donnees.tgz --numeric-owner -C /home/admin/data/ws
cd ia-locale-annexe
docker compose up -d postgres
until docker compose exec -T postgres pg_isready -U litellm; do sleep 2; done
docker compose exec -T postgres pg_restore -U litellm -d litellm --no-owner < ~/litellm.dump
docker compose up -d
docker compose ps
```

LiteLLM démarre même si vLLM n'est pas encore là ; Open WebUI répond déjà sur `https://<SPARK_HOSTNAME>:8443`.

Recherche dans les normes : bge-m3 reste le même modèle, l'index Qdrant copié reste valable. Si les résultats semblent
moins bons qu'avant, réindexer : `docker compose exec mcp-docs python ingest.py --reset` (avec le modèle démarré, pour
les images).

### 7.4 Démarrer le modèle seul sur les Spark

Spark 1, dans `.env` (le bloc « Serveur annexe » est en bas de `.env.spark.example`) :

```bash
cd /home/admin_spark4c7c/data/ws/ia-locale-duo
nano .env
#   COMPOSE_FILE=docker-compose.yml:docker-compose.spark.yml:docker-compose.spark-duo.yml:docker-compose.spark-annexe.yml
#   ANNEXE_SSH=admin@<IP_SERVEUR>
#   ANNEXE_DOSSIER=/home/admin/data/ws/ia-locale-annexe
#   IP_SPARK1=192.168.8.106
#   IP_SPARK2=192.168.8.194
#   VLLM_GPU_UTIL_FIXE=0.80
docker compose config --services     # vllm gpu-exporter node-exporter, rien d'autre
./scripts/modeles.py utiliser "$(sed -n 's/^VLLM_PROFIL=//p' .env)" --sans-redemarrer --sans-telecharger
grep -E "^VLLM_GPU_UTIL=" .env       # 0.80
scp .env .env.vllm admin_spark19e5@192.168.100.11:/home/admin_spark19e5/data/ws/ia-locale-duo/
```

Spark 2 : rétablir ses deux lignes (SPARK-DUO.md, section 5.5), puis `docker compose up -d --force-recreate`.

Spark 1 :

```bash
docker compose up -d
docker compose logs -f vllm          # « Available KV cache memory » doit avoir grandi, puis « Application startup complete »
./scripts/modeles.py confirmer       # attend vLLM, note le profil (pile: serveur-annexe), copie l'historique et les cibles
                                     # sur le serveur, puis y lance l'évaluation fixe
```

### 7.5 Vérifier, puis monter la mémoire

Sur le serveur :

```bash
./scripts/02-verifier-spark.sh       # 5 lignes [OK] : chat, outil, débit, embeddings, reranker
curl -s http://127.0.0.1:9090/api/v1/targets | python3 -c "import json,sys; [print(t['labels']['job'], t['scrapeUrl'], t['health']) for t in json.load(sys.stdin)['data']['activeTargets']]"
                                     # vllm, gpu, machine, spark2, tei, qdrant, serveur : toutes « up »
```

Puis une question dans Open WebUI et une recherche dans les normes depuis OpenCode.

Monter ensuite `VLLM_GPU_UTIL_FIXE` par paliers (0.80, puis 0.85), avec à chaque palier : `utiliser … --sans-redemarrer`,
copie du `.env` sur le Spark 2, redémarrage (7.4). Avant de monter, regarder la mémoire libre des deux Spark, modèle
arrêté : `free -g` (colonne `available`). Le plafond est **« Gio libres ÷ 121,7 » sur le Spark le moins libre**, moins une
marge de 3 à 4 Gio. Avec ~110 Gio libres (ce que montrait le Spark 2), 0.85 laisse ~6 Gio de marge et 0.88 ~3 Gio.
Désactiver le bureau graphique d'Ubuntu sur les deux Spark rend 1 à 3 Gio de plus :
`sudo systemctl set-default multi-user.target` puis redémarrer.

## 8. Le RAG sur CPU : ce qui ralentit et comment régler

| Étape d'une recherche | Spark (avant) | Serveur, CPU (après) |
|---|---|---|
| Embedding de la question (quelques mots) | GPU, quelques ms | CPU, quelques dizaines de ms |
| Qdrant | quelques ms | pareil |
| **Reranker** : relit `RAG_CANDIDATS` passages (20) d'environ 300 jetons | GPU, ~0,1 s | CPU, **de l'ordre de 1 à quelques secondes** selon les cœurs (estimation, à mesurer) |
| Indexation de documents | GPU | nettement plus lente, en arrière-plan |

La génération des réponses n'est pas concernée. Seules les questions qui déclenchent une recherche dans les normes
attendent le reranker, une fois par recherche.

**Mesurer.** Le journal RAG note chaque durée (`t_embed_ms`, `t_rerank_ms`, `t_total_ms`) : comparer avant et après la
bascule.

```bash
# Serveur : p50 et p95 du reranker sur les dernières recherches
cat ../logs/rag/rag-*.jsonl | python3 -c "
import json,sys,statistics as s
t=sorted(e['t_rerank_ms'] for e in map(json.loads,sys.stdin) if e.get('t_rerank_ms') is not None)[-200:]
print(len(t),'recherches  p50',t[len(t)//2],'ms  p95',t[int(len(t)*.95)],'ms')"
```

**Régler**, du plus simple au plus efficace :

1. `TEI_THREADS` : plus de cœurs pour le reranker (jusqu'au nombre de cœurs physiques moins 2).
2. `RAG_CANDIDATS=10` : deux fois moins de passages à relire, donc environ deux fois plus rapide ; à vérifier sur
   l'évaluation `normes` (`./scripts/evaluer.py --seulement normes`), car un bon passage peut ne plus être relu.
3. Un GPU pour le RAG, sur le serveur ou une autre machine du réseau (même une petite carte : bge-m3 et le reranker
   tiennent en 3 Go). Y lancer TEI en version GPU, puis dans le `.env` du serveur `EMBED_URL=http://<machine>:<port>` et
   `RERANK_URL=…` (`RAG_BACKEND=vllm` si c'est vLLM en mode pooling) ; changer aussi `api_base` de `bge-m3` dans
   `config/litellm.yaml` pour Open WebUI.

Après un changement : `docker compose up -d mcp-docs tei-embed tei-rerank`.

## 9. Les postes

- **Le nom ne change pas** (recommandé) : si `SPARK_HOSTNAME` est un nom DNS (`spark1.lan`), le faire pointer sur
  l'IP du serveur. Caddy y présente un certificat signé par la même autorité (dossier `caddy` copié) : rien à faire sur
  les postes.
- **Le nom change** (ou c'était l'IP du Spark) : mettre le nouveau nom dans `SPARK_HOSTNAME` du serveur,
  `docker compose up -d caddy mcp-docs`, puis sur chaque poste relancer
  `installer-opencode.sh --serveur https://<serveur> … --mcp https://<serveur>/mcp` (GUIDE, section 8) et donner la
  nouvelle adresse d'Open WebUI. L'autorité de certification reste la même.

## 10. Journaux, métriques, évaluations

Tout ce qui servait à comparer les modèles est désormais **sur le serveur**, dans son `WS_DIR` :

| Donnée | Écrit par | Où |
|---|---|---|
| Historique des profils (`profils.jsonl`, champ `pile`) | `modeles.py` sur le Spark 1, copié par SSH | `logs/profils.jsonl` (les deux machines) |
| Cibles Prometheus (`vllm.json`, `spark1.json`, `spark2.json`) | `modeles.py` sur le Spark 1, copiées par SSH | `prometheus/cibles/` |
| Métriques vLLM, GPU et machine des deux Spark, TEI, Qdrant, serveur | Prometheus du serveur | `prometheus/data` (historique du Spark recopié) |
| Requêtes LiteLLM, avis Open WebUI, journal RAG, accès Caddy | services du serveur | comme avant |
| Évaluations fixes | `evaluer.py`, lancé sur le serveur | `evals/` |

- **Rapport par profil** : sur le serveur, `./scripts/rapport-modeles.py --depuis 2026-11-01`.
- **Évaluation** : lancée sur le serveur par `modeles.py confirmer` (par SSH). À la main, sur le serveur :
  `./scripts/evaluer.py` (le profil est lu dans l'historique, jamais dans le `VLLM_PROFIL` copié du Spark 1).
- **Prometheus** : `http://127.0.0.1:9090` sur le serveur (tunnel : `ssh -L 9090:127.0.0.1:9090 admin@<IP_SERVEUR>`).
  Les cibles vLLM portent maintenant `pile="serveur-annexe"` : comparer avant et après la bascule, par exemple
  `histogram_quantile(0.95, sum by (le, pile) (rate(vllm:time_to_first_token_seconds_bucket[1h])))`.
- **Historique Prometheus** : les séries des Spark gardent leurs noms de job (`vllm`, `gpu`, `machine`, `spark2`) ;
  seul le label `instance` change (IP au lieu du nom de conteneur). Les embeddings passent du job `vllm-pooling` au job `tei`.
- Si la copie SSH a échoué (message ⚠ de `modeles.py`), Prometheus sur le serveur ne connaît pas le nouveau profil :
  corriger SSH, puis `./scripts/modeles.py confirmer --sans-eval` sur le Spark 1.

## 11. Revenir en arrière

Les données du Spark 1 n'ont pas été effacées : on peut revenir à la pile complète sur le Spark 1 à tout moment.
Ce qui a été écrit sur le serveur depuis la bascule (conversations, nouvelles clés, métriques) y reste ; le recopier
dans l'autre sens si besoin (même méthode, section 7).

```bash
# Serveur
cd /home/admin/data/ws/ia-locale-annexe && docker compose down
# Spark 1 : retirer « :docker-compose.spark-annexe.yml » de COMPOSE_FILE, commenter ANNEXE_SSH et VLLM_GPU_UTIL_FIXE
cd /home/admin_spark4c7c/data/ws/ia-locale-duo && nano .env
./scripts/modeles.py utiliser "$(sed -n 's/^VLLM_PROFIL=//p' .env)" --sans-redemarrer --sans-telecharger   # remet 0.68
scp .env .env.vllm admin_spark19e5@192.168.100.11:/home/admin_spark19e5/data/ws/ia-locale-duo/
# Spark 2 : rétablir ses deux lignes (SPARK-DUO.md 5.5), docker compose up -d --force-recreate
# Spark 1
docker compose up -d && ./scripts/modeles.py confirmer --sans-eval
sudo ufw allow from 172.16.0.0/12 to any port 8000 proto tcp
```

## 12. Dépannage

| Symptôme | Cause et solution |
|---|---|
| `required variable VLLM_API_BASE is missing` à toute commande `docker compose` du serveur | `VLLM_API_BASE` absent du `.env` du serveur (section 6) |
| Open WebUI ou OpenCode : erreur 500 / `Connection refused` vers le modèle | Depuis le serveur : `curl -s -H "Authorization: Bearer <VLLM_API_KEY>" http://192.168.8.106:8000/v1/models`. Échec : ufw du Spark 1 (section 4) ou vLLM arrêté. `401` : `VLLM_API_KEY` différente entre serveur et Spark |
| Prometheus : cible `vllm`, `gpu` ou `machine` « down » | ufw (section 4) ; `spark1.json` vide : relancer `modeles.py confirmer --sans-eval` sur le Spark 1 avec `ANNEXE_SSH` et `IP_SPARK1` remplis |
| Cible `spark2` « down » | ufw du Spark 2 (section 4), ou `IP_SPARK2` = IP du câble au lieu du réseau de l'équipe |
| `⚠ Copie vers le serveur annexe impossible` | `ssh -o BatchMode=yes admin@<IP_SERVEUR> true` depuis le Spark 1 (section 5) ; `ANNEXE_DOSSIER` doit contenir le `.env` du serveur |
| `tei-embed` redémarre en boucle | bge-m3 sans version ONNX : `./scripts/01-telecharger-modeles.sh --serveur` |
| Recherches dans les normes lentes | Section 8 |
| `Free memory on device … less than desired GPU memory utilization` | `VLLM_GPU_UTIL_FIXE` trop haut pour l'un des Spark : redescendre d'un palier (section 7.5), vider le cache disque (SPARK-DUO.md, section 9) |
| `pg_restore` : erreurs « already exists » | LiteLLM a démarré avant la restauration et a créé ses tables : `docker compose down`, vider `WS_DIR/postgres` du serveur, refaire 7.3 |
