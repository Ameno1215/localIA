# NORME-DEV-001 — Conventions de développement Python (EXEMPLE fictif, à remplacer par vos normes)

Version 1.2 — applicable à tous les projets Python internes.

## 1. Nommage

Les fonctions et variables sont en snake_case, en français ou en anglais, mais jamais un mélange
des deux dans un même module. Les classes sont en PascalCase. Les constantes sont en MAJUSCULES.

## 2. Gestion des erreurs

Il est interdit d'utiliser `except:` ou `except Exception:` sans relancer l'exception ou la journaliser
avec le module `logging`. Toute erreur journalisée doit inclure l'identifiant de la requête (`request_id`).

## 3. Journalisation

L'utilisation de `print()` est interdite dans le code livré ; utiliser `logging.getLogger(__name__)`.
Les niveaux autorisés en production sont INFO, WARNING et ERROR.

## 4. Tests

Toute fonction publique doit avoir au moins un test unitaire pytest. La couverture minimale exigée
est de 80 % par module. Les tests sont dans le dossier `tests/` et nommés `test_<module>.py`.

## 5. Données personnelles

Aucune donnée personnelle (nom, e-mail, téléphone) ne doit apparaître dans les journaux.
Les e-mails doivent être masqués sous la forme `a***@domaine.fr`.
