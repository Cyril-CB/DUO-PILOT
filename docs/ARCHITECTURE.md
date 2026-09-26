# Architecture de DUO PILOT 0.4.1

## Installation VPS

Les services systemd de `deploy/` partagent le compte `duopilot`, les connexions CLI et la base SQLite. Gunicorn écoute uniquement sur la boucle locale ; SSH fournit l’accès depuis le PC. Un seul worker exécute les travaux. Le timer appelle `daily --enqueue-only`, sans second processus de discussion. La sauvegarde utilise l’API SQLite, sans arrêter le worker. Les horaires sont exprimés en Europe/Paris.

Le compte dispose du moteur Docker local pour les tests. Son `UMask=0077` conserve les données privées ; seuls les instantanés destinés aux conteneurs ont leurs répertoires traversables et leurs fichiers lisibles. Le worker ne doit pas utiliser `PrivateTmp=true` : le daemon Docker doit voir les mêmes chemins temporaires à monter.

## Composants

| Module | Responsabilité |
| --- | --- |
| `web.py`, `kanban_web.py`, `execution_web.py` | Interface locale, formulaires CSRF, consultation, exports et commandes mises en file. |
| `settings.py` | Réglages humains persistants, validation et instantanés sans secrets. |
| `orchestrator.py`, `roles.py` | Six messages par sujet, alternance et vote sur un plan exact. |
| `providers.py` | CLI officiels, connexions d’abonnement, schémas JSON, délais et arrêt des processus. |
| `github.py`, `repository.py` | Contexte initial en lecture, documentation et extraits fixés à un commit. |
| `kanban.py` | Cartes, historique, versions optimistes, sélection bornée et découpage. |
| `executor.py` | Machine à états, budgets cumulatifs, reprise, suivi de PR et dépendances. |
| `execution_prompts.py` | Contrats de patch, lecture et revue des agents. |
| `execution_io.py` | Git contrôlé, validation des patches, tests Docker et API de PR. |
| `test_images.py` | Préparation explicite des images par l’utilisateur. |
| `cli.py` | Worker, discussions directes, diagnostics et préparation des tests. |
| `db.py` | Schéma SQLite, migrations et transactions. |

## Discussions et accord

La clé quotidienne est `(run_date, mode, sequence=0)`, avec date Europe/Paris. Les cycles supplémentaires ont une séquence positive, un nonce unique et un ou deux sujets. Le nonce empêche le double envoi du formulaire de créer deux cycles. Chaque session conserve son premier intervenant ; l’autre sujet utilise l’agent opposé et les cycles supplémentaires alternent les rôles.

Le cycle est revendiqué atomiquement. Chaque intervention est réservée avant appel et n’est pas rejouée automatiquement. Les tours 1–4 discutent et peuvent demander du contexte ; le tour 5 propose un plan ; le tour 6 accepte son identifiant exact ou refuse. L’identifiant est un SHA-256 tronqué à vingt caractères de la représentation canonique. Il inclut le rattachement à une carte et le détail des sous-tâches. Il prouve l’identité des données votées, pas la justesse du travail.

Le contexte partagé contient au plus huit cartes dans 4 000 caractères et le contexte GitHub dans le reste de l’enveloppe de 12 000 caractères, plus la courte mémoire. Les contenus du dépôt, commentaires et cartes sont des données non fiables. L’application fournit des lectures contrôlées ; les CLI n’obtiennent pas d’outils directs. Les requêtes de discussion sont exclusivement des GET GitHub sur une origine fixe ; les blobs sont vérifiés par leur empreinte Git.

## Cartes et découpage

Les tables `tasks`, `task_events` et `task_decisions` conservent les demandes, plans et historiques. Un accord, sa carte, ses enfants et ses événements sont inscrits dans la même transaction. Le numéro de version lu au début du sujet doit encore correspondre lors du vote ; sinon le plan reste dans la conversation avec un conflit, sans écraser la carte.

L’action `split` comprend 2–6 plans de feuilles ; un indice `depends_on` vaut zéro ou désigne un enfant précédent. L’application transforme ces indices en références de cartes, ce qui empêche les cycles entre les nouveaux enfants. La hiérarchie est limitée à trois niveaux. Une carte déjà découpée ou ayant une exécution active ne peut être redécoupée. Seules les feuilles apparaissent dans la sélection de travail. Elles héritent des rôles de l’accord ; chacune possède ensuite son propre budget.

Une dépendance est prête si sa carte est terminée et n’a plus d’exécution active. Les fusions distantes constatées terminent les cartes non modifiées depuis le travail, puis les parents dont tous les enfants sont terminés. Les changements humains plus récents sont conservés. Une PR fermée sans fusion ne termine pas la carte.

## Exécution

Une mission acceptée devient une ligne `executions`, accompagnée d’un instantané des réglages, du plan, des rôles et de la version de carte. Les états actifs sont uniques par carte ; une seule mission peut être `running` par dépôt. La prise en charge est transactionnelle et possède un identifiant de worker.

Phases : `prepare → develop → apply → tests → review_code → publish → done`. Une demande de lecture revient au même agent ; une correction retourne à `develop`. Les statuts distinguent attente, travail, pause, blocage, PR ouverte, fusion constatée, fermeture, abandon et simulation.

- `prepare` vérifie la capacité, fixe le digest de l’image locale et crée une copie Git depuis la branche configurée.
- `develop` attend un patch, une demande de lecture ou un constat de blocage. Les réponses ne sont jamais exécutées comme des commandes shell.
- `apply` utilise un index Git temporaire pour calculer l’arbre attendu avant toute application. Le checkpoint conserve le commit précédent, cet arbre et le patch.
- `tests` exécute la commande humaine dans Docker. Le commit et la propreté du dossier sont contrôlés avant et après les tests.
- `review_code` présente le diff complet et le résultat des tests à l’autre agent. L’approbation n’est retenue que si les tests sont réussis sur le commit actuel.
- `publish` exige `HEAD = tested_sha = approved_sha` et un dossier propre. Il pousse exactement ce commit vers la branche générée, puis crée ou retrouve sa PR.

Après une interruption, un patch déjà committé n’est pas appliqué à nouveau ; un patch exactement présent dans l’index peut être committé. Une divergence conserve le travail et bloque la mission. Une réponse GitHub perdue après publication est récupérable en retrouvant la PR par sa branche. La publication peut être reprise sans nouvel appel IA. L’application ne force jamais un push et ne réécrit pas une branche de base.

## Budgets et contrôle humain

`app_settings` contient uniquement des paramètres validés. Ils sont lus à la mise en file, et à chaque reprise explicite ; les secrets restent dans la configuration du processus. Les agents ne disposent d’aucune route permettant de modifier ces réglages.

Chaque appel du développeur consomme une tentative, y compris une lecture ou une erreur. Les appels du contrôleur sont comptés séparément et le total reste limité à quatre fois le plafond de tentatives. Le contrôleur dispose de deux demandes de contexte par version. Les événements sont réservés avant l’appel, puis complétés ; les réservations interrompues restent consommées.

Le budget de temps utilise une horloge monotone durant l’exécution et une durée cumulée en base. Un contrôle périodique examine propriétaire, statut, demande de pause, interrupteur global, version de carte et délai. Le heartbeat est persisté environ toutes les deux secondes ; un arrêt brutal peut perdre le dernier intervalle. Les compteurs persistent aux reprises. Les timeouts ne mesurent pas les tokens ou la consommation interne des CLI.

La préparation des images est une commande humaine distincte, hors budget des missions. Elle installe des dépendances avec réseau. Les tests autonomes réutilisent une image locale fixée par digest et ne téléchargent rien.

## Accès et isolation

Le coordinateur est un processus local de confiance. Il gère les jetons GitHub sans les transmettre aux modèles ou aux conteneurs. Git est lancé sans shell, avec configuration globale/système ignorée, hooks et assistants de connexion désactivés. L’authentification HTTP du push est injectée dans l’environnement du processus Git, pas dans sa ligne de commande ou la configuration du dépôt.

Les patches sont textuels, bornés à vingt fichiers et 80 000 caractères. Les chemins traversants, fichiers de contrôle Git, secrets usuels, données locales, workflows, liens et sous-modules sont refusés. Le diff complet soumis au contrôleur est également borné. Les extensions prises en charge ciblent les sources Python/web, SQL, JSON et la documentation.

Les tests reçoivent une copie filtrée des fichiers suivis par Git. Le conteneur a une racine en lecture seule, un utilisateur non privilégié, un répertoire temporaire limité, des capacités supprimées et aucun réseau. Le dépôt est monté en lecture seule puis copié dans `/tmp/project` pour permettre les écritures de test. Les connexions personnelles, `.git` et les données locales ne sont pas montées. Cela dépend de Docker ; ce n’est pas une preuve d’isolation absolue contre une faille du moteur ou du noyau.

L’API GitHub autorise seulement les lectures de PR et de commentaires, la création de PR et la modification du seul corps d’une PR existante. Aucune route de fusion, fermeture, suppression, déploiement ou modification de protection n’est proposée. Les permissions d’un jeton d’écriture peuvent néanmoins permettre une fusion en dehors de ce code ; l’interdiction technique au niveau du compte GitHub dépend des règles de branches et des permissions configurées par l’utilisateur.

Les API sont à origine fixe, sans redirection, avec taille et durée bornées. La limite de trois travaux compare les PR ouvertes et les branches locales préparées non terminées, avant préparation et avant publication. Les autres acteurs GitHub ne sont pas inclus dans un verrou distribué.

## Worker et suivi

Le worker traite successivement les synchronisations demandées, les cycles et les missions. Il met les cartes acceptées en file lorsque les deux interrupteurs sont activés. Les missions bloquées ne sont pas relancées automatiquement. Environ toutes les cinq minutes, il programme une lecture d’état des PR ouvertes connues ; aucun appel IA n’est nécessaire.

Les retours GitHub sont importés explicitement avant de retravailler une PR. La collecte est bornée, sans pagination complète ni import des checks CI. Le travail se poursuit sur la même branche avec les mêmes compteurs. Une action extérieure sur la branche ou la PR bloque la publication lorsqu’elle ne correspond plus à l’état attendu.

Le worker ne crée pas lui-même le rendez-vous quotidien. `daily` ou l’interface créent les cycles ; le planificateur de l’utilisateur peut appeler `daily`. Une journée réelle antérieure est refusée et aucun nouveau message n’est commencé après son passage à minuit.

## Historique et migrations

Le journal SQLite conserve les conversations, contextes, décisions, cartes et événements d’exécution ; les anciens messages ne sont pas recalculés. `execution_events` contient les prompts et réponses structurées, lectures, commits, tests, publications et arrêts. Les exports sont locaux ; les journaux peuvent contenir du code et des informations confidentielles du dépôt. Il n’existe pas de purge automatique.

La migration 0.4 reconstruit la seule table `cycles` pour élargir sa clé unique, en conservant ses identifiants et colonnes originales. Les références étrangères sont désactivées temporairement hors transaction, la migration est sérialisée avec `BEGIN IMMEDIATE`, puis `foreign_key_check` vérifie les liens avant réactivation. Les colonnes de sous-tâches et tables d’exécution/réglages sont additives. Les tests incluent une base au schéma exact de 0.3 et la migration historique de 0.1.

Une auto-amélioration utilise le même mécanisme dans une copie distincte de DUO PILOT. Aucun rechargement du code développé n’a lieu dans le processus actif. L’installation d’une nouvelle version reste une action humaine.
