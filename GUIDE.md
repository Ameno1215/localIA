# Guide de l'IA locale

Ce guide explique comment fonctionne la plateforme d'IA locale et comment s'en servir au quotidien : coder avec un agent, interroger les normes internes, gérer les accès. Tout tourne sur nos machines, sans envoyer de données sur internet.

Aujourd'hui, la plateforme tourne sur un PC de test (RTX 4060, 8 Go de VRAM). Elle est prévue pour passer telle quelle sur le DGX Spark, avec un modèle plus gros. La section 8 décrit ce passage.

---

## 1. Vue d'ensemble

### Le principe

L'agent de code tourne **sur le poste du développeur** : il lit et modifie tes fichiers et lance tes commandes chez toi. Seul le modèle (le « cerveau ») tourne sur le serveur. Entre les deux ne circule que du texte : la demande et la réponse.

```
 Poste développeur                               Serveur (PC de test, puis DGX Spark)
 ┌────────────────────────────────┐              ┌──────────────────────────────────────────┐
 │ OpenCode (terminal)            │   requêtes   │ LiteLLM :4000  clés, quotas, file d'attente│
 │  - lit / écrit TES fichiers    │ ───────────> │   └─> vLLM :8000  modèle "qwen-local" (GPU)│
 │  - lance TES tests, git, etc.  │   (texte)    │                                          │
 │  - montre les diffs, chat      │ ───────────> │ MCP docs-internes :9901  recherche normes │
 └────────────────────────────────┘              │   └─> Qdrant + bge-m3 + reranker          │
                                                 │ Open WebUI :3000  chat dans le navigateur │
                                                 └──────────────────────────────────────────┘
```

### Les briques

| Brique | Rôle | Port |
|---|---|---|
| **vLLM** | Fait tourner le modèle de langage sur le GPU. Il traite plusieurs requêtes en parallèle et met les autres en file d'attente. | 8000 (local) |
| **LiteLLM** | Porte d'entrée unique vers les modèles. Il donne une clé API à chaque personne, applique des quotas et enregistre la consommation. | 4000 |
| **Postgres** | Base de données de LiteLLM : clés, utilisateurs, consommation. | interne |
| **Qdrant** | Base « vectorielle » : stocke les morceaux de tes documents indexés pour la recherche. | 6333 (local) |
| **TEI bge-m3** | Transforme un texte en vecteur (embedding), pour trouver les passages qui parlent du même sujet. Tourne sur CPU. | interne |
| **TEI reranker** | Parmi les passages trouvés, garde ceux qui répondent vraiment à la question. Tourne sur CPU. | interne |
| **MCP docs-internes** | Serveur d'outils pour les agents. Son outil principal, `rechercher_docs`, cherche dans les normes. | 9901 |
| **Open WebUI** | Chat dans le navigateur, avec un compte par personne et l'historique des conversations. | 3000 |
| **OpenCode** | Agent de code dans le terminal, l'équivalent local de Claude Code. **C'est l'outil principal pour coder.** | sur le poste |
| **Panneau web** | Démarre et arrête les services, affiche leur état et rappelle les commandes. | 7000 (local) |

### Le chemin d'une question sur les normes

1. Tu demandes à l'agent : « Que dit la norme sur les logs ? ».
2. L'agent appelle l'outil `rechercher_docs`.
3. bge-m3 trouve les 20 passages les plus proches dans Qdrant.
4. Le reranker garde les 5 meilleurs.
5. Le modèle rédige la réponse à partir de ces passages et cite la source (fichier, page).

### Le chemin d'une modification de code

1. Tu demandes à OpenCode : « Ajoute un score au jeu ».
2. OpenCode lit tes fichiers en local.
3. Il envoie la demande et le contenu utile au modèle.
4. Le modèle répond par des actions : modifier tel fichier, lancer telle commande.
5. OpenCode te montre le diff et attend ton accord.
6. Il applique la modification sur ton disque et lance les tests chez toi.

---

## 2. Les modèles

| Nom (vu par les clients) | Ce que c'est | Où |
|---|---|---|
| `qwen-local` | Le modèle de langage principal : chat, code, appels d'outils. Sur le PC de test, c'est **Qwen3.5-4B** quantifié en AWQ 4 bits, avec 64K tokens de contexte et le raisonnement activé. | vLLM, GPU |
| `normes` | Le même modèle, plus un adaptateur LoRA entraîné sur les normes. **Pas encore disponible** (voir la section 6). | vLLM, GPU |
| `bge-m3` | Modèle d'embeddings pour la recherche dans les documents. Il ne sait pas discuter. | TEI, CPU |
| reranker `bge-reranker-v2-m3` | Trie les passages trouvés par pertinence. Il n'est pas exposé aux clients. | TEI, CPU |

Les clients ne voient que des **noms**. Le vrai modèle derrière `qwen-local` peut changer (un Qwen 4B sur le PC de test, Nemotron 3.5 Super 120B sur le Spark) sans rien modifier chez les utilisateurs. Le nom `qwen-local` est historique : il ne désigne pas forcément un modèle Qwen.

### Changer de modèle

1. Mets les poids dans `models/<dossier>`.
2. Dans `.env`, règle :
   - `VLLM_MODEL=<dossier>` ;
   - `VLLM_MAX_MODEL_LEN`, la taille de contexte ;
   - `VLLM_TOOL_PARSER`, le format d'appels d'outils de la famille du modèle (`hermes` pour Qwen3-4B, `qwen3_xml` pour Qwen3.6 et 3.8, d'après NVIDIA) ;
   - `VLLM_EXTRA_ARGS`, les options propres au modèle.
3. Redémarre vLLM : bouton **Redémarrer** sur la carte `vllm` du panneau, ou `docker compose up -d vllm`.
4. Vérifie dans les logs qu'il démarre, et note la ligne `GPU KV cache size`.

**Historique sur le PC de test :**
- **Qwen3-4B-Instruct-2507 FP8 :** 20K de contexte au maximum, et du code souvent cassé en mode agent.
- **Qwen3.5-4B AWQ :** 64K de contexte, un jeu complet et testé du premier coup. C'est le modèle retenu.

L'ancien modèle reste dans `models/` (13 Go). Tu peux supprimer ses deux dossiers si tu as besoin de place.

**Sur le Spark**, ne règle pas ces variables à la main : utilise les profils de modèles (section 8, « Tester d'autres modèles »).

---

## 3. Démarrer et arrêter

### Le panneau web (le plus simple)

Lance-le, puis ouvre http://localhost:7000 :

```bash
./scripts/panneau.sh
```

- **Modes rapides :**
  - *Tout démarrer* ;
  - *Mode chat* : modèle et Open WebUI ;
  - *Mode agent de code* : modèle, RAG et outils ;
  - *Mode fine-tuning* : libère le GPU ;
  - *Tout arrêter*.
- **Une carte par service :** son état, sa RAM et son CPU, et les boutons Démarrer, Arrêter, Redémarrer, Logs et Ouvrir.
- **Jauges :** l'occupation du GPU et de la RAM.

Le panneau n'est accessible que depuis le PC lui-même.

### En ligne de commande

Les commandes se lancent depuis le dossier du projet.

| Action | Commande |
|---|---|
| Tout démarrer | `docker compose up -d` |
| État des services | `docker compose ps` |
| Tout arrêter (rien n'est perdu) | `docker compose stop` |
| Arrêter un seul service | `docker compose stop vllm` |
| Redémarrer un service | `docker compose restart litellm` |
| Lire les logs | `docker compose logs -f vllm` |

vLLM met **1 à 2 minutes** à démarrer. Il est prêt quand la carte du panneau affiche « En marche », ou quand `curl -s localhost:8000/health` répond.

Au redémarrage du PC, les services repartent seuls, sauf ceux que tu avais arrêtés avec `stop`. Le panneau web, lui, est à relancer à la main.

**N'utilise pas** `docker compose down -v` : le `-v` efface les comptes, les clés et l'index des documents.

---

## 4. Coder avec l'agent (OpenCode)

OpenCode est un agent de code dans le terminal. Il fonctionne comme Claude Code :
- tu discutes avec lui ;
- il lit et modifie les fichiers de ton projet ;
- il lance des commandes (tests, git, ton programme) ;
- il te montre chaque modification et attend ton accord ;
- la conversation continue d'une demande à l'autre.

### Installer sur un poste (une fois)

Prérequis : Node.js 20 ou plus récent (déjà présent sur le PC de test via nvm), et de préférence ripgrep.

```bash
sudo apt install ripgrep
```

1. **Crée ta clé personnelle.** C'est l'admin qui le fait, sur le serveur. La clé ne s'affiche qu'une fois : garde-la.
   ```bash
   ./scripts/creer-cle-utilisateur.sh prenom.nom
   ```
2. **Installe et configure OpenCode.** Le script demande la clé, puis vérifie qu'elle fonctionne.
   ```bash
   ./scripts/installer-opencode.sh
   ```
3. **Ouvre un nouveau terminal**, ou lance `source ~/.bashrc`.

Le script :
- installe OpenCode 1.18.33 ;
- enregistre ta clé dans `~/.config/ia-locale/env` (lisible par toi seul) ;
- écrit la config `~/.config/opencode/opencode.json`, qui pointe vers le modèle et la recherche dans les normes.

Tu peux le relancer à tout moment pour mettre à jour la config.

### Première session

1. **Va dans ton projet**, de préférence un dépôt git propre, sans modification en cours :
   ```bash
   cd mon-projet
   ```
2. **Copie le modèle de consignes**, puis adapte-le (commande de tests, fichiers à ne pas toucher) :
   ```bash
   cp ~/Documents/IA\ locale/clients/AGENTS.md .
   ```
3. **Lance l'agent :**
   ```bash
   opencode
   ```

Écris ta demande et valide. Quand l'agent veut modifier un fichier ou lancer une commande, il affiche le diff ou la commande et te demande l'autorisation. Accepte ou refuse, puis enchaîne ta demande suivante dans la même conversation.

**Raccourcis utiles dans OpenCode :**

| Commande | Effet |
|---|---|
| `/undo` | Annule la dernière modification de l'agent |
| `/redo` | Rétablit ce qui a été annulé |
| `/new` | Démarre une nouvelle conversation |
| `/sessions` | Reprend une conversation précédente |
| `/compact` | Résume la conversation pour libérer du contexte, utile quand elle devient longue |
| `/models` | Choisit le modèle (`qwen-local`, ou `normes` plus tard) |
| `Tab` | Bascule entre l'agent *build* (modifie les fichiers) et l'agent *plan* (réfléchit sans rien modifier) |
| `/exit` | Quitte |

### En une seule commande (sans interface)

```bash
opencode run "Ajoute un test pour la fonction verifier"
```

- `--continue` (ou `-c`) poursuit la dernière conversation. Exemple : `opencode run -c "Le test échoue, corrige"`.
- `--auto` autorise automatiquement les modifications et les commandes. **À réserver à un projet de test**, car l'agent ne te demande plus rien.

### Bien formuler ses demandes (petit modèle)

Avec un modèle de 4B, la qualité dépend beaucoup de la précision de la demande.

- **Une demande à la fois.** Mieux vaut cinq petites étapes qu'un « fais-moi toute l'application ».
- **Nomme les fichiers et les fonctions.** Par exemple : « Dans `jeu.py`, la fonction `verifier(proposition, secret)` doit renvoyer `'plus'`, `'moins'` ou `'gagné'` ».
- **Demande toujours de lancer les tests**, en donnant la commande exacte.
- **Colle l'erreur exacte** quand quelque chose plante : « Voici l'erreur : … Corrige et relance les tests ».
- **Vérifie toujours le résultat réel** (`git diff`, les tests, le programme). Un agent peut annoncer « c'est fait » alors que ce n'est pas le cas.

Exemple réel sur le PC de test (projet `~/projets/jeu-test`) :
1. **Première demande :** « Crée jeu.py : un jeu où l'ordinateur choisit un nombre entre 1 et 100 et le joueur doit le deviner… Crée test_jeu.py avec 3 tests pytest, puis lance python3 -m pytest -q. » En 30 secondes, il a produit un jeu complet et jouable, et les 3 tests passaient.
2. **Deuxième demande (`-c`) :** « Si je tape q, le programme plante. Corrige-le et relance les tests. » En 25 secondes, le bug était corrigé et vérifié par l'agent lui-même.

### La recherche dans les normes depuis l'agent

OpenCode a accès à l'outil `rechercher_docs`. Le fichier `AGENTS.md` lui demande de s'en servir dès qu'une question touche une norme. Tu peux aussi le demander explicitement : « Cherche dans nos normes ce qui concerne la journalisation, puis vérifie si `jeu.py` les respecte ».

Chaque réponse se termine par une section **Sources** : une ligne par extrait cité, avec un lien. Pour un PDF, le lien ouvre le document directement à la bonne page (`…/docs/norme.pdf#page=12`) ; pour les autres formats, il ouvre l'extrait utilisé. Dans le terminal, Ctrl+clic (Cmd+clic sur Mac) sur le lien l'ouvre dans le navigateur.

Pour voir les recherches qu'il fait réellement :

```bash
docker compose logs -f mcp-docs
```

---

## 5. Les normes et documents internes (RAG)

1. **Ajoute tes documents.** Dépose les fichiers (PDF, DOCX, MD, TXT) dans `docs-internes/` (sur le Spark : `WS_DIR/docs-internes/`, ou le dossier choisi par `DOCS_DIR` dans `.env`). Les sous-dossiers sont acceptés.
2. **Lance l'indexation.** Depuis le panneau : section **Documents**, bouton **Indexer les nouveautés** (journal en direct, `mcp-docs` redémarré à la fin, ce qui rend l'étape 3 inutile). Ou en ligne de commande, le mode agent devant tourner. Seuls les fichiers nouveaux ou modifiés sont traités :
   ```bash
   docker compose exec mcp-docs python ingest.py
   ```
3. **Redémarre le serveur MCP** quand tu as ajouté ou renommé des fichiers : la liste des documents, que le modèle lit pour décider de chercher, n'est construite qu'au démarrage.
   ```bash
   docker compose restart mcp-docs
   ```
4. **Pour tout réindexer** (par exemple après avoir supprimé des fichiers) :
   ```bash
   docker compose exec mcp-docs python ingest.py --reset
   ```
5. **Vérifie ce qui est trouvé** pour une question :
   ```bash
   docker compose exec mcp-docs python -c "from server import rechercher_docs; print(rechercher_docs('ma question', 3))"
   ```

Les documents sont découpés en morceaux d'environ 1 200 caractères.

**Tableaux et images.**
- Les **tableaux Word** sont convertis en tableaux Markdown, à leur place dans le texte. Un long tableau est coupé en blocs qui répètent la ligne d'en-tête : chaque morceau garde le nom des colonnes.
- Les **tableaux PDF « texte »** (sélectionnables à la souris) sont lus comme du texte : le contenu est trouvé, mais la mise en colonnes est perdue.
- Les **images** (PDF et Word : tableau en image, schéma, capture, page scannée) sont transcrites par le LLM actif, s'il lit les images. C'est le cas du modèle par défaut, Nemotron 3.5 Super **VL**. Le texte obtenu (tableau Markdown, description de schéma, texte de la page) est indexé à la page de l'image et cité comme le reste. Les petites images (moins de 100 pixels de côté : icônes, puces), les images répétées (logo d'en-tête) et celles que le modèle juge décoratives sont ignorées.
- **Durée :** quelques secondes par image (environ 50 jetons/s). Pendant ce temps l'indexation occupe une des 8 places de génération de vLLM : pour une grosse norme pleine de figures, lance-la plutôt hors des heures de travail.
- **Modèle sans vision** (Lightning, Qwen…) : les images sont comptées « en attente » et le texte est indexé quand même. Au prochain `ingest.py` lancé avec un modèle vision, les documents concernés sont repris automatiquement. `--sans-images` force ce comportement (pour ne pas solliciter le LLM).
- Les lignes `!` signalent ce qui n'a pas été lu : image non décodée, page sans texte, transcription tronquée.

Après la mise à jour qui a ajouté la lecture des images, reconstruis le conteneur (`docker compose up -d --build mcp-docs`). Le premier `ingest.py` réindexe alors tous les documents une fois, images comprises.

Le fichier `NORME-DEV-001` est un **exemple fictif**. Remplace-le par tes vraies normes.

---

## 6. Autres usages

### Chat dans le navigateur (Open WebUI)

1. Lance le *Mode chat* depuis le panneau, puis ouvre http://localhost:3000.
2. Crée ton compte. Le premier compte créé devient administrateur.
3. La recherche dans les normes (`docs-internes`) est déclarée d'office et ouverte à tous les comptes. Sur une installation antérieure à ce réglage, elle n'est visible que des admins : l'ouvrir à tous avec
   ```bash
   docker compose exec open-webui python3 -c "
   import sqlite3, json
   db = sqlite3.connect('/app/backend/data/webui.db')
   (v,) = db.execute(\"select value from config where key='tool_server.connections'\").fetchone()
   conns = json.loads(v)
   for c in conns:
       if (c.get('info') or {}).get('id') == 'docs-internes':
           c.setdefault('config', {})['access_grants'] = [{'principal_type': 'user', 'principal_id': '*', 'permission': 'read'}]
   db.execute(\"update config set value=? where key='tool_server.connections'\", (json.dumps(conns),))
   db.commit()
   "
   docker compose restart open-webui
   ```
4. **Sources cliquables (admin, une fois).** Installe le filtre `services/open-webui/filtre_sources.py` : *Panneau d'administration > Fonctions > +*, colle le contenu du fichier, enregistre, puis active la fonction et rends-la **Globale** (menu ⋯). Les références `[a1b2c3d4]` des réponses deviennent des liens numérotés `[1]`, `[2]`… suivis de la liste des sources. Un clic ouvre, dans un nouvel onglet, le PDF à la bonne page (ou l'extrait pour un Word ou un texte) ; le lien « extrait » à côté montre le passage exact utilisé. Après une mise à jour du fichier, recolle-le à la place de l'ancien.

---

## 7. Plusieurs utilisateurs

- **Une clé par personne :** `./scripts/creer-cle-utilisateur.sh prenom.nom`. Par défaut, la clé autorise 2 requêtes en parallèle et 60 par minute. Les limites se règlent dans le script.
- **Suivi de consommation :** http://localhost:4000/ui, avec la clé maître (`LITELLM_MASTER_KEY` dans `.env`).
- **File d'attente :** vLLM traite `VLLM_MAX_SEQS` requêtes à la fois (2 sur le PC de test, 8 prévues sur le Spark). Les suivantes attendent leur tour, sans erreur.
- **La clé maître est réservée à l'admin.** Ne la donne jamais : chaque personne doit avoir sa propre clé.

---

## 8. Passage au DGX Spark

> Les fichiers sont prêts, mais **pas encore testés sur le Spark**. Les points marqués ⚠ sont à vérifier sur place.
>
> **Pas à pas complet, de la copie du projet à la vérification, avec les pannes probables : [SPARK-PREMIER-DEMARRAGE.md](SPARK-PREMIER-DEMARRAGE.md).** Les étapes ci-dessous en sont le résumé.

### Côté serveur (sur le Spark)

1. **Clone le projet** sur le Spark dans `/home/data/ws/ia-locale` (`git clone -b spark …`, voir le pas-à-pas ; Docker est déjà installé et configuré, `./scripts/00-prerequis.sh` ne fait que vérifier), puis copie à part les PDF des normes dans le dossier de données (`WS_DIR/docs-internes/`) : ils ne sont pas dans git.
2. **Prépare la configuration :**
   ```bash
   ./scripts/preparer-env-spark.sh spark1.lan /home/data/ws
   ```
   Le script génère les secrets, crée les dossiers de données (`WS_DIR`, par exemple `/home/data/ws`) (la clé maître LiteLLM doit commencer par `sk-`), fixe `SPARK_HOSTNAME` et crée `MCP_TOKEN`. Le `.env` obtenu contient `COMPOSE_FILE`, qui fait utiliser la config Spark à toutes les commandes `docker compose` et au panneau web.
3. **Télécharge les modèles du Spark** (~82 Go, dans `WS_DIR/models`), avec internet ou depuis un disque de transfert :
   ```bash
   ./scripts/01-telecharger-modeles.sh --spark
   ```
4. **Démarre la stack :**
   ```bash
   docker compose up -d
   ```
   Le panneau web (`./scripts/panneau.sh`) écoute sur `127.0.0.1:7000` du Spark. Depuis ton poste : `ssh -L 7000:localhost:7000 <utilisateur>@<spark>`, puis http://localhost:7000.
5. **Attends que vLLM ait chargé le modèle de 120B (75 Go).** Ça prend 15 à 30 minutes la première fois, et la commande `docker compose logs -f vllm` montre la progression.
6. **Vérifie que tout répond** : chat, appels d'outils, débit, embeddings et reranker.
   ```bash
   ./scripts/02-verifier-spark.sh
   ```
   Attendu : 5 lignes OK. Le débit doit être nettement au-dessus de celui du PC (45 tok/s) : NVIDIA mesure 51 tok/s pour ce modèle sur Spark, pour une seule requête de code (le script en mesure une aussi, avec le préremplissage compté, donc un peu moins). Si une ligne échoue, voir la section 9.
7. **Crée tout de suite le compte admin d'Open WebUI** : ouvre `https://<SPARK_HOSTNAME>:8443` et inscris-toi. **Le premier compte créé devient administrateur**, et la page est joignable par tout le réseau, donc fais-le avant d'annoncer le serveur à l'équipe. Ajoute ensuite le MCP des normes (section 6).
8. **Récupère le certificat HTTPS interne de Caddy**, à distribuer aux postes (le navigateur affiche sinon un avertissement) :
   ```bash
   docker compose cp caddy:/data/caddy/pki/authorities/local/root.crt ./spark-ca.crt
   ```

Ce que la surcharge `docker-compose.spark.yml` change par rapport au PC :
- **Modèle :** `Nemotron 3.5 Super VL 120B-A12B` NVFP4 (NVIDIA, sorti le 15/10/2026), le plus gros Nemotron 3.5 qui tient sur un Spark 128 Go. Texte et images, 262 144 jetons de contexte, 8 requêtes en parallèle. Le Spark n'a pas à ressembler au PC de test : il prend le meilleur modèle qu'il peut porter.
- **Réglages vLLM :** ceux de la **recette officielle NVIDIA** du modèle (dépôt [local-ai-inference-recipes](https://github.com/NVIDIA/local-ai-inference-recipes), validée le 15/10/2026 sur un Spark avec le pilote 580 et CUDA 13.0). Un profil par modèle dans `config/modeles-spark/`. Le conteneur reçoit aussi les réglages que NVIDIA met dans toutes ses recettes : mémoire partagée de 16 Go, mémoire verrouillée illimitée, caches de compilation conservés dans `WS_DIR/cache/` (démarrages suivants plus rapides).
- **Image vLLM :** `eugr/spark-vllm:nightly-20261015` (image communautaire ARM64 que cite la recette NVIDIA, tag figé) pour le modèle par défaut. Réglable avec `VLLM_IMAGE` ; chaque profil indique l'image de sa recette. Le panneau web affiche la mémoire unifiée du Spark via la jauge RAM.
- **Embeddings et reranker :** servis par vLLM sur le GPU, car TEI n'existe pas pour ARM64.
- **Accès réseau :** seul **Caddy** (HTTPS) est joignable depuis le réseau. Port 443 : LiteLLM sur `/` (pour OpenCode) et la recherche dans les normes sur `/mcp`, protégée par le jeton d'équipe. Port 8443 : **Open WebUI**, le chat web, à ouvrir sur `https://<SPARK_HOSTNAME>:8443`. Tout le reste n'écoute qu'en local.
- **Serveur MCP :** il n'expose que `rechercher_docs` (`OUTILS_CODE=0`). Les outils fichiers et commandes ne doivent jamais être joignables par le réseau.

### Côté postes (chaque développeur)

1. **Crée sa clé** sur le Spark :
   ```bash
   ./scripts/creer-cle-utilisateur.sh prenom.nom
   ```
2. **Lance l'installation sur son poste**, avec l'adresse du Spark :
   ```bash
   ./scripts/installer-opencode.sh --serveur https://spark1.lan --contexte 262144 --mcp https://spark1.lan/mcp --jeton-mcp <jeton> --ca ./spark-ca.crt
   ```

C'est la seule différence avec le PC de test : `opencode` s'utilise exactement de la même façon.

### Tester d'autres modèles

Chaque modèle a besoin de ses propres réglages (image vLLM, format d'appels d'outils, options). Ils sont rangés dans un **profil** par modèle, dans `config/modeles-spark/`. Le script `scripts/modeles.py` lit les recettes officielles de [recipes.vllm.ai](https://recipes.vllm.ai) (filtre « DGX Spark ») pour les créer, donc rien n'est à recopier à la main.

```bash
./scripts/modeles.py lister                      # profils connus, téléchargés ou non, modèle actif
./scripts/modeles.py chercher nvidia             # modèles dont la recette est vérifiée pour le Spark (filtre facultatif)
./scripts/modeles.py ajouter nvidia/Qwen3.8-27B-NVFP4   # crée le profil d'un modèle de la liste
./scripts/modeles.py utiliser qwen3.8-27b-nvfp4  # télécharge si besoin, met .env à jour, redémarre vLLM
./scripts/02-verifier-spark.sh                   # teste chat, outils, débit
```

Pour comparer deux modèles : `utiliser` le premier, lance `02-verifier-spark.sh` et note le débit, puis fais pareil avec le second. Les clients n'ont rien à changer, le nom reste `qwen-local`.

Profils fournis (liste revue le 21/10/2026 d'après Hugging Face, alignée sur les recettes officielles NVIDIA). « Débit » = génération de code, une seule requête, mesuré par NVIDIA sur un DGX Spark :

| Profil | Type | Mémoire | Débit | À savoir |
|---|---|---|---|---|
| `nemotron-3.5-lightning-30b-a3b-nvfp4` | MoE, 3 Md actifs | 18 Go | 144 tok/s | Le plus rapide. Image communautaire figée, brouillon DSpark |
| `qwen3.6-35b-a3b-nvfp4` | MoE, 3 Md actifs | 21 Go | 124 tok/s | Ancien modèle par défaut : rapide, repli si Nemotron Super pose problème. Conteneur NGC de NVIDIA |
| `bolt-74b-a9b-24gb` | MoE hybride, 9 Md actifs, 74 Md au total | 24 Go | 89 tok/s | Conçu pour les agents locaux. Image communautaire figée, brouillon LiLiCorr |
| `qwen3.6-35b-a3b-fp8` | MoE, 3 Md actifs | 42 Go | non mesuré | Version 8 bits de Qwen3.6 : plus fidèle, un peu plus lente |
| `ornith-1.5-122b-nvfp4` | MoE, 122 Md | ~70 Go | non mesuré | **Expérimental** : publié le 20/10, scores annoncés par l'éditeur, aucune validation Spark. Licence MIT |
| `nemotron-3.5-super-vl-120b-a12b-nvfp4` | MoE, 120 Md, 12 Md actifs | 75 Go | 51 tok/s | **Modèle par défaut.** Le plus capable. Texte et images. Image communautaire figée, brouillon LiLiCorr. Prend l'essentiel de la mémoire |
| `qwen3.8-flash-next-fp4-local` | MoE, 125 Md au total, 6 Md actifs | ~100 Go | 38 tok/s | **Pour viser ~90 % de la mémoire.** Image communautaire figée et conteneur moins isolé exigés par la recette |
| `qwen3.8-27b-nvfp4` | Dense, 27 Md | 32 Go | 37 tok/s | Modèle recommandé par le playbook NVIDIA vLLM. Conteneur NGC, brouillon DFlash |

**Remplir la mémoire ne rend pas plus rapide** : les modèles de 75 à 100 Go, dont le modèle par défaut, sont environ 3 fois plus lents que les MoE à 3 Md actifs (Lightning, Qwen3.6). Ils répondent mieux, mais moins vite. À plusieurs utilisateurs, le débit par personne baisse encore.

Écartés après examen : DeepSeek V4.1 Flash (765 Go), GLM-5.3 (328 Go au minimum) et Kimi K3 (2,8 To) ne tiennent pas sur un Spark ; Mistral Medium 3.5 128B est un modèle dense (environ 90 Go lus à chaque mot, donc très lent) ; Kolibri 1 ne gère que l'allemand et l'anglais ; Step 3.7 Flash (117 Go) ne laisse pas de marge.

**Utiliser ~90 % de la mémoire.** Les recettes NVIDIA réservent 0.8 pour vLLM (`VLLM_GPU_UTIL`), mais avec le modèle seul. Ici, la pile complète (système, embeddings et reranker, Open WebUI, bases) occupe déjà ~30 Gio au démarrage de vLLM : les profils réservent donc 0.74 (mesuré sur le Spark, 0.8 refuse de démarrer). Un seul gros modèle à la fois, et rien d'autre de lourd sur le GPU. Autres gros modèles à comparer avec le modèle par défaut : `qwen3.8-flash-next…`, puis `ornith…` (le plus récent et le moins validé). Compare chacun avec `02-verifier-spark.sh` et sur tes vraies tâches.

Où trouver d'autres modèles :
- **`chercher`** : la source la plus fiable, car chaque modèle listé a une recette de réglages testée sur le GB10.
- **`nouveautes`** : les derniers modèles publiés sur Hugging Face par un auteur (`./scripts/modeles.py nouveautes nvidia`). Un modèle de la semaine n'a souvent pas encore de recette : le profil se rédige alors à la main d'après sa fiche (modèle : `bolt-74b-a9b-24gb.env`).
- **Hugging Face** : les modèles NVIDIA au format NVFP4 sont dans l'organisation [nvidia](https://huggingface.co/nvidia). Un modèle qui n'a pas de recette GB10 n'est pas refusé, mais ses réglages sont à trouver à la main.
- **NVIDIA** : les [playbooks du Spark](https://build.nvidia.com/spark) et le forum développeurs annoncent les modèles validés.

Règles utiles :
- **Jusqu'où monter :** les 128 Go sont partagés avec le système, les services Docker et les embeddings (environ 10 Go). Un modèle jusqu'à ~85 Go de « mémoire min. » est réaliste. Au-delà (DeepSeek V4, 170 Go) il faut deux Spark.
- **Plus gros n'est pas plus rapide.** À chaque mot généré, le Spark relit les paramètres actifs en mémoire, et sa bande passante (environ 270 Go/s, ordre de grandeur) limite la vitesse. Un MoE à 3 Md actifs génère donc bien plus vite qu'un modèle de 120 Md à 12 Md actifs, et un modèle dense de 27 Md est encore plus lent.
- **Ce que la mémoire en plus apporte :** soit un modèle plus capable (120 Md en 4 bits), soit le même modèle en meilleure précision (8 bits au lieu de 4). Compare avec `02-verifier-spark.sh` et sur tes vraies tâches, pas seulement sur la taille.

Les poids se téléchargent avec internet, puis tout tourne hors ligne. Une LoRA entraînée sur un modèle ne fonctionne pas avec un autre : réentraîne-la après un changement.

### Plus tard

- **Deuxième Spark :** décommente la deuxième entrée `qwen-local` dans `config/litellm.yaml`. LiteLLM répartit alors la charge entre les deux.
- **Réseau fermé :** prévois un miroir du paquet npm `opencode-ai`, de ripgrep et des images Docker. Les variables `OPENCODE_DISABLE_*` sont déjà posées par le script d'installation.
- **LoRA :** il faudra la réentraîner sur le modèle du Spark. Un adaptateur entraîné sur le 4B ne fonctionne pas avec Nemotron Super. Si vLLM refuse de démarrer avec la LoRA et le MTP ensemble, vide `VLLM_SPEC_ARGS`.

---

## 9. Dépannage

| Symptôme | Cause | Solution |
|---|---|---|
| vLLM redémarre en boucle avec `operator torchvision::nms does not exist` | Image vLLM incohérente (le tag `-cu129` de la v0.30.0) | Utiliser `vllm/vllm-openai:v0.30.0`, déjà en place |
| vLLM : `CUDA driver version is insufficient` | Driver NVIDIA trop ancien | Driver ≥ 580 (595 installé) |
| vLLM : `invalid loads value` au démarrage | Les guillemets JSON de `VLLM_EXTRA_ARGS` ont été retirés | Entourer le JSON d'apostrophes : `'{"cle":valeur}'` |
| vLLM : pas assez de mémoire pour le contexte | `VLLM_MAX_MODEL_LEN` trop grand pour la VRAM | Baisser `VLLM_MAX_MODEL_LEN` ou `VLLM_GPU_UTIL` dans `.env` |
| Plus de RAM disponible, PC lent | TEI qui garde 10 à 15 Go par instance | Déjà corrigé (`MALLOC_ARENA_MAX`, lots réduits). Vérifier avec `docker stats` |
| OpenCode : `401` / clé refusée | Clé incomplète ou mal collée | `curl -s localhost:4000/v1/models -H "Authorization: Bearer $LITELLM_API_KEY"`, puis recréer la clé et relancer `installer-opencode.sh` |
| `opencode : commande introuvable` | Terminal ouvert avant l'installation | `source ~/.bashrc` |
| OpenCode en mode `run` : « permission requested… auto-rejecting » | Les modifications demandent ton accord, et il n'y a pas d'interface en mode `run` | Utiliser `opencode` (interactif), ou `--auto` sur un projet de test |
| L'agent annonce « c'est fait » mais rien n'a changé | Petit modèle qui décrit au lieu d'agir | `git diff`, puis redemander en nommant le fichier. Suivre les outils appelés avec `docker compose logs -f mcp-docs` |
| L'agent ne trouve rien dans les normes | Documents non indexés, ou mode agent arrêté | `docker compose exec mcp-docs python ingest.py`, et vérifier que `mcp-docs`, `qdrant` et `tei-*` sont en marche |
| Un tableau ou un schéma en image n'est pas trouvé | Image non transcrite : modèle sans vision, ou `ingest.py` lancé avant | Relancer `docker compose exec mcp-docs python ingest.py` avec le modèle par défaut (VL) : les images « en attente » sont reprises |
| Spark : vLLM plante au démarrage, renvoie du texte incohérent, ou le débit est très bas | Noyaux GPU mal pris en charge sur GB10 par cette version de vLLM | Dans `.env`, vider `VLLM_SPEC_ARGS`, puis relancer avec `docker compose up -d vllm`. Si ça ne suffit pas : repli sur `./scripts/modeles.py utiliser qwen3.6-35b-a3b-nvfp4`. Les pannes probables sont détaillées dans [SPARK-PREMIER-DEMARRAGE.md](SPARK-PREMIER-DEMARRAGE.md), section 8 |
| Spark : manque de mémoire alors qu'il en reste | Le cache disque n'est pas libéré (mémoire unifiée) | `sudo sh -c 'sync; echo 3 > /proc/sys/vm/drop_caches'` |

---

## 10. Référence

### Arborescence

```
IA locale/
├── GUIDE.md                    ce guide
├── README.md                   démarrage rapide
├── SPARK-PREMIER-DEMARRAGE.md  pas à pas du premier démarrage sur le Spark
├── AUDIT-NVIDIA.md             écarts avec les tutoriels et recettes NVIDIA
├── docker-compose.yml          la stack (PC de test)
├── docker-compose.spark.yml    surcharge pour le DGX Spark
├── .env / .env.example         réglages et secrets du PC (.env n'est jamais partagé)
├── .env.spark.example          réglages du Spark
├── config/
│   ├── litellm.yaml            modèles exposés aux clients
│   ├── modeles-spark/          un profil par modèle testable sur le Spark
│   └── Caddyfile.spark         proxy HTTPS du Spark
├── models/                     poids des modèles (hors git)
├── docs-internes/              tes normes et documents (indexés pour la recherche)
├── services/mcp-docs/          serveur d'outils : server.py (recherche), ingest.py (indexation)
├── clients/
│   ├── opencode.json           modèle de config OpenCode (utilisé par le script d'installation)
│   └── AGENTS.md               consignes de l'agent, à copier dans chaque projet
├── finetune/                   génération du jeu d'entraînement et entraînement LoRA
├── loras/                      adaptateurs entraînés
├── panneau/                    panneau web (server.py, index.html)
└── scripts/
    ├── 00-prerequis.sh         PC : driver, NVIDIA Container Toolkit (une fois, sudo) ; Spark : vérifications seulement
    ├── 01-telecharger-modeles.sh   poids des modèles (--spark pour le Spark)
    ├── 02-verifier-spark.sh    vérifie que chat, outils, embeddings et reranker répondent
    ├── preparer-env-spark.sh   crée le .env du Spark (secrets générés)
    ├── diagnostic-spark.sh     rassemble l'état du Spark dans un fichier à transmettre
    ├── modeles.py              modèles du Spark : chercher, ajouter, utiliser (profils)
    ├── creer-cle-utilisateur.sh    clé LiteLLM pour une personne
    ├── installer-opencode.sh   OpenCode sur un poste
    └── panneau.sh              lance le panneau web
```

### Ports

| Port | Service | Accessible depuis |
|---|---|---|
| 3000 | Open WebUI | ce PC (le Spark : via Caddy, port 8443) |
| 4000 | LiteLLM (API des modèles et interface d'admin `/ui`) | ce PC (le Spark : via Caddy en 443) |
| 6333 | Qdrant (tableau de bord : `/dashboard`) | ce PC |
| 7000 | Panneau web | ce PC |
| 8000 | vLLM | ce PC |
| 9901 | MCP docs-internes | ce PC (le Spark : via Caddy, `/mcp`) |

### Variables de `.env`

| Variable | Rôle |
|---|---|
| `LITELLM_MASTER_KEY` | Clé admin de LiteLLM |
| `POSTGRES_PASSWORD`, `WEBUI_SECRET_KEY` | Secrets internes |
| `VLLM_MODEL` | Dossier du modèle dans `models/`, servi sous le nom `qwen-local` |
| `VLLM_MAX_MODEL_LEN` | Contexte maximal par requête, en tokens |
| `VLLM_GPU_UTIL` | Part de la VRAM réservée à vLLM (0 à 1) |
| `VLLM_MAX_SEQS` | Nombre de requêtes traitées en parallèle |
| `VLLM_TOOL_PARSER` | Format des appels d'outils du modèle |
| `VLLM_EXTRA_ARGS` | Options vLLM propres au modèle |
| `VLLM_LORA_ARGS` | Active l'adaptateur `normes` (vide tant qu'il n'est pas entraîné) |
| `WS_DIR` | Spark uniquement : dossier des données des conteneurs (modèles, caches, bases, documents) |
| `DOCS_DIR` | Spark uniquement : dossier des normes indexées (défaut `WS_DIR/docs-internes`). `./docs-internes` = le dossier du projet |
| `VLLM_IMAGE` | Spark uniquement : image Docker de vLLM |
| `VLLM_SPEC_ARGS` | Spark uniquement : décodage spéculatif MTP (vide pour le désactiver) |
| `MCP_OUTILS_CODE` | 0 (défaut) : seulement la recherche dans les normes. 1 : ajoute des outils fichiers/commandes sur `workspace/` |
| `SPARK_HOSTNAME`, `MCP_TOKEN` | Spark uniquement : nom du serveur et jeton d'équipe pour les normes |

### Fichiers sur chaque poste (créés par `installer-opencode.sh`)

| Fichier | Contenu |
|---|---|
| `~/.config/ia-locale/env` | Ta clé, le jeton MCP et les options hors ligne (lisible par toi seul) |
| `~/.config/opencode/opencode.json` | Serveur, modèles, recherche dans les normes, autorisations |
| `~/.bashrc` | Une ligne qui charge `~/.config/ia-locale/env` |
