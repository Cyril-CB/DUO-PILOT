# DUO PILOT 0.1.1

Le départ d'une expérience : **OpenAI et Claude disposent chacun de trois messages pour choisir une action sur CS-PILOT, puis de trois messages pour améliorer DUO PILOT.**

Application personnelle en Python 3.11+, Flask et SQLite. Cette version 0.1.1 met en place la discussion, l'accord sur un plan précis, la répartition des rôles et le journal quotidien. **Elle ne développe pas encore de code et ne crée ni branche, ni commit, ni pull request. Elle ne fusionne rien.**

## Mise à jour depuis la version 0.1

1. Termine ou arrête le cycle en cours, puis ferme le serveur et le worker.
2. Fais une copie de sauvegarde de ton dossier actuel `duo_pilot`, avec son fichier `.env` et son dossier `instance/`.
3. Recopie le contenu du dossier `duo_pilot` de cette archive dans ton dossier actuel, en remplaçant les fichiers de code. **Conserve ton `.env` et ton dossier `instance/` : ils contiennent les connexions configurées et l'historique.** L'archive ne contient ni `.env` ni `instance/`.
4. Relance `python app.py` depuis le même environnement Python. Aucune dépendance supplémentaire n'est nécessaire. La base est mise à jour automatiquement, sans effacer les échanges.

Les cycles terminés conservent leur premier intervenant réel, même si les deux sujets avaient été ouverts par la même IA en 0.1. Ils ne sont pas rejoués. La nouvelle alternance s'applique aux nouvelles discussions. Les attributions affichées indiquent qui développera et qui contrôlera lorsqu'une phase d'exécution sera ajoutée.

## Répartition des rôles et historique

L'IA qui ouvre la discussion sera chargée de développer la tâche retenue ; l'autre contrôlera le résultat. Les deux sujets d'une journée sont ouverts par des IA différentes, et les rôles s'inversent le lendemain. Exemple de rotation :

| Jour | Sujet | Ouvre et développera | Contrôlera |
| --- | --- | --- | --- |
| 1 | CS-PILOT | OpenAI · Codex | Anthropic · Claude |
| 1 | DUO PILOT | Anthropic · Claude | OpenAI · Codex |
| 2 | CS-PILOT | Anthropic · Claude | OpenAI · Codex |
| 2 | DUO PILOT | OpenAI · Codex | Anthropic · Claude |

La date détermine laquelle ouvre CS-PILOT en premier. Chacune a donc au maximum une tâche de développement et une revue prévues par jour, si les deux discussions aboutissent à des tâches. Un refus, un échec ou « aucune action » ne crée pas de travail artificiel.

Tous les messages, contextes, décisions et résultats des votes restent dans le journal SQLite. La répartition des rôles est enregistrée par discussion, affichée sur la page du cycle et incluse dans son export JSON. Les liens « Cycles plus anciens » et « Cycles plus récents » donnent accès à l'intégralité de l'historique, par pages de 30 cycles.

La mémoire envoyée aux IA reste limitée aux trois dernières décisions du sujet et du mode concernés. Conserver toutes les conversations ne signifie pas les renvoyer intégralement aux modèles à chaque lancement.

## Ce qui fonctionne

- Tableau de bord local : cycles, échanges, décisions et état des connexions.
- Deux discussions par cycle : CS-PILOT et DUO PILOT ; six interventions chacune, soit douze au maximum.
- Premier intervenant différent selon le sujet, et alternance quotidienne des rôles.
- Proposition finale structurée au cinquième tour ; acceptation de son identifiant exact au sixième. Un refus ou une décision de ne rien faire est possible.
- Mémoire courte des trois dernières décisions de chaque sujet et du même mode.
- Journal SQLite conservé intégralement, navigation dans tous les cycles et export JSON des échanges et rôles.
- Démonstration avec réponses et situations explicitement fictives, sans appel IA ou GitHub.
- Connecteurs pour les outils officiels Codex et Claude Code, exécutés sur la machine où leurs connexions ont été préparées.
- Lecture limitée du contexte GitHub en mode réel.

Le mode réel est fourni pour connexion et essai sur ta machine. **Il n'a pas été validé ici avec tes abonnements ou tes dépôts.**

## Démarrage : la démo

Dans le dossier extrait du projet, sous Linux, macOS ou un environnement WSL compatible avec les outils officiels :

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
cp .env.example .env
python app.py
```

Ouvre ensuite [http://127.0.0.1:5055](http://127.0.0.1:5055), puis lance une journée de démonstration. Aucune clé ni connexion n'est nécessaire pour cette étape.

Sur Windows, Flask peut également être lancé avec PowerShell :

```powershell
py -3 -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item .env.example .env
python app.py
```

Pour le mode réel, suis les instructions d'installation propres à Codex et Claude Code sur ton système. Les deux commandes doivent être accessibles à Python, sous le même utilisateur que celui qui s'y est connecté. Une installation dans WSL et une installation Windows ne partagent pas automatiquement leurs connexions.

## Brancher les deux abonnements

DUO PILOT utilise les connexions des outils officiels. Il ne collecte aucun mot de passe, cookie ou jeton OAuth de ChatGPT ou Claude. Aucun champ de clé API IA n'est prévu dans `.env`.

1. Installe **Codex CLI** en suivant sa documentation officielle, puis exécute `codex login` et choisis la connexion ChatGPT proposée par l'outil.
2. Installe **Claude Code** en suivant sa documentation officielle, puis démarre `claude` et suis sa connexion interactive avec ton compte Claude abonné.
3. Vérifie dans chaque outil que la connexion correspond bien à l'abonnement souhaité. La présence d'un exécutable ne prouve pas l'authentification ou la disponibilité d'un quota.
4. Renseigne `.env` :

```dotenv
USE_SUBSCRIPTION_CLI=true
CSPILOT_REPO=ton-compte/ton-depot-cspilot
CSPILOT_BASE_BRANCH=dev
SELF_REPO=ton-compte/ton-depot-duo-pilot
SELF_BASE_BRANCH=main
```

Les noms ci-dessus sont des exemples à remplacer. Le dépôt DUO PILOT doit exister et posséder la branche choisie avant une discussion réelle. Ce starter ne crée pas de dépôt GitHub. `main` est la branche de lecture par défaut pour DUO PILOT ; choisis `dev` lorsque tu auras préparé cette branche.

Laisse `CODEX_MODEL` et `CLAUDE_MODEL` vides pour utiliser le choix par défaut de chaque outil. Tu peux ensuite renseigner un identifiant de modèle compatible avec ta version de l'outil et ton abonnement. `CODEX_BIN` et `CLAUDE_BIN` permettent d'indiquer leurs chemins si nécessaire.

Le connecteur refuse une configuration Codex comportant des outils globaux, hooks, plugins, profils ou fournisseurs personnalisés. Dans ce cas, utilise un compte système dédié avec les CLI officiels et leurs connexions par abonnement. Le programme ne modifie pas ta configuration existante. Linux ou WSL est conseillé pour le mode réel : en cas de délai dépassé, l'arrêt porte sur tout le groupe de processus ; sous Windows natif, seul le processus lancé est arrêté.

Pour des dépôts privés, renseigne `GITHUB_TOKEN` avec un jeton GitHub à portée limitée aux deux dépôts et avec les seules permissions de lecture nécessaires : **Contents**, **Issues** et **Pull requests**. Ce jeton GitHub est distinct des connexions IA. Ne le publie pas ; `.env` et `instance/` sont exclus de Git.

Vérifie la configuration :

```bash
flask --app app doctor
```

Cette commande vérifie la configuration locale, sans appel IA et sans lire les identifiants des outils. Elle ne vérifie pas les droits GitHub, l'authentification IA ou les quotas.

## Lancer une vraie discussion

Deux méthodes sont disponibles. Pour un premier essai, la commande directe est la plus simple :

```bash
flask --app app daily --mode live
```

Pour utiliser le bouton de l'interface, démarre le serveur avec `python app.py` et, dans un second terminal avec le même environnement virtuel actif, lance :

```bash
flask --app app worker
```

Le bouton du mode réel place le cycle dans la file d'attente. Le worker traite les appels en dehors du serveur web. Avec `worker --once`, il traite au maximum un cycle en attente puis quitte. Recharge la page du cycle pour consulter la progression.

Un cycle est unique **par date Europe/Paris et par mode**. La démo et le réel peuvent donc être lancés le même jour ; répéter une commande ou un clic ne déclenche pas un second cycle du même mode. Un échec n'est pas relancé automatiquement : les interventions déjà tentées peuvent avoir consommé du quota.

Un cycle réel resté en attente depuis une date précédente n'est pas traité. Si minuit passe pendant un cycle réel, aucun nouveau tour n'est lancé pour cette ancienne journée.

Si le processus a été interrompu et qu'un cycle reste indiqué en cours, arrête d'abord le worker concerné, puis clôture le cycle avec son numéro :

```bash
flask --app app mark-interrupted 1
```

Cette opération marque l'interruption sans rejouer la discussion. Elle n'autorise pas un nouveau cycle du même mode à la même date.

## Ce que les IA voient

Pour chaque dépôt, le connecteur récupère au maximum vingt entrées issues ouvertes, puis exclut les PR présentes dans cette liste, quatre PR ouvertes et cinq commits de la branche configurée. Les textes sont abrégés et certaines entrées peuvent être omises pour respecter la taille du contexte.

**Aucun fichier source, diff, commentaire, résultat CI ou review n'est lu à ce stade.** Une anomalie citée dans une issue reste donc une piste à examiner, et les décisions sont des propositions à valider. À partir de trois PR ouvertes observées, le contexte demande de choisir une revue ou de ne rien faire ; cette version n'exécute de toute façon aucun développement.

Les discussions partagent uniquement ce contexte borné, les interventions du sujet en cours et une mémoire de trois décisions. Les modes démo et réel ont des mémoires distinctes.

## Limites de consommation et accès

Les douze messages correspondent au maximum à douze lancements de CLI. Ils limitent les interventions visibles, **pas les requêtes internes des CLI, les tokens, le raisonnement des modèles ou leur consommation d'abonnement**. Les appels peuvent rencontrer un quota, un modèle indisponible ou une déconnexion. Aucun coût en euros n'est calculé à partir d'un abonnement ; les compteurs de tokens ne sont renseignés que lorsqu'ils sont fournis par le connecteur.

`CLI_TIMEOUT` limite l'attente d'une intervention ; sa valeur doit être comprise entre 10 et 600 secondes, avec 180 secondes par défaut. Une erreur, une réponse structurée invalide ou un délai dépassé arrête le sujet concerné. L'autre sujet peut encore être traité. Il n'y a pas de nouvelle tentative automatique.

Cette V0.1.1 est destinée à **un usage personnel local**. Le serveur écoute sur `127.0.0.1` et refuse les adresses clientes non locales. Il n'y a pas de compte utilisateur. Ne l'expose pas sur Internet ou derrière un reverse proxy sans ajouter une authentification adaptée : un proxy local pourrait rendre les requêtes distantes apparemment locales.

## Données et vérification

La base et son journal se trouvent dans `instance/duopilot.sqlite3`. Elle contient les contextes GitHub et échanges, qui peuvent être confidentiels. Le bouton d'export fournit le journal JSON d'un cycle. Pour copier la base, arrête les processus qui l'utilisent et conserve le dossier `instance/` complet.

Les tests locaux s'exécutent ainsi :

```bash
python -m pip install -r requirements-dev.txt
python -m pytest
```

Les tests utilisent des doublures locales et ne démontrent pas la compatibilité de tes connexions avec les versions des CLI installées.

## Suite du projet

La prochaine étape sera un exécuteur séparé : lecture des sources, branche de travail isolée depuis `dev`, développement, tests, revue par l'autre IA et PR vers `dev`. Chaque dépôt devra avoir ses protections et permissions interdisant la fusion aux agents. Les limites, les droits et l'activation d'une nouvelle version du coordinateur resteront sous contrôle humain.

Pour l'auto-amélioration, la version active restera celle validée par Cyril. Les IA proposeront leurs changements dans le dépôt DUO PILOT ; elles ne pourront pas remplacer à chaud leur propre processus.

Voir [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) pour l'organisation du code et les invariants du prototype.

## Documentation officielle

Références consultées lors de la préparation de cette version :

- [Installation de Codex CLI](https://developers.openai.com/codex/cli) et [authentification](https://developers.openai.com/codex/auth).
- [Exécution non interactive de Codex](https://developers.openai.com/codex/non-interactive-mode).
- [Installation et connexion de Claude Code](https://code.claude.com/docs/en/quickstart).
- [Exécution non interactive de Claude Code](https://code.claude.com/docs/en/headless) et [conditions d'utilisation et conformité](https://code.claude.com/docs/en/legal-and-compliance).
- [API GitHub : issues](https://docs.github.com/en/rest/issues/issues).

L'accès dépend de l'abonnement, des quotas et de la version installée. Les connexions restent celles des binaires officiels ; cette application ne reproduit pas les API privées de ChatGPT ou de Claude.
