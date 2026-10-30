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
| [SPARK-SERVEUR.md](SPARK-SERVEUR.md) | Le Spark ne fait tourner que le modèle, un serveur annexe porte le reste (non testé) |
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
