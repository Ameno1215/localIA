# IA locale

Une plateforme d'IA qui tourne entièrement sur nos machines :
- **un agent de code** façon Claude Code (OpenCode) ;
- **une recherche dans les normes internes** (RAG) ;
- **un chat web** avec des comptes utilisateurs.

Elle tourne aujourd'hui sur un PC de test (RTX 8 Go) et est prête à passer sur le DGX Spark.

📖 **Toute la documentation est dans [GUIDE.md](GUIDE.md)** : fonctionnement, utilisation, passage au Spark, dépannage.

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

Installe le driver NVIDIA, Docker compose, le NVIDIA Container Toolkit et Python. Il demande sudo, et il faut redémarrer ensuite.

```bash
./scripts/01-telecharger-modeles.sh
```

Télécharge les modèles (ajoute `--spark` sur le DGX Spark).

```bash
cp .env.example .env
docker compose up -d
```

Crée la configuration à partir du modèle (pense à changer les secrets), puis démarre la stack.

## État au 12/10/2026

| Brique | État |
|---|---|
| Modèle local `qwen-local` (Qwen3.5-4B AWQ, 64K de contexte) | ✅ testé |
| Agent de code OpenCode (fichiers locaux, diffs, commandes, conversation suivie) | ✅ testé |
| Recherche dans les normes (RAG + MCP) | ✅ testé, avec la norme d'exemple seulement |
| Clés et quotas par personne (LiteLLM) | ✅ testé |
| Panneau web de contrôle | ✅ testé |
| Open WebUI | ⚠️ prêt, compte admin à créer |
| Fine-tuning LoRA `normes` | ⏳ scripts prêts, pas encore lancé |
| Configuration DGX Spark | ⏳ fichiers prêts, non testés |
