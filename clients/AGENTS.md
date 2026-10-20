# Consignes pour l'agent de code

À copier à la racine de chaque projet (`cp ~/Documents/IA\ locale/clients/AGENTS.md .`), puis à compléter
avec ce qui est propre au projet (commande de tests, conventions, dossiers à ne pas toucher).

## Règles générales
- Réponds toujours en français.
- Avant toute question ou modification qui touche une norme, une convention ou une procédure interne,
  appelle l'outil `docs-internes_rechercher_docs` et cite la source (fichier, section) dans ta réponse.
- Lis un fichier avant de le modifier. Fais des modifications petites et ciblées.
- Après chaque modification, lance les tests du projet et corrige jusqu'à ce qu'ils passent.
- Une modification n'existe que si le fichier a été écrit. N'annonce jamais un changement que tu n'as pas fait.
- Si une information manque, dis-le au lieu d'inventer.

## Ce projet
- Lancer les tests : `python -m pytest -q`   <!-- à adapter -->
- Lancer l'application : `python main.py`    <!-- à adapter -->
- Ne pas modifier : `.env`, les fichiers de données de production   <!-- à adapter -->
