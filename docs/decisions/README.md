# Décisions

Un choix tranché par page, avec le contexte et le pourquoi : dans six mois, on saura pourquoi on a fait comme ça. Une décision ne se réécrit pas ; si elle change, on en écrit une nouvelle qui la remplace et on passe l'ancienne en « remplacée par 00XX ».

Nom de fichier : `00XX-sujet.md`, numéro suivant le dernier.

| N° | Décision | Date | Statut |
|---|---|---|---|
| [0001](0001-doc-dans-le-depot.md) | La documentation de travail vit dans le dépôt, en Markdown et Mermaid | 2026-11-16 | acceptée |

## Modèle de décision

```markdown
# 00XX : titre court de la décision

> **Date** : AAAA-MM-JJ · **Statut** : proposée | acceptée | remplacée par 00YY

## Contexte
Le problème, les contraintes.

## Options
1. Option A : pour, contre.
2. Option B : pour, contre.

## Décision
Ce qu'on retient, en une phrase.

## Conséquences
Ce que ça change, ce qu'on accepte de perdre.
```
