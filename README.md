# DUO PILOT 0.4.1

**Installation sur le VPS Ubuntu : commence par [deploy/README-VPS.md](deploy/README-VPS.md).** Le kit contient l’installateur, les services, la connexion des CLI sous un compte dédié et les sauvegardes. Les instructions Windows ci-dessous restent applicables.

Deux agents, OpenAI via Codex CLI et Anthropic via Claude Code, choisissent ensemble des missions pour **CS-PILOT** et pour **DUO PILOT**. Application personnelle locale en Python 3.11+, Flask et SQLite.

Cette version relie les discussions au travail : **lecture du dépôt → plan accepté → patch → tests → contrôle par l’autre agent → pull request**. L’agent qui ouvre un sujet développe ; l’autre contrôle. La fusion et l’installation d’une nouvelle version restent manuelles.

Les nouveautés :

- Limites de durée et de tentatives réglables depuis **Configuration**.
- **Un tour supplémentaire** à la demande, sur un projet ou les deux, avec un historique distinct.
- Découpage d’une carte en **deux à six sous-tâches**, éventuellement dépendantes.
- Choix du modèle de chaque agent dans l’application.
- Exécutions suivies, mises en pause et reprises, avec journal, tests, revue et lien vers la PR.

Le moteur réel est **désactivé au départ**. Les discussions et le Kanban restent utilisables immédiatement. Les connexions officielles déjà fonctionnelles sont conservées.

## Mettre à jour la version existante

1. Arrête le serveur et le worker. Sauvegarde le dossier actuel `duo_pilot` complet.
2. Recopie le contenu du dossier `duo_pilot` de cette archive dans ton dossier actuel, en remplaçant le code. **Conserve ton `.env` et tout ton dossier `instance/`.** L’archive ne contient aucun de ces fichiers personnels.
3. Dans ton environnement Python habituel :

```powershell
python -m pip install -r requirements.txt
python app.py
```

La migration SQLite s’effectue au démarrage. Elle conserve les conversations, votes, cartes, retours, identifiants et rôles historiques des versions 0.1 à 0.3. Aucun ancien cycle n’est rejoué. Conserve la sauvegarde : revenir à une ancienne version exige de restaurer aussi sa base, serveur et worker arrêtés.

Pour que les agents travaillent sur le code de cette version, reporte-le également dans le dépôt configuré par `SELF_REPO` et sa branche `SELF_BASE_BRANCH`. Le code exécuté dans une mission vient de GitHub ; il ne vient pas de ton dossier d’installation.

## Démarrage neuf ou démo

Dans le dossier extrait, avec PowerShell :

```powershell
py -3 -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item .env.example .env
python app.py
```

Ne recopie pas `.env.example` sur ton `.env` lors d’une mise à jour.

Ouvre [http://127.0.0.1:5055](http://127.0.0.1:5055). Lance la démo, ouvre une carte de démonstration, puis **Simuler l’exécution**. Dans un second terminal, même dossier et environnement Python :

```powershell
python -m flask --app app worker
```

La démo n’appelle aucun modèle, GitHub ou Docker. Les réponses, travaux et résultats sont fictifs ; elle sert à découvrir le parcours. Les cartes démo et réelles sont séparées.

Sous Linux, remplace l’activation par `source .venv/bin/activate`, et utilise `python3` pour créer l’environnement si nécessaire.

## Tes agents et leurs modèles

L’application utilise les connexions des **CLI officiels**, préparées par toi sur la machine du worker. Elle ne demande pas de clé API IA. Garde les chemins et l’authentification qui fonctionnent déjà.

Exemple de configuration Windows, à adapter :

```dotenv
USE_SUBSCRIPTION_CLI=true
CODEX_BIN=codex
CLAUDE_BIN=C:/Users/Cyril/.local/bin/claude.exe
CODEX_HOME=C:/Users/Cyril/.duopilot-codex
CSPILOT_REPO=ton-compte/ton-depot-cspilot
CSPILOT_BASE_BRANCH=dev
SELF_REPO=ton-compte/ton-depot-duo-pilot
SELF_BASE_BRANCH=main
GITHUB_TOKEN=
GITHUB_WRITE_TOKEN=
```

Les dépôts et les branches doivent exister. `CODEX_HOME` peut rester celui dédié que tu as déjà connecté avec ChatGPT. Le connecteur refuse les configurations Codex personnelles comportant des MCP, hooks, plugins ou fournisseurs incompatibles avec ce fonctionnement. Il ne modifie pas tes connexions.

Dans **Configuration** :

- **Modèle OpenAI · Codex** : identifiant disponible dans ton CLI et ton abonnement.
- **Modèle Anthropic · Claude** : identifiant ou alias disponible, par exemple `sonnet` ou `opus`.
- **Champ vide** : choix par défaut du CLI correspondant.

Les réglages enregistrés dans l’application prennent priorité sur `CODEX_MODEL`, `CLAUDE_MODEL` et `CLI_TIMEOUT` de `.env`. Ils sont figés à la mise en file d’un cycle ou d’une mission. Une reprise manuelle adopte les réglages actuels et conserve les compteurs. Le programme ne dresse pas un catalogue de modèles et ne vérifie pas leur disponibilité en appelant les IA ; un modèle refusé par le CLI produit une erreur dans le journal.

Vérifications locales :

```powershell
codex --version
& "$env:USERPROFILE\.local\bin\claude.exe" --version
python -m flask --app app doctor
```

`doctor` vérifie la configuration, pas les droits, les quotas ni les connexions réelles. Pour une nouvelle installation, authentifie séparément `codex login` et Claude Code. Windows et WSL ne partagent pas automatiquement leurs connexions.

## Activer le développement et les PR

Il faut **Git**, **Docker avec un moteur Linux démarré**, une image de tests par projet et un jeton GitHub adapté. Docker sert à exécuter le code de test dans un conteneur ; les modèles proposent des patches et n’obtiennent pas un terminal libre.

### 1. GitHub

`GITHUB_TOKEN` reste le jeton de lecture du contexte : Contents, Issues et Pull requests en lecture pour les dépôts privés.

Ajoute dans `.env` un `GITHUB_WRITE_TOKEN` limité aux dépôts concernés, avec **Contents : lecture/écriture** et **Pull requests : lecture/écriture**. Il sert au coordinateur pour pousser une branche et créer ou mettre à jour la description de sa PR. Il n’est transmis ni aux modèles ni aux conteneurs de tests. Redémarre les deux processus après une modification de `.env`.

Le code de DUO PILOT ne comporte aucune opération de fusion, de déploiement ou de modification des protections GitHub. Toutefois, les permissions d’un jeton d’écriture peuvent permettre davantage que ce que le programme utilise. La limitation du jeton ne suffit donc pas à garantir une interdiction de fusion au niveau de GitHub : garde les règles et protections des branches sous ton contrôle et n’accorde pas de contournement au compte technique.

### 2. Images de tests

Vérifie les outils :

```powershell
git --version
docker version
```

Prépare l’image de DUO PILOT :

```powershell
python -m flask --app app prepare-tests --topic self
```

Pour CS-PILOT, indique un fichier de dépendances provenant d’une copie de ton projet et que tu as examiné :

```powershell
python -m flask --app app prepare-tests --topic cspilot --requirements "C:/chemin/CS-PILOT/requirements-dev.txt"
```

Utilise `requirements.txt` si le projet n’a pas de `requirements-dev.txt`. Le préparateur accepte les dépendances PyPI et les inclusions `-r`/`-c` de fichiers `.txt` dans le même dossier ou ses sous-dossiers. Il construit une image Python 3.12 avec Git, pytest et ces dépendances. **Cette construction utilise le réseau et installe des dépendances.** Elle est lancée explicitement par toi, jamais par une réponse d’agent. Elle peut prendre plusieurs minutes.

`--write-only` prépare le dossier et le Dockerfile sans construire l’image. Les fichiers se trouvent dans `instance/test-images/`. Aucun `.env`, code applicatif ou dossier de données local n’est envoyé dans le contexte de construction.

Si CS-PILOT exige une autre version de Python, des bibliothèques système, des dépendances privées, un navigateur ou une base de test particulière, adapte le Dockerfile produit puis construis l’image avec `docker build --tag cspilot-tests:local "CHEMIN_DU_DOSSIER"`. L’image doit contenir la commande `python`, les dépendances et les outils nécessaires. Les tests ne peuvent joindre aucun serveur externe. Utilise des données et services de test locaux au conteneur.

Dans **Configuration → Délais des appels et tests**, règle l’image et la commande de chaque dépôt. Exemples de commandes, saisies comme tableaux JSON d’arguments :

```json
["python", "-m", "pytest", "-q"]
```

```json
["python", "-m", "unittest", "discover", "-s", "tests"]
```

La commande doit réellement vérifier le projet. Un retour zéro est considéré comme réussi ; le contrôleur examine aussi les résultats et les critères. Ce prototype ne prouve pas à lui seul la pertinence ou l’exhaustivité des tests.

### 3. Mise en route

Dans **Configuration**, active **Activer le développement, les tests et la publication de PR**. Choisis si le worker doit **mettre automatiquement en file les nouvelles missions retenues**. Ce choix concerne toutes les cartes réelles déjà dans « Plan retenu » et encore sans exécution active ; inspecte cette colonne avant de l’activer.

Avec la mise en file automatique décochée, ouvre une carte acceptée puis clique sur **Lancer / ouvrir l’exécution**. Le worker doit fonctionner dans son second terminal. Commence par une petite mission dont tu peux facilement apprécier le résultat.

Le worker :

1. Vérifie les développements non intégrés et prépare une copie isolée depuis la branche configurée (`dev` pour CS-PILOT par défaut).
2. Crée sa branche `duo/task-…`, lit la documentation et fournit des extraits au développeur.
3. Applique le patch proposé et crée un commit local.
4. Exécute les tests dans Docker, sur une copie des fichiers suivis par Git. Le conteneur n’a pas de réseau, de connexion GitHub/IA ou de montage de tes données locales.
5. Transmet le diff complet et les tests à l’autre agent. Un test échoué ou un refus demande une correction, dans les limites fixées.
6. Pousse le commit et ouvre la PR uniquement si **le même commit** a passé les tests et reçu l’accord du contrôleur.

Le maximum est de trois développements non intégrés par dépôt : les PR ouvertes observées et les travaux DUO PILOT déjà préparés sont comptés. Les PR sont laissées à ton examen. Le programme vérifie à nouveau la capacité avant publication ; une action simultanée faite directement sur GitHub peut cependant changer cet état entre deux vérifications.

## Un tour supplémentaire quand un bug arrive

Sur l’accueil, utilise **Un tour supplémentaire** et choisis **CS-PILOT**, **DUO PILOT** ou **les deux**. Il s’agit d’une nouvelle discussion, pas d’un rejeu du matin : la sélection de cartes et le contexte du dépôt sont actualisés au démarrage du sujet.

- Un projet : six messages, trois par agent.
- Les deux : douze messages, trois par agent et par projet.
- Le premier intervenant s’inverse entre cycles supplémentaires successifs.
- Chaque cycle a son numéro, ses décisions et son historique.
- Un double envoi du même formulaire ne crée pas deux cycles. Recharge l’accueil pour lancer volontairement un autre tour.

Le cycle quotidien reste unique par date Europe/Paris et par mode. Un échec ou une ancienne journée n’est pas rejoué automatiquement. Les tours supplémentaires sont explicites et consomment du quota comme les autres cycles.

Le worker traite les éléments en attente ; il ne crée pas de rendez-vous quotidien de lui-même. Ton planificateur peut continuer à lancer :

```powershell
python -m flask --app app daily --mode live
```

`daily` traite la discussion ; le worker réalise ensuite les missions si le moteur est actif. `worker --once` traite au maximum un élément en attente puis quitte. `execute NUMERO_CARTE` permet de mettre en file et traiter directement une mission acceptée ; en mode réel, cette commande utilise les vraies connexions.

## Kanban et sous-tâches

Tu peux ajouter un bug, une amélioration ou une tâche pour chaque projet, fixer sa priorité, ajouter des retours et consulter toute son histoire. Les agents reçoivent au plus huit cartes ouvertes dans un contexte de 4 000 caractères. Les travaux en cours et les retours passent avant les nouveaux chantiers.

Un plan accepté se rattache à la carte lue ou crée une nouvelle mission. Le titre, la description et la priorité que tu as saisis sont conservés. Si tu modifies la carte pendant la discussion ou le travail, le programme conserve ta modification et signale le conflit.

Les agents peuvent voter un découpage en deux à six sous-tâches, chacune avec un périmètre et des critères. Une sous-tâche peut dépendre d’une étape précédente. Le parent devient une carte de regroupement ; seules les feuilles sont exécutables. Le découpage peut aller jusqu’à trois niveaux au total.

Les sous-tâches héritent du projet, du mode, de la priorité et des rôles du plan accepté. **Chaque sous-tâche a sa propre enveloppe de durée et de tentatives.** Une dépendance attend que sa carte soit terminée et qu’elle n’ait plus d’exécution ou de PR ouverte. Lorsqu’une PR est fusionnée par toi, le worker constate la fusion et peut terminer la carte, puis le parent lorsque tous ses enfants sont terminés. Une carte modifiée depuis le dernier travail garde son état pour que tu l’examines.

Le worker vérifie environ toutes les cinq minutes les PR ouvertes qu’il suit, entre deux travaux. Un travail long peut retarder cette vérification. **Actualiser l’état GitHub** déclenche aussi une vérification en file, sans appel IA. Fermer une PR sans fusion ne marque pas la carte terminée.

Les cartes restent locales ; elles ne sont pas automatiquement créées comme issues GitHub. Les archives, exports JSON et l’import explicite d’un ancien plan depuis sa conversation sont conservés.

## Durées, tentatives et reprises

| Réglage | Défaut | Plage |
| --- | --- | --- |
| Attente par message de discussion | 180 s | 10–600 s |
| Temps actif cumulé d’une mission | 30 min | 1–240 min |
| Appels du développeur par mission | 6 | 1–30 |
| Attente par appel de développement/contrôle | 600 s | 10–1 800 s |
| Durée par commande de test | 180 s | 10–1 800 s |

Une **tentative** est un appel du développeur, y compris une demande d’extrait de code ou un appel qui échoue. Le contrôleur peut demander deux compléments de lecture par version. Le total des appels de développement et de contrôle est borné à quatre fois le nombre de tentatives configuré. Les appels sont réservés et comptés avant le lancement du CLI.

Le temps actif couvre la préparation, les appels, Git et les tests ; les pauses et l’attente d’une revue humaine ne comptent pas. Il est enregistré régulièrement. Après un arrêt brutal, le dernier intervalle non enregistré peut manquer ; ces limites ne sont pas un compteur financier exact. Les opérations de nettoyage peuvent également se terminer après le délai.

Dans **Exécutions**, consulte les messages, patches, résultats, compteurs et erreurs. **Mettre en pause** demande l’arrêt au prochain contrôle et interrompt les commandes actives. **Reprendre avec les réglages actuels** conserve le dossier, la branche, les tentatives et le temps consommé. Si le budget est épuisé, augmente-le explicitement dans Configuration avant de reprendre.

Une erreur de CLI ou de réseau bloque la mission : aucun rappel gratuit ou essai infini. Une reprise après publication partielle vérifie si la PR existe déjà. Une interruption pendant l’application d’un patch est récupérée seulement si l’état Git correspond au résultat attendu ; des changements inattendus sont conservés et bloquent la reprise.

Pour reprendre une PR à partir de tes remarques, clique sur **Récupérer les retours GitHub**, attends leur apparition dans le journal, puis **Retravailler la PR**. La collecte est bornée aux six derniers éléments des trente premiers éléments retournés par chacune des API commentaires, revues et commentaires de code. Elle n’est pas un historique complet des grandes discussions. Les résultats CI de GitHub ne sont pas collectés : les vérifications affichées sont celles du conteneur local.

Après un arrêt brutal du worker, arrête bien l’ancien processus et ses commandes, puis utilise **Marquer l’interruption** après 30 secondes sans signe de vie. Sous Windows, l’arrêt normal appelle `taskkill /T` pour arrêter l’arbre de processus ; ce comportement reste à vérifier sur ta machine.

Pour une discussion interrompue, la commande existante reste disponible :

```powershell
python -m flask --app app mark-interrupted NUMERO_CYCLE
```

Elle clôt la discussion sans rejouer ses messages. Tu peux ensuite lancer un nouveau tour explicite.

## Mémoire, sources et limites

Toutes les conversations et traces restent dans SQLite. Le contexte des modèles reste borné : trois dernières décisions, cartes sélectionnées et extraits utiles. La conservation de l’historique ne signifie pas que tout est renvoyé à chaque appel.

Avant une discussion réelle, jusqu’à deux extraits de documentation sont lus. Les quatre premiers messages peuvent demander du code, au plus huit demandes par sujet, 60 lignes / 1 800 caractères par extrait. Le contexte dépôt + Kanban reste limité à 12 000 caractères par défaut. Les sources du sujet sont fixées à un même commit.

Pour l’exécution, les lectures sont faites dans la branche isolée actuelle : au plus 180 lignes / 10 000 caractères par extrait, avec un contexte d’extraits borné à 30 000 caractères. Un patch est limité à vingt fichiers et 80 000 caractères ; le diff complet soumis à la revue doit tenir dans 80 000 caractères. Les fichiers binaires, renommages, liens, workflows GitHub, environnements et données locales sont exclus. Une grosse mission doit être découpée.

Les commandes de lecture suivantes ne consomment aucun appel IA :

```powershell
python -m flask --app app inspect-repo --topic cspilot
python -m flask --app app inspect-repo --topic self
```

Les messages et délais limitent les lancements et le temps d’attente, **pas les tokens ni la consommation interne des CLI**. Les quotas restent ceux de tes abonnements. Aucun coût en euros n’est déduit d’un abonnement.

L’auto-amélioration produit une PR sur DUO PILOT. Le programme courant continue à utiliser la version installée. Tu examines, fusionnes et installes la nouvelle version toi-même.

## Données et vérification

`instance/` contient la base, les copies de travail et les dossiers d’images. Il reste hors Git avec `.env`. Arrête les processus avant une sauvegarde et conserve le dossier entier. Aucun nettoyage automatique des conversations ou branches n’est effectué.

Usage personnel sur `127.0.0.1:5055`, avec formulaires protégés par CSRF. Il n’y a pas de gestion de comptes ; cette application n’est pas prévue pour être exposée sur Internet.

```powershell
python -m pip install -r requirements-dev.txt
python -m pytest -q
```

Les tests emploient Git local réel et des doubles pour les IA, Docker et GitHub. Ils ne constituent pas une validation des nouvelles fonctions avec tes abonnements, tes dépôts, Docker Desktop ou Windows. Voir [docs/VERIFICATION.md](docs/VERIFICATION.md) et [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Références officielles

- [Codex : exécution non interactive](https://developers.openai.com/codex/noninteractive), [commandes CLI](https://developers.openai.com/codex/cli/reference) et [authentification](https://developers.openai.com/codex/auth).
- [Claude Code : CLI](https://code.claude.com/docs/en/cli-reference) et [exécution non interactive](https://code.claude.com/docs/en/headless).
- [GitHub : API des pull requests](https://docs.github.com/en/rest/pulls/pulls).
- [Docker : options de lancement des conteneurs](https://docs.docker.com/reference/cli/docker/container/run/).
