# Vérification de la version 0.4.1

Correctif d’installation du 26 septembre : le script choisit maintenant le dossier application avant les appels sous le compte `duopilot`, afin d’éviter le dossier courant inaccessible `/home/ubuntu`. Vérification Bash de ce correctif effectuée ; pas de modification du moteur applicatif.

Vérification locale le 26 septembre 2026 : **143 tests réussis**, avec pytest, sous Linux. Les 119 tests de la version 0.4.0 sont conservés. Git 2.51.1 a été utilisé sur de vrais dépôts temporaires ; les connexions IA, Docker et GitHub ont été remplacées par des doubles dans les tests.

Commande :

```text
python -m pytest -q
143 passed
```

## Parcours vérifiés

- VPS : cycle quotidien mis en file sans appel IA, création idempotente, traitement ultérieur par le worker et contrôles préalables du mode réel.
- Configuration guidée : secret de session stable, fichier privé remplacé atomiquement, jeton absent des sorties, refus des liens et de la reconfiguration pendant des travaux ou avec le moteur automatique actif.
- Sauvegarde SQLite réelle, y compris transactions présentes dans le WAL ; restauration et comparaison, exclusion des transactions non validées, rétention des sept archives, permissions et refus des liens.
- Snapshot Docker sous umask 0077 : fichiers imbriqués lisibles et dossiers traversables sans ouvrir les dossiers privés parents. Le changement effectif d’identité vers UID 65534 est interdit dans l’environnement de vérification : les permissions sont vérifiées, pas un conteneur réel.
- Syntaxe des scripts Bash et unités systemd, interprétation du calendrier Europe/Paris. Les binaires de `/opt/duopilot` ne sont pas installés ici ; la vérification systemd signale leur absence attendue.

- Cycles quotidiens conservés, cycles supplémentaires à un ou deux sujets, six ou douze messages, double envoi idempotent et alternance des rôles.
- Réglages persistants, limites invalides refusées, conflits entre deux formulaires, modèles transmis aux deux CLI et instantanés des paramètres.
- Vote exact sur les sous-tâches et leurs dépendances ; cartes enfant créées avec les bons rôles et liens ; une dépendance portée par un parent reste applicable à ses descendants.
- Chaîne complète sur un dépôt Git temporaire : patch réel, commit, résultat de test simulé, revue simulée, publication simulée et passage de la carte à contrôler.
- Test échoué ou refus du contrôleur suivi d’une correction, de nouveaux tests et d’une nouvelle revue ; publication du seul commit testé et approuvé.
- Appel de modèle échoué compté avant son lancement ; plafond atteint ; reprise explicite après augmentation ; lectures du développeur également comptées.
- Pause, durée cumulée, modification humaine en cours de mission, exclusivité de prise en charge d’un travail et d’un dépôt.
- Reprise d’un patch déjà présent dans l’index ou déjà committé ; conservation de changements imprévus ; publication reprise après une réponse distante perdue, sans rappeler les modèles.
- Capacité de trois travaux, retours de PR, reprise sur la même branche, dépendance débloquée par une fusion constatée et progression du parent.
- Fusion constatée sans écraser une nouvelle précision humaine sur la carte.
- Refus des chemins sensibles, traversants, workflows, liens et binaires ; credentials absents des arguments Git et du contexte des CLI ; push vers une seule branche générée.
- Vérification des arguments Docker : absence de réseau, copie montée en lecture seule, utilisateur non privilégié, limites de ressources et suppression du conteneur. Changement de commit pendant les tests refusé.
- API de fusion, suppression de branche, modification de protection et autres mutations exclues ; délais de commande bornés et erreurs sans sortie brute.
- Préparation des images limitée aux fichiers de dépendances prévus, avec exclusions des chemins externes et absence de copie de `.env`.
- Routes Flask, protection CSRF, simulation de mission, affichage du journal et exports JSON.
- Migration depuis une base au schéma exact de 0.3 : comparaison de toutes les anciennes colonnes des cycles, sessions, messages, cartes, retours et liens avant/après ; deuxième démarrage idempotent et références étrangères intactes. Les tests historiques de migration 0.1 restent présents.

## Ce qui reste à vérifier sur le PC ou le VPS de Cyril

Aucun nouvel appel à ses abonnements, aucune création de PR réelle, aucun build ou lancement de conteneur réel et aucun essai sous Windows n’ont été effectués pendant cette vérification. L’installation complète par apt, le démarrage de Gunicorn/systemd et les connexions sur Ubuntu 26.04 ne sont pas testés ici. Les commandes non interactives et nouveaux prompts doivent être essayés avec les versions installées des CLI. Les écrans sont vérifiés par le client de test Flask ; leur rendu dans un navigateur Windows n’a pas été inspecté ici.

Le fonctionnement des tests de CS-PILOT dépend de son image, de ses dépendances, de sa commande et des éventuels services de test nécessaires. Cette archive ne contient pas son code ni sa base métier. Une commande qui retourne zéro sans test pertinent ne prouve pas la qualité du changement.

Premier essai conseillé : sauvegarder l’installation existante, démarrer la démo, préparer une image de test adaptée, puis lancer une petite mission réelle depuis une carte. Examiner le journal, les tests et le diff de sa PR avant toute fusion. L’exécuteur est désactivé par défaut et doit être activé dans Configuration après cette préparation.
