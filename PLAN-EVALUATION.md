# Plan d'évaluation des modèles par cas d'usage

But : savoir, **pour chaque usage de l'équipe** (OS de carte à puce, embarqué C), quel profil vLLM répond le mieux, à quel coût
(latence, mémoire, utilisateurs simultanés), et suivre l'évolution dans le temps. Tout tourne **en local** : le code et les
documents internes ne sortent jamais du Spark, y compris pour la notation (pas de juge dans le cloud).

Ce document est un plan : rien n'est encore implémenté au-delà de l'existant (PR #13). Il dit quoi construire, dans quel ordre,
et ce qui doit être décidé avant.

## 1. Point de départ

Ce qui existe déjà sur `spark-duo` :

| Élément | Ce qu'il fait | Limite pour la comparaison |
|---|---|---|
| `scripts/evaluer.py` | Jeux `normes` (vrai RAG + faits attendus en regex + source citée), `outils` (appel d'outil ou non), `code` (pytest dans un conteneur sans réseau), `charge` (même prompt à 1/4/8 en parallèle) | 6 + 4 + 4 cas d'exemple : aucune différence entre modèles n'est significative. Code en Python seulement. Température 0 imposée. |
| `scripts/rapport-modeles.py` | Une ligne par profil : trafic réel LiteLLM, pouces Open WebUI, journal RAG, Prometheus, dernière évaluation | Ne garde que **le dernier** `.resume.json` par profil : une seconde évaluation écrase la première dans le rapport, pas de moyenne ni de dispersion. |
| `WS_DIR/logs/profils.jsonl` | Quel profil était actif quand | — |
| `WS_DIR/logs/rag/rag-*.jsonl` | Chaque recherche : candidats, scores vecteur et rerank, retenus, durées | La configuration RAG (version d'index, modèle d'embedding, reranker, taille des morceaux, top-k) n'est pas notée dans les résultats d'évaluation. |
| Prometheus | TTFT, latence, file d'attente, jetons générés, puissance GPU | Cache KV, préemptions et cache de préfixe pas encore exploités dans le rapport. |
| Issue #12 | `evals/normes.jsonl` ne contient que des questions d'exemple | Bloquant pour tout le reste : il faut des questions de référence réelles. |

Principe retenu : **étendre `evaluer.py` plutôt que d'adopter un gros framework**, et n'utiliser les outils externes que là où
ils apportent quelque chose qu'on ne refera pas mieux (benchmarks publics, tests de charge, RAG).

## 2. Règles communes à tous les jeux

### 2.1 Données : publiques, synthétiques, internes

Chaque cas porte un champ `origine` :

- `public` : tiré d'un benchmark ouvert, versionné dans le dépôt (`evals/`) ou téléchargé une fois dans `WS_DIR/benchmarks/`.
- `synthetique` : fabriqué automatiquement à partir de matériel interne (bug injecté, incohérence injectée, commentaire retiré).
  Pas de rédaction humaine, mais **reste confidentiel** puisqu'il contient du code interne.
- `interne` : écrit ou validé par un ingénieur de l'équipe, sur du vrai code ou de vraies specs.

Tout cas `synthetique` ou `interne` vit dans **`WS_DIR/evals-internes/`**, jamais dans git. `evaluer.py` lit les deux
dossiers ; le dépôt ne garde que les exemples et le format. Les résultats détaillés (qui contiennent les réponses) restent dans
`WS_DIR/evals/`, déjà hors dépôt.

Les benchmarks publics servent de **garde-fou** (le modèle n'est pas cassé, le parser d'outils marche, la quantification n'a
pas dégradé le raisonnement). Ils ne décident pas : sur les usages qui comptent, seuls nos cas `interne` et `synthetique`
représentent notre métier (C embarqué, ISO 7816, GlobalPlatform, Java Card, contraintes mémoire).

### 2.2 Taille minimale

Avec 6 questions, un modèle qui en réussit 5 et un autre 4 ne sont pas distinguables. Pour départager deux modèles proches
il faut **au moins 50 cas par usage**, idéalement 100. Le rapport affichera un intervalle de confiance (bootstrap à 95 %)
et signalera les écarts non significatifs.

### 2.3 Méthodes de notation, de la plus fiable à la moins fiable

1. **Exécution** : le code compile, les tests passent, la couverture mesurée par gcov augmente. Aucune ambiguïté.
2. **Correspondance exacte ou structurée** : identifiant d'exigence, numéro de ligne, choix QCM, statut APDU, ensemble de liens.
   Précision, rappel, F1.
3. **Faits attendus** (regex ou liste de points clés), comme `normes.jsonl` aujourd'hui.
4. **LLM juge**, seulement quand rien d'objectif n'existe (qualité d'une explication, d'une doc). Avec précautions (§ 2.4).
5. **Revue humaine** : pour calibrer le juge et pour trancher entre les deux finalistes.

### 2.4 Utiliser un LLM juge sans se tromper

- Juge = le plus gros modèle local (aujourd'hui Nemotron 3.5 Super 120B), servi **à part** de l'évaluation : on génère d'abord
  toutes les réponses de tous les profils, puis on charge le juge et on note tout en une passe. Les réponses sont stockées,
  donc on peut re-noter avec un autre juge sans tout relancer.
- Grille explicite par cas (liste de points attendus, erreurs éliminatoires), pas une note globale de 1 à 10.
- Comparaison **par paires à l'aveugle** (A contre B, ordre inversé une seconde fois) plutôt que note absolue ; un gagnant
  ne compte que si les deux ordres sont d'accord.
- Un modèle ne se juge pas lui-même : quand le candidat est le juge, on prend le second plus gros d'une autre famille.
- **Calibration** : 30 à 50 jugements doublés par un humain ; on publie l'accord (kappa de Cohen). Sous 0,6, le juge n'est
  pas utilisé pour cet usage.

### 2.5 Ce qu'on enregistre pour chaque exécution

Ajouts au `.resume.json` (et à chaque ligne du `.jsonl`) :

- `id_execution`, commit git du dépôt, empreinte des jeux de cas (hash des fichiers), nombre de cas par origine ;
- profil, mode (simple / duo), image vLLM, longueur de contexte, paramètres d'échantillonnage, raisonnement activé ou non ;
- configuration RAG : version d'index (déjà calculée par `ingest.py`), modèle d'embedding, reranker et où il tourne
  (GPU du Spark ou CPU du serveur annexe, PR #15), taille et chevauchement des morceaux, nombre de candidats et top-k ;
- pour chaque réponse : jetons de raisonnement et de réponse séparés, `finish_reason` (une réponse coupée est un échec à part).

`rapport-modeles.py` agrège **toutes** les exécutions d'un même profil et d'une même configuration (moyenne, écart, nombre
d'exécutions) au lieu de garder la dernière.

### 2.6 Échantillonnage

`evaluer.py` force aujourd'hui température 0 et graine 1. Les modèles de raisonnement (Qwen3.x, Nemotron) sont réglés pour
leurs paramètres recommandés et se dégradent souvent en greedy (boucles, réponses tronquées). Proposition : chaque profil déclare
ses paramètres recommandés, l'évaluation les utilise, et chaque cas est tiré **3 fois** (pass@1 moyen et pass@3). La
température 0 reste disponible en option pour les tests de régression rapides. *À décider (§ 6).*

### 2.7 Deux niveaux d'exécution

- **Rapide** (~15 min) : lancé automatiquement par `modeles.py utiliser`, comme aujourd'hui. Sous-ensemble fixe de chaque jeu
  (10 cas par usage) + charge à 1/4/8. Sert à détecter une régression.
- **Complet** (une nuit) : tous les cas, 3 tirages, juge, benchmarks publics, balayage de charge et de contexte. Lancé à la
  main ou par minuterie systemd quand le Spark est libre. Sert à comparer.

## 3. Cas d'usage

Pour chaque usage : ce qu'on mesure, le jeu de données, ce qu'on peut réutiliser, la notation et les métriques.

### 3.1 Questions techniques et problèmes d'ingénierie

**Mesure** : exactitude sur notre domaine (ISO 7816-3/4, ISO 14443, GlobalPlatform, Java Card, EMV, Critères Communs,
contraintes mémoire et énergie d'une carte), capacité à raisonner sur un problème d'ingénierie, à dire « je ne sais pas ».

**Jeu de données**
- 50 à 100 questions `interne` écrites par l'équipe, en trois types : QCM (notation automatique), réponse courte avec faits
  attendus (regex, format actuel de `normes.jsonl`), problème ouvert avec grille de points attendus.
- Sources : questions réellement posées en revue, à un nouvel arrivant, sur le canal d'équipe ; erreurs connues des modèles.
- 10 % de questions **sans réponse dans nos documents** ou à prémisse fausse : on attend un refus ou une correction.
- Le jeu `normes` actuel en devient un sous-ensemble (questions qui exigent le RAG).

**Réutilisable** : MMLU-Pro (catégories ingénierie et informatique) et GPQA Diamond via `lm-evaluation-harness` en mode
`local-chat-completions` vers LiteLLM, comme garde-fou de raisonnement général uniquement.

**Notation** : QCM et faits attendus automatiques ; problèmes ouverts au juge avec grille, calibré par un humain.

**Métriques** : % exact, % de faits couverts, % d'hallucinations (faits interdits, références inventées), % de refus corrects
sur les questions sans réponse, jetons et durée par réponse.

### 3.2 Écrire du code

**Mesure** : produire du C embarqué correct, qui compile sans avertissement, respecte nos règles (MISRA, pas d'allocation
dynamique, tailles bornées) et passe des tests.

**Jeu de données**
- 30 à 50 tâches `interne` : une fonction à écrire avec sa signature, son contrat (commentaire Doxygen) et des tests unitaires
  cachés. Matière : fonctions existantes de nos modules (on retire le corps, on garde l'en-tête et les tests), en priorité les
  utilitaires sans dépendance matérielle (parsing APDU/TLV, BER-TLV, CRC, gestion de buffers, machines à états).
- Le jeu `code` actuel (Python, dont `analyser_apdu`) reste comme exemple public.

**Réutilisable** : HumanEval+ / MBPP+ (EvalPlus), MultiPL-E (variantes C++), LiveCodeBench pour suivre les modèles sur des
problèmes récents non vus à l'entraînement. Garde-fous seulement : ce n'est ni du C ni de l'embarqué.

**Notation** : exécution. Le conteneur de test actuel (`python:3.12-slim`, sans réseau) est complété par une image
`eval-c` construite une fois, hors ligne : `gcc`/`clang`, Unity ou CppUTest (selon ce que l'équipe utilise), `cppcheck`
avec l'addon MISRA, `gcovr`. Si le compilateur croisé de l'équipe peut tourner dans un conteneur, on l'ajoute pour vérifier
la taille du code.

**Métriques** : pass@1 (et pass@3), % qui compile avec `-Wall -Wextra -Werror`, nombre de violations `cppcheck`/MISRA par
tâche, taille du code et pile (`-fstack-usage`) quand c'est mesurable, jetons et durée.

### 3.3 Corriger des bugs

**Mesure** : à partir d'un symptôme (test qui échoue, trace, description), trouver et corriger le défaut sans casser le reste.

**Jeu de données**
- **Bugs injectés** (`synthetique`), la méthode principale parce qu'elle donne beaucoup de cas sans effort humain : on prend un
  module C avec de bons tests, un outil de mutation (Mull pour C/C++, ou un petit mutateur maison : `<` ↔ `<=`, décalage de
  ±1, condition inversée, `break` retiré, mauvais index de buffer) crée des variantes ; on garde celles qui font échouer au moins
  un test. Le modèle reçoit le fichier, la sortie du test et doit rendre un correctif.
- 20 à 30 **vrais bugs historiques** (`interne`) tirés du gestionnaire de tickets et de git : le commit de correction donne
  la vérité, les tests de non-régression servent de juge. Plus rares mais plus représentatifs (débordements, alignement,
  endianness, sécurité contre les attaques en faute).

**Réutilisable** : SWE-bench Multilingual (inclut des dépôts C et C++) et SWE-bench Verified, avec leurs images Docker, pour
évaluer le mode agent (OpenCode) plus tard (phase 4). Lourds à faire tourner : pas dans l'exécution rapide.

**Notation** : exécution (le test qui échouait passe, tous les autres aussi). Localisation notée à part : le correctif
touche-t-il la bonne fonction ?

**Métriques** : % corrigés, % de correctifs qui cassent un autre test, % de bonne localisation, taille du diff
(un correctif minimal est préférable), durée et jetons.

### 3.4 Expliquer du code

**Mesure** : expliquer correctement une fonction ou un module (rôle, flux de contrôle, effets de bord, cas limites) sans
inventer.

**Jeu de données** : 30 extraits `interne` de taille variée (une fonction, un fichier, un module de 2 000 lignes) avec, pour
chacun, une **liste de points que l'explication doit contenir** et d'erreurs classiques, écrite par l'auteur du code ou un
relecteur. On peut aussi poser des **questions vérifiables** sur le code (« que renvoie f si le buffer est vide ? », « quel
octet de statut est renvoyé si le fichier n'existe pas ? ») : réponses exactes, notation automatique.

**Réutilisable** : peu de choses pertinentes (CodeXGLUE code-to-text est noté en BLEU, ce qui ne mesure pas la justesse).

**Notation** : questions vérifiables en exact ; explications libres au juge avec la grille de points, calibré.

**Métriques** : % de questions vérifiables justes, % de points couverts, nombre d'affirmations fausses par explication.

### 3.5 Documentation à partir du code (doc de code)

**Mesure** : produire des commentaires Doxygen et des docs de module justes, complets et au format maison.

**Jeu de données** (`synthetique`) : nos fonctions déjà bien documentées. On retire le commentaire, le modèle le réécrit, on
compare à l'original. Coût humain nul pour construire le jeu.

**Notation**
- Structure, automatique : Doxygen sans avertissement, chaque paramètre documenté, `@return`, sens `[in]/[out]` justes.
- Contenu : le juge compare la doc générée à l'originale (contradiction, omission d'un cas limite, invention).
- Test « aller-retour » optionnel : à partir de la seule doc générée, un autre modèle répond aux questions vérifiables
  de § 3.4 ; une bonne doc permet d'y répondre.

**Métriques** : % de docs sans avertissement Doxygen, % de paramètres justes, score juge (contradictions, omissions),
% de réponses justes à l'aller-retour.

### 3.6 Extraction des exigences de test

**Mesure** : à partir d'une spec (ou d'une section de norme), lister les exigences testables, chacune atomique, avec
condition et résultat attendu.

**Jeu de données** (`interne`) : 10 à 20 sections de specs pour lesquelles l'équipe a **déjà** une liste d'exigences validée
(issue de l'outil de gestion des exigences, export ReqIF ou tableur). C'est la vérité de référence ; aucune rédaction nouvelle
si ces listes existent.

**Réutilisable** : pas de benchmark public de ce type dans notre domaine.

**Notation** : appariement entre exigences extraites et exigences de référence. Le juge décide seulement « même exigence ou
non » pour chaque paire candidate (pré-filtrée par similarité d'embedding avec bge-m3, déjà sur le Spark), question simple
où un juge est fiable, puis calcul automatique.

**Métriques** : rappel (exigences de référence retrouvées), précision (exigences extraites qui existent vraiment), %
d'exigences non atomiques ou non testables, doublons.

### 3.7 Couverture des exigences

**Mesure** : à partir d'une liste d'exigences et d'une liste de tests (noms, descriptions, code), dire quel test couvre
quelle exigence et lesquelles ne sont pas couvertes.

**Jeu de données** (`interne`) : notre matrice de traçabilité existante sur un ou deux modules. Variante `synthetique` : on
retire quelques tests de la liste et on vérifie que le modèle signale les exigences devenues orphelines.

**Notation** : comparaison d'ensembles de liens (exigence, test), automatique.

**Métriques** : précision, rappel et F1 sur les liens ; rappel sur les exigences non couvertes (le plus important : une
exigence non couverte manquée est l'erreur coûteuse).

### 3.8 Couverture du code par les tests

Deux tâches distinctes, toutes deux notées par l'exécution réelle de gcov, donc sans jugement :

1. **Analyse** : on donne un fichier C et ses tests ; le modèle liste les fonctions et branches non couvertes. Vérité :
   rapport `gcovr` réel. Métriques : précision et rappel sur les branches non couvertes.
2. **Génération de tests** : le modèle écrit des tests pour couvrir ce qui manque. On les compile et exécute dans `eval-c`.
   Métriques : % de tests qui compilent et passent, gain de couverture lignes/branches, et **score de mutation** des
   nouveaux tests (tuent-ils les mutants du § 3.3 ?), qui distingue un test utile d'un test qui exécute sans rien vérifier.

**Jeu de données** (`synthetique`) : 10 à 20 modules C compilables sur PC avec leur framework de tests unitaires.

### 3.9 Cohérence doc / code

**Mesure** : signaler les écarts entre une doc (spec, commentaire, doc de module) et le code, sans fausses alertes.

**Jeu de données**
- Base : paires doc/code jugées cohérentes par l'équipe.
- `synthetique` : on injecte une incohérence soit dans la doc (valeur, ordre des étapes, code d'erreur changé) soit dans le code
  (mutations du § 3.3 sur des lignes documentées). La position de l'écart est connue.
- Moitié des paires laissées intactes : un modèle qui crie toujours au loup doit être pénalisé.
- Quelques vrais écarts trouvés en revue (`interne`).

**Notation** : automatique (écart signalé ou non, à la bonne ligne ou section).

**Métriques** : rappel sur les écarts injectés, taux de fausses alertes sur les paires intactes, précision de localisation.

### 3.10 RAG : recherche et réponses

On évalue séparément la **recherche** (sans LLM, rapide, permet de tester beaucoup de configurations) et la **réponse**
(avec le LLM, coûteux).

**Jeu de données**
- 100 questions avec les passages pertinents connus (`qrels` : question → documents et pages). Construction : le modèle propose
  des questions à partir de morceaux tirés au hasard, un humain garde les bonnes et ajuste ; + toutes les questions de § 3.1 qui
  relèvent des docs. Les questions générées sont trop proches du texte : il faut au moins un tiers de questions écrites par
  des humains, reformulées comme on les pose vraiment.
- Questions sans réponse dans la base (abstention attendue).
- Le journal RAG réel (`logs/rag/`) donne la distribution des vraies questions : on s'en sert pour choisir les thèmes, sans
  copier les questions des collègues dans le jeu sans leur accord.

**Réutilisable** : format BEIR et `ir_measures`/`pytrec_eval` pour les métriques de recherche ; RAGAS (fidélité, précision et
rappel du contexte) branché sur LiteLLM, c'est-à-dire un juge local. Les scores RAGAS sont des jugements de LLM : à calibrer
comme au § 2.4.

**Ce qu'on fait varier (recherche seule)** : modèle d'embedding, reranker ou non, nombre de candidats (20 aujourd'hui), top-k
(5), taille et chevauchement des morceaux (1 200 / 200 caractères), recherche hybride BM25 + vecteurs, reranker sur GPU ou CPU
(PR #15). Chaque variante = une version d'index, notée dans les résultats.

**Notation** : recherche automatique avec les qrels ; réponse avec les faits attendus et la source citée (déjà dans
`eval_normes`), plus le juge pour la fidélité (la réponse ne dit rien que les passages ne disent pas).

**Métriques** : rappel@5 et @20, MRR, nDCG@5, latence de recherche (embed, Qdrant, rerank : déjà dans le journal RAG) ;
pour la réponse : % juste, % avec bonne source citée, références inventées, fidélité, % d'abstention correcte, nombre
d'appels à l'outil par question.

## 4. Comportement du système

Ces mesures ne dépendent pas des cas d'usage mais de la pile (modèle, quantification, simple/duo, contexte maximal, cache
KV). Elles se lisent avec la qualité : un modèle meilleur mais qui ne tient que 3 utilisateurs n'est pas le bon choix.

### 4.1 Utilisateurs simultanés

**Outils** : `vllm bench serve` (déjà dans l'image vLLM, rien à installer) ou GuideLLM, lancés contre LiteLLM pour mesurer la
pile entière, et directement contre vLLM pour isoler le modèle.

**Scénarios**
- Balayage de débit d'arrivée (requêtes/s) jusqu'à saturation, et concurrence fixe 1, 4, 8, 16, 32.
- **Profils de requêtes réalistes**, tirés des tailles réelles dans les lignes LiteLLM (jetons d'entrée et de sortie, jamais le
  texte) : une session OpenCode envoie 30 à 60 k jetons de contexte, une question Open WebUI quelques centaines. Le mélange
  des deux est le cas critique.
- Préfixe partagé (même prompt système + même dépôt) pour mesurer le gain du cache de préfixe.

**Métriques** : TTFT p50/p95, latence inter-jetons p50/p95, débit total, et surtout le **goodput** : nombre de requêtes par
seconde qui respectent un objectif de service (par exemple TTFT < 3 s et > 15 jetons/s par utilisateur). Le résultat lisible
est « ce profil tient N utilisateurs actifs avec cet objectif ». *L'objectif est à fixer (§ 6).*

### 4.2 Longs contextes

**Mesure** : la qualité tient-elle quand le contexte grandit, et combien coûte le pré-remplissage ?

**Outils** : RULER (NVIDIA, synthétique, longueur réglable : aiguille dans une botte de foin, suivi de variables, agrégation)
à 8 k, 32 k, 64 k, 128 k et jusqu'au maximum du profil (131 072 ou 262 144). Puis une version maison : questions vérifiables
du § 3.4 posées avec **un module entier de notre code** dans le contexte, à tailles croissantes, la réponse étant à
différentes profondeurs.

**Métriques** : précision par longueur et par profondeur, longueur à laquelle la précision passe sous 80 % de celle à 8 k,
TTFT en fonction de la longueur (coût du pré-remplissage), mémoire KV consommée.

### 4.3 Longues réflexions

**Mesure** : ce que le raisonnement apporte, ce qu'il coûte, et quand il déraille.

**Jeu** : les problèmes d'ingénierie (§ 3.1) et les bugs (§ 3.3), avec raisonnement activé, désactivé, et à plusieurs budgets
quand le modèle le permet.

**Métriques** : gain de précision par rapport au mode sans raisonnement, jetons de raisonnement (médiane, p95, max), % de
réponses tronquées (`finish_reason = length`), % de boucles (répétition détectée), durée de bout en bout. Pour un usage
interactif, le résultat lisible est « précision obtenue en moins de 60 s ».

### 4.4 Pression sur le cache KV

**Mesure** : ce qui se passe quand plusieurs longues sessions saturent la mémoire KV (préemption, recalcul, files d'attente).

**Scénario** : N sessions OpenCode simulées à 32–64 k de contexte chacune, augmentées jusqu'à saturation, en simple et en
duo (TP2 donne plus de KV par séquence : à vérifier sur nos profils).

**Métriques Prometheus** (déjà collectées, à ajouter au rapport) : `vllm:kv_cache_usage_perc`, `vllm:num_requests_running` et
`vllm:num_requests_waiting`, `vllm:num_preemptions_total`, taux de succès du cache de préfixe. Résultat lisible : nombre de
sessions longues simultanées avant la première préemption, et dégradation de TTFT au-delà.

## 5. Extension des scripts

Sans écrire le harnais ici, la forme proposée :

- **`scripts/evaluer.py`** reste l'orchestrateur. Ajouts : `--niveau rapide|complet`, `--tirages N`, lecture de
  `WS_DIR/evals-internes/`, métadonnées du § 2.5, séparation génération / notation (les réponses sont stockées, la notation
  peut être relancée), un jeu par usage : `questions`, `code_c`, `bugs`, `explication`, `doc`, `exigences`, `tracabilite`,
  `couverture`, `coherence`, `rag_reponse`. Les jeux actuels `normes`, `outils`, `code` sont conservés.
- **`scripts/juger.py`** : charge les réponses d'une ou plusieurs exécutions, appelle le juge (le profil juge doit être actif),
  écrit les notes à côté. Gère les comparaisons par paires et l'export de l'échantillon de calibration humaine.
- **`scripts/eval-rag.py`** : recherche seule, sans LLM, sur une ou plusieurs versions d'index ; rappel@k, MRR, nDCG.
- **`scripts/bench-charge.sh`** : enveloppe de `vllm bench serve` exécutée dans le conteneur vLLM, scénarios du § 4, résultats
  dans `WS_DIR/evals/charge/` avec le même `id_execution`.
- **`scripts/fabriquer-cas.py`** : génère les cas `synthetique` (mutants, commentaires retirés, incohérences injectées) depuis
  un dépôt de code interne monté en lecture seule.
- **Image `eval-c`** : gcc/clang, framework de tests unitaires de l'équipe, cppcheck + MISRA, gcovr, Mull. Construite une fois
  avec accès réseau puis utilisée sans réseau, comme `executer_tests` aujourd'hui.
- **`scripts/rapport-modeles.py`** : agrège toutes les exécutions (moyenne, intervalle de confiance, nombre d'exécutions),
  une colonne par usage, et une vue « qualité × capacité » (score par usage face au nombre d'utilisateurs tenus).

Benchmarks publics : `lm-evaluation-harness` pour MMLU-Pro et GPQA, EvalPlus, RULER, plus tard SWE-bench. Téléchargés une fois
dans `WS_DIR/benchmarks/`, lancés par `evaluer.py --niveau complet` et versés dans le même format de résultats.

Revue humaine : Open WebUI a un mode **Arena** (deux modèles anonymes côte à côte, vote, classement Elo). En duo on peut
servir deux profils à la fois ; sinon on compare les réponses stockées dans une page de vote simple. C'est le juge final
entre deux finalistes, et la source de calibration du juge.

## 6. Décisions à prendre

1. **Qui écrit les questions et réponses de référence**, et combien de temps on y met. Estimation : 2 ingénieurs × 1 jour pour
   100 questions techniques et RAG (avec réponses et sources), une demi-journée pour 30 grilles d'explication. Sans ça, seuls
   les jeux `synthetique` et publics sont disponibles.
2. **Quel matériel interne on peut utiliser** : un ou deux modules C compilables sur PC avec de bons tests, des specs avec
   leurs exigences validées, une matrice de traçabilité, l'historique des bugs. Et quel framework de tests unitaires l'équipe
   utilise (Unity/Ceedling, CppUTest, GoogleTest, autre), et si Java Card est aussi dans le périmètre.
3. **Juge** : accepter un LLM juge local (le plus gros profil) pour les usages sans vérité objective, à condition de le
   calibrer sur 30 à 50 jugements humains.
4. **Échantillonnage** : passer aux paramètres recommandés par modèle avec 3 tirages (proposé), ou garder la température 0.
5. **Objectif de service** pour la charge : combien d'utilisateurs simultanés viser et quelles latences sont acceptables
   (par exemple TTFT < 3 s pour Open WebUI, < 10 s pour une requête OpenCode à 50 k de contexte).
6. **Créneau** pour l'évaluation complète : une nuit par profil occupe le Spark ; à caler avec l'usage réel.

## 7. Déploiement par étapes

**Phase 0 : hygiène des résultats** (1 à 2 jours, prérequis de tout le reste)
- Métadonnées du § 2.5 (dont la configuration RAG), plus aucune exécution écrasée dans le rapport, dossier
  `WS_DIR/evals-internes/`, `--tirages`, jetons de raisonnement et `finish_reason` enregistrés.
- Métriques KV et préemptions ajoutées au rapport.

**Phase 1 : ce qui sert tout de suite** (1 semaine)
- 50 premières questions techniques réelles (débloque l'issue #12) et les qrels de 50 questions RAG.
- `eval-rag.py` : recherche seule, comparaison de 3 ou 4 configurations d'index. Pas besoin de changer de modèle, donc
  rapide à itérer et utile même avant de choisir le LLM.
- `bench-charge.sh` avec profils de requêtes réalistes et objectif de service : répond à « combien d'utilisateurs ».

**Phase 2 : code C, objectif et automatique** (1 à 2 semaines)
- Image `eval-c`, écriture de code (fonctions retirées de nos modules), bugs injectés par mutation, couverture gcov
  (analyse et génération de tests). Tout est noté par exécution : aucune dépendance au juge.

**Phase 3 : documentation, exigences, cohérence** (2 semaines)
- `juger.py` et calibration humaine du juge.
- Doc de code (aller-retour avec les commentaires existants), explication, extraction d'exigences, traçabilité, cohérence
  doc/code avec incohérences injectées.

**Phase 4 : contexte long, raisonnement, agent** (au fil de l'eau)
- RULER et contexte long sur notre code, budgets de raisonnement, pression KV en duo.
- Évaluation en mode agent (OpenCode non interactif sur des tâches de bug et de code), puis SWE-bench Multilingual.
- Arena Open WebUI entre les deux finalistes.

À l'issue de la phase 2, le rapport donne déjà une réponse par usage pour le code, le RAG et la capacité, ce qui couvre
l'essentiel du choix de modèle.
