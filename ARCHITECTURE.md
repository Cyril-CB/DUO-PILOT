# Architecture de DUO PILOT 0.1.1

## Périmètre

Le programme coordonne deux sujets de délibération par jour. Un accord produit un plan enregistré. **Aucune étape d'exécution de ce plan n'est encore implémentée.** GitHub est consulté par requêtes GET ; aucun composant n'écrit sur un dépôt, ne crée de PR et ne fusionne.

La démo produit un scénario déterministe et fictif sans connexion externe. Le mode réel demande à Codex CLI (acteur A, OpenAI) et Claude Code (acteur B, Anthropic) de répondre à un contexte fourni. Les outils officiels gèrent eux-mêmes leur authentification d'abonnement.

## Organisation

| Élément | Rôle |
| --- | --- |
| `app.py` | Point d'entrée local, port 5055. |
| `duopilot/__init__.py` | Fabrique Flask, configuration et initialisation. |
| `duopilot/web.py` | Tableau de bord, file d'attente, journal, export et contrôle local. |
| `duopilot/cli.py` | Commandes `daily`, `worker`, `doctor` et `mark-interrupted`. |
| `duopilot/orchestrator.py` | Enchaînement des tours, mémoire courte et accord sur le plan. |
| `duopilot/roles.py` | Alternance entre sujets et jours ; ouverture, développement prévu et contrôle. |
| `duopilot/providers.py` | Fournisseur démo et adaptateurs des deux CLI. |
| `duopilot/github.py` | Collecte GitHub bornée et exclusivement en lecture. |
| `duopilot/db.py` | Schéma SQLite, connexions et horodatages UTC. |
| `duopilot/templates/`, `duopilot/static/` | Interface locale. |
| `instance/` | Base SQLite locale et fichiers associés, hors Git. |

## Un cycle

La clé `(run_date, mode)` est unique dans SQLite. La date métier est calculée en Europe/Paris ; les horodatages techniques sont enregistrés en UTC. Le cycle possède deux sessions, `cspilot` et `self`.

1. L'interface ou la commande `daily` crée le cycle en attente, s'il n'existe pas déjà.
2. Le worker ou `daily` revendique atomiquement le cycle en passant de `queued` à `running`. Un deuxième processus ne peut pas revendiquer la même ligne.
3. Pour chaque sujet, le programme construit un contexte GitHub ou fictif et ajoute au maximum trois décisions antérieures du même mode.
4. Les agents alternent pendant six tours. Le premier dépend de la date et du sujet : les deux missions ont des premiers intervenants opposés, puis les rôles s'inversent le lendemain. Chacun obtient trois interventions par sujet. L'ouvreur est le développeur prévu, son interlocuteur le contrôleur prévu ; aucune exécution n'est encore déclenchée.
5. Le cinquième tour fournit un plan structuré validé localement. Le programme calcule son identifiant sur sa représentation JSON canonique.
6. Le sixième tour doit accepter cet identifiant exact. Il ne peut pas accepter implicitement une variante du plan.
7. Le résultat devient `agreed`, `no_action` ou `rejected`, ou `failed` en cas d'erreur.

La réservation de chaque intervention est enregistrée **avant** l'appel au fournisseur. La contrainte unique `(session_id, turn)` et l'absence de reprise automatique empêchent de rejouer silencieusement un tour après un arrêt. Une réservation en échec reste comptée comme une tentative.

Le premier intervenant est enregistré dans `sessions.opening_actor` dès la création d'un nouveau cycle. Les rôles sont transmis aux deux agents, affichés dans le journal et inclus dans l'export JSON. La migration depuis 0.1 ajoute cette colonne sous un verrou d'écriture SQLite et retrouve l'ouvreur des anciennes discussions dans leur message numéro 1. Elle ne réattribue pas leurs rôles selon la nouvelle règle et ne modifie aucun message, décision, identifiant ou état. Une ancienne session sans message reste sans attribution jusqu'à son éventuel démarrage.

En mode réel, un cycle d'une ancienne date est refusé avant le premier appel. La date est également contrôlée avant chaque intervention : un passage à minuit termine le sujet sans lancer de nouveau tour de l'ancienne journée. Un appel commencé avant minuit peut cependant se terminer après.

Les deux sessions sont traitées successivement. Une erreur dans l'une n'empêche pas nécessairement le traitement de l'autre. Un cycle avec au moins un échec est marqué `failed`, même si son autre session a abouti.

## Accord explicite

Le plan contient une action parmi `investigate`, `fix_bug`, `feature`, `improve_process`, `review` et `none`, un titre, une justification, un périmètre et des critères de réussite. Les chaînes et les listes sont bornées et contrôlées côté application.

L'identifiant est dérivé de SHA-256, tronqué à vingt caractères hexadécimaux. Il sert à vérifier que les deux derniers messages parlent du même plan ; il ne constitue ni une signature d'identité, ni une preuve de qualité du plan. Une acceptation d'un autre identifiant est un refus. Une action `none` acceptée devient `no_action`.

## Mémoire et contexte

La mémoire ne conserve dans le prompt que la date, le statut et le titre des trois dernières décisions du sujet dans le même mode. Elle indique explicitement qu'il s'agit de décisions proposées et non de travaux réalisés. Les échanges complets restent dans le journal SQLite.

Le journal n'a aucune purge automatique. L'accueil pagine les cycles par groupes de 30, avec accès aux cycles antérieurs, à leur détail complet et à leur export. Cette pagination ne réduit pas les données conservées et n'augmente pas le contexte envoyé aux modèles.

GitHub fournit un instantané partiel : jusqu'à vingt entrées issues, dont les PR sont retirées, quatre PR ouvertes et cinq commits. Les branches de lecture sont configurables. Ni le code, ni les diffs, ni les commentaires, ni la CI ne sont consultés. Le plafond de trois PR est transmis aux agents comme règle de sélection ; un futur exécuteur devra l'appliquer lui-même avant toute création de branche ou PR.

Les contenus des dépôts sont des données externes non fiables. Ils ne doivent pas modifier les règles de l'application. Le client GitHub utilise une origine fixe, refuse les redirections et borne les délais et la taille des réponses. Les erreurs ne recopient pas les corps HTTP ni les identifiants de connexion.

## Connexions et protections

Chaque CLI est authentifié séparément par l'utilisateur. L'application n'implémente pas de flux OAuth et n'extrait pas de jeton de connexion des fichiers des fournisseurs. Les modèles sont configurables ; une valeur vide conserve le défaut de l'outil officiel.

Les appels de délibération ne doivent disposer d'aucun outil de modification ou d'exécution de code. Les options exactes sont centralisées dans l'adaptateur et doivent être revérifiées lorsque les CLI évoluent. Le mode réel doit être testé sur la machine cible après installation des outils ; les tests avec doublures ne remplacent pas cette vérification.

L'adaptateur lance chaque appel sans shell, dans un dossier temporaire. Il retire de l'environnement transmis les clés API et secrets applicatifs, force les connexions d'abonnement, désactive les outils exposés et refuse les configurations Codex globales identifiées comme incompatibles. Les connexions existantes restent sous la gestion des CLI officiels ; aucune configuration d'authentification n'est copiée ou réécrite par l'application. Ces mesures ne remplacent pas une isolation système complète : un compte système dédié est recommandé si la configuration habituelle contient des extensions.

Le jeton GitHub, éventuellement nécessaire pour un dépôt privé, est utilisé par le collecteur et ne fait pas partie du contexte transmis aux modèles. La configuration est locale ; les vues ne révèlent pas sa valeur. Les erreurs de subprocess ne sont pas retransmises brutes dans le journal.

Le serveur écoute uniquement en local, vérifie l'adresse cliente et les hôtes attendus, et protège les formulaires POST par un jeton CSRF. Ces contrôles conviennent au prototype personnel ; ils ne fournissent pas l'authentification nécessaire à un service public ou partagé.

## Ce que mesurent les compteurs

Un message représente une intervention du protocole, pas une quantité fixe de travail du modèle. Les appels structurés et les traitements internes du CLI peuvent consommer davantage qu'un simple texte visible. Le timeout borne l'attente, pas un coût financier garanti. Les compteurs de tokens absents restent inconnus ; ils ne sont pas interprétés comme zéro consommation.

## Évolution vers le développement autonome

L'exécuteur futur devra être séparé du coordinateur et obtenir une autorisation technique limitée à l'action acceptée. Avant d'écrire, il devra lire le code, confirmer le contexte à jour, vérifier les PR en cours et appliquer le plafond de travaux non intégrés.

Le parcours prévu est une branche isolée depuis `dev`, une modification bornée, des tests pertinents, une revue par l'autre IA puis une PR vers `dev`. Les deux dépôts auront leurs règles de protection ; aucun agent ne devra disposer du droit de fusionner ou de contourner ces règles. Les droits, les plafonds et les règles d'activation resteront hors de leur périmètre de modification.

Une auto-amélioration sera un changement proposé dans le dépôt DUO PILOT. Après revue et fusion humaine, une procédure d'activation séparée démarrera la version validée. Le processus courant ne chargera pas automatiquement les modifications de sa propre branche de travail.

Les points à ajouter avant ce stade sont le checkout isolé, l'identité GitHub limitée, le contrôle des actions autorisées, la validation des tests et des diffs, le suivi des retours de PR, un budget d'exécution distinct et une procédure de récupération après interruption.
