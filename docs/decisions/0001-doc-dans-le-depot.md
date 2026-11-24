# 0001 : la documentation de travail vit dans le dépôt

> **Date** : 2026-11-16 · **Statut** : acceptée

## Contexte

Il faut un endroit sur GitHub pour les notes, réflexions, explications et schémas d'architecture de la plateforme, avec des schémas faciles à modifier.

## Options

1. **Dossier `docs/` dans le dépôt, Markdown + Mermaid.** Versionné avec le code, relu dans les PR, lié aux branches. GitHub affiche Mermaid sans rien installer.
2. **Wiki GitHub + Mermaid.** Rien à mettre en place, écriture très rapide depuis le navigateur. Mais c'est un dépôt git à part : la doc n'évolue pas avec le code ni avec les branches (`spark`, `spark-duo`, `laptop`).
3. **GitHub Pages.** Un vrai site, mais il faut une construction et un déploiement, et sur un dépôt privé il faut un plan payant. Inutile pour des notes internes.

## Décision

Option 1 : `docs/` dans le dépôt, sur la branche `spark-duo`.

## Conséquences

- Un schéma d'architecture peut être mis à jour dans la même PR que le changement de stack qu'il décrit.
- Écrire une note demande un commit. Depuis GitHub, « Add file » dans le dossier suffit.
- Le wiki reste possible pour des notes très libres : il s'active dans les réglages du dépôt (Settings, General, Features, Wikis).
