# DUO PILOT 0.4.1 — installation sur ton VPS

Prévu pour un VPS dédié Ubuntu 26.04 LTS x86_64 ; le script accepte aussi Ubuntu 24.04. Ton serveur dispose d’environ 8 Go de RAM et 70 Go libres, ce qui convient pour commencer avec un worker et des tests limités à 1 Go par conteneur. Les besoins réels dépendront des tests de CS-PILOT.

**Flask, Codex et Claude Code sont installés directement sur le VPS.** Leurs connexions restent dans le compte Linux `duopilot`. Docker sert uniquement aux tests du code proposé. Il n’y a pas de connexion d’abonnement à effectuer dans un conteneur.

L’interface écoute sur `127.0.0.1:5055`. Tu y accèdes par SSH depuis ton PC ; il n’y a aucun port web public à ouvrir. Les services continuent à tourner lorsque tu fermes le PC. Le premier tour réel reste désactivé jusqu’à ta connexion aux CLI et à son activation.

## 1. Transférer et installer

Les exemples utilisent le compte SSH `ubuntu`. Adapte-le si ton compte est différent.

Sur le **PC, PowerShell**, après avoir téléchargé cette archive dans Téléchargements :

```powershell
scp "$env:USERPROFILE\Downloads\DUO_PILOT_VPS_v0.4.1.zip" ubuntu@164.132.79.216:~/
```

Dans ta **session SSH sur le VPS** :

```bash
sudo apt-get update
sudo apt-get install -y unzip
unzip DUO_PILOT_VPS_v0.4.1.zip -d duo-install-0.4.1
sudo bash ~/duo-install-0.4.1/duo_pilot/deploy/install-ubuntu.sh
```

Le script installe Python, Git, Gunicorn et Docker CE, crée le compte `duopilot`, puis installe les CLI officiels. Il prépare l’interface, le worker et la sauvegarde quotidienne. Il ne change pas SSH, le pare-feu ou les autres comptes ; il ne redémarre pas le VPS. Il ne supprime pas automatiquement des paquets Docker préexistants.

Chemins : code `/opt/duopilot/app`, environnement Python `/opt/duopilot/venv`, données `/opt/duopilot/app/instance`, connexions `/home/duopilot`, sauvegardes `/var/backups/duopilot`.

Le compte `duopilot` appartient au groupe Docker : ce droit donne un accès puissant à la machine, comparable à un accès administrateur. C’est pourquoi cette installation vise ton VPS dédié. Le programme limite les commandes du moteur et les conteneurs de tests ; l’appartenance au groupe Docker n’est pas une isolation de ce compte vis-à-vis du serveur.

## 2. Connecter tes abonnements

Toujours **sur le VPS**, utilise ces commandes pour te connecter sous le même compte que les services :

```bash
sudo duopilotctl login-codex
sudo duopilotctl login-claude
```

Pour Codex, ouvre sur ton PC l’URL indiquée et saisis le code temporaire. Si l’authentification par code d’appareil est désactivée, active-la dans les paramètres de sécurité de ChatGPT. Pour Claude, ouvre le lien affiché, choisis ton compte avec abonnement Claude et termine la procédure indiquée par le CLI. Ne choisis pas la facturation API/Console.

Il faut une nouvelle connexion sur le VPS ; celle de Windows n’y est pas automatiquement présente. Ne copie pas les fichiers d’authentification dans cette conversation. Les deux CLI conservent et renouvellent leurs connexions selon les règles de leur fournisseur ; une révocation ou expiration peut demander une reconnexion. Les quotas restent ceux de tes abonnements.

## 3. Renseigner GitHub

```bash
sudo duopilotctl config
sudo duopilotctl doctor
sudo duopilotctl lire-depot --topic cspilot
sudo duopilotctl lire-depot --topic self
```

La configuration demande les deux dépôts au format `propriétaire/dépôt`, leurs branches (`dev` pour CS-PILOT, `main` pour DUO PILOT par défaut) et ton jeton GitHub à saisie masquée. Pour un jeton fine-grained limité à ces deux dépôts : Contents et Pull requests en écriture, Issues en lecture, Metadata en lecture. Le configurateur utilise ce jeton pour la lecture et l’écriture ; l’application permet aussi deux jetons séparés dans `.env`.

Les dépôts et branches doivent déjà exister. Les permissions d’organisation peuvent nécessiter une validation côté GitHub. Le dépôt DUO PILOT doit contenir la version que les agents doivent améliorer : le moteur travaille depuis GitHub, pas directement dans `/opt/duopilot/app`.

Une saisie initiale vide du jeton permet seulement les lectures publiques compatibles. Un jeton est nécessaire pour les dépôts privés et pour créer les branches et PR.

Les commandes de lecture n’appellent aucun modèle. `doctor` examine la configuration locale ; il ne garantit ni les droits distants ni les quotas. L’application ne possède pas de commande de fusion. Pour rendre cette interdiction indépendante du programme, conserve les protections et permissions GitHub décrites dans le README principal.

Une reconfiguration ultérieure est refusée pendant un travail en file ou en cours, ou tant que le développement est activé. Désactive celui-ci dans Configuration, termine ou mets en pause les missions, puis relance `config`. Le helper arrête temporairement l’interface et le worker au repos, les relance ensuite et conserve les connexions.

## 4. Ouvrir l’application sur le PC

Dans une **nouvelle fenêtre PowerShell**, laisse cette commande tourner :

```powershell
ssh -N -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 -L 127.0.0.1:5056:127.0.0.1:5055 ubuntu@164.132.79.216
```

Ouvre **http://127.0.0.1:5056** sur ton PC. Le port 5056 évite un conflit avec l’installation Windows utilisant 5055. Une fenêtre SSH silencieuse est normale. Fermer ce tunnel ferme seulement ton accès à l’interface ; le VPS continue à travailler.

Commence par une démo, puis un petit tour réel depuis le tableau de bord. L’historique, le Kanban, le choix des modèles et les limites de durée et de tentatives sont dans l’application. Le bouton **Un tour supplémentaire** permet de relancer une discussion lorsqu’un nouveau bug arrive.

L’interface n’a pas de comptes utilisateurs : n’ajoute pas de proxy public devant elle et ne remplace pas l’adresse d’écoute par `0.0.0.0`.

## 5. Préparer les tests et les PR

Pour DUO PILOT, sur le VPS :

```bash
sudo duopilotctl preparer-tests --topic self
```

Pour CS-PILOT, il faut les fichiers de dépendances correspondant à ton application. Copie sur le VPS le dossier contenant `requirements.txt`, et tous les fichiers inclus par `-r` ou `-c`, sans `.env` ni données métier. Par exemple, après transfert dans `/home/ubuntu/cspilot-dependencies` :

```bash
sudo install -d -o duopilot -g duopilot -m 700 /home/duopilot/dependencies
sudo cp -R /home/ubuntu/cspilot-dependencies /home/duopilot/dependencies/cspilot
sudo chown -R duopilot:duopilot /home/duopilot/dependencies/cspilot
sudo duopilotctl preparer-tests --topic cspilot --requirements /home/duopilot/dependencies/cspilot/requirements.txt
```

Si `requirements-dev.txt` contient les dépendances des tests, utilise-le à la place. Lis ces fichiers avant la construction : l’installation des paquets peut exécuter leur code dans l’image. Le préparateur construit une image Python 3.12 ; les tests réels se déroulent ensuite sans réseau. Un projet qui requiert des paquets système, une autre version Python, une base externe ou des dépendances privées demande une image adaptée et une commande de test appropriée. Voir le README principal. L’installateur ne peut pas deviner l’environnement de CS-PILOT à partir du nom du dépôt.

Dans **Configuration**, vérifie les noms d’images et les commandes, puis active **le développement, les tests et la publication de PR**. Les anciennes missions acceptées et éligibles peuvent alors être prises en charge : examine ton Kanban avant l’activation. Le développeur et le contrôleur disposent des limites que tu règles ; aucune PR n’est fusionnée par ce programme.

## 6. Activer le rendez-vous quotidien

```bash
sudo duopilotctl activer-quotidien
sudo duopilotctl status
```

Le rendez-vous est à **08:00, heure de Paris**, avec gestion du changement d’heure. Il ajoute le cycle à la file ; le worker unique le traite après son travail en cours. Un second déclenchement le même jour ne crée pas de doublon quotidien. Les tours manuels restent distincts.

Il n’y a pas de rattrapage automatique des journées manquées lorsque le VPS est arrêté, ni de déclenchement immédiat à l’activation après 08:00. Utilise un tour supplémentaire si tu veux démarrer tout de suite.

Pour changer l’heure :

```bash
sudo systemctl edit duopilot-daily.timer
```

Insère, par exemple pour 09:30 :

```ini
[Timer]
OnCalendar=
OnCalendar=*-*-* 09:30:00 Europe/Paris
```

Puis `sudo systemctl daemon-reload` et `sudo systemctl restart duopilot-daily.timer`.

Pour suspendre les prochains rendez-vous : `sudo duopilotctl pause-quotidien`. Cette commande laisse les travaux déjà lancés continuer. Pour les interrompre, mets les missions en pause depuis l’interface.

## 7. Sauvegardes, historique et migration du PC

Chaque nuit vers 03:00, heure de Paris, une sauvegarde cohérente de SQLite et une copie de `.env` sont réunies dans une archive privée. Les sept dernières archives sont conservées. Tu peux en lancer une avec `sudo duopilotctl sauvegarder` et consulter leur liste avec `sudo ls -lh /var/backups/duopilot`.

La base contient les conversations, cartes, réglages et journaux. **Ces archives ne contiennent ni les connexions des CLI, ni les images Docker, ni les copies Git des missions.** Elles ne permettent donc pas, à elles seules, de reprendre une mission avec ses fichiers. La capture de la base et celle de `.env` sont successives. Pour une sauvegarde complète avant une mise à jour, attends le repos, arrête les services, puis conserve `/opt/duopilot/app` et `/home/duopilot` dans un emplacement privé. Ne déplace pas cette sauvegarde complète dans un dépôt Git.

Les sauvegardes automatiques restent sur ce VPS : télécharge régulièrement une copie ou utilise une sauvegarde OVH pour couvrir aussi sa perte. Une archive contient le jeton GitHub de `.env` ; conserve-la comme un fichier confidentiel.

Pour **garder l’historique du PC** au lieu de commencer avec une base vide :

1. Termine les travaux en cours, puis arrête le serveur ET le worker sur le PC. Sauvegarde ton dossier local `duo_pilot` complet. Ne relance pas son rendez-vous quotidien une fois le VPS activé.
2. Transfère le fichier `instance/duopilot.sqlite3` fermé vers `/home/ubuntu/duopilot-pc.sqlite3` sur le VPS. Si SQLite a encore un fichier `-wal` non vide après fermeture de tous les processus, ne copie pas seulement le fichier principal : effectue d’abord une sauvegarde SQLite avec son API de sauvegarde.
3. Sur le VPS, suspends le tour quotidien, désactive le développement dans l’interface et attends le repos. Sauvegarde, puis arrête les services :

```bash
sudo duopilotctl pause-quotidien
sudo duopilotctl sauvegarder
sudo systemctl stop duopilot-web.service duopilot-worker.service duopilot-backup.timer duopilot-backup.service
sudo mv /opt/duopilot/app/instance /opt/duopilot/app/instance-before-pc-import
sudo install -d -o duopilot -g duopilot -m 700 /opt/duopilot/app/instance
sudo install -o duopilot -g duopilot -m 600 /home/ubuntu/duopilot-pc.sqlite3 /opt/duopilot/app/instance/duopilot.sqlite3
sudo systemctl start duopilot-web.service duopilot-backup.timer
```

4. L’interface migre la base au démarrage. Vérifie l’historique et désactive le développement si la base importée l’avait activé. Les chemins et copies de travail Windows ne sont pas transférés par cette procédure : aucune mission ne doit être en cours ou en file dans la base importée. Ne relance pas aveuglément des missions Windows inachevées.
5. Démarre `sudo systemctl start duopilot-worker.service`, reconstruis les images si nécessaire et réactive le rendez-vous quand tout est prêt. Garde le dossier `instance-before-pc-import` jusqu’à validation ; ne rejoue pas les commandes de migration si ce dossier existe déjà.

Le `.env` Windows n’est pas recopié : ses chemins et ses connexions diffèrent de ceux du VPS. Les exports JSON consultables dans l’application restent une seconde façon d’archiver les conversations.

## 8. Exploitation et mises à jour

```bash
sudo duopilotctl status
sudo duopilotctl logs
sudo journalctl -u duopilot-backup.service -n 30 --no-pager
df -h /
```

Le script d’installation est destiné à une installation neuve. Une nouvelle invocation après réussite ne remplace pas le code. Avant une mise à jour : suspends le timer, désactive le développement, attends le repos, sauvegarde complètement, puis arrête l’interface, le worker et le timer de sauvegarde. Remplace seulement le code de l’application, conserve `.env`, `instance` et `/home/duopilot`, réinstalle `requirements-vps.txt` dans le même environnement, puis redémarre les services. Une PR sur DUO PILOT n’actualise jamais automatiquement la version qui tourne.

Les services redémarrent avec le VPS et après une défaillance du processus. **Une mission interrompue n’est pas automatiquement rejouée.** Après arrêt ou redémarrage pendant une mission, examine le journal et utilise la procédure « Marquer l’interruption » puis la reprise explicite du README principal. Un conteneur orphelin après un arrêt brutal peut nécessiter un nettoyage manuel après vérification ; aucun nettoyage destructeur global n’est programmé.

Les copies de travail Git et les images consomment progressivement le disque. Les conversations ne sont pas purgées automatiquement. Surveille l’espace ; ne supprime pas un dossier de mission encore suivi.

Si l’installation échoue après avoir démarré les services, une relance du script les détecte et refuse de remplacer le code en fonctionnement. Consulte `sudo duopilotctl logs` et `sudo journalctl -u duopilot-backup.service -n 30 --no-pager`. Pour reprendre une **première installation où aucun travail n’a été lancé**, arrête les services avant de relancer l’installateur :

```bash
sudo systemctl stop duopilot-web.service duopilot-worker.service duopilot-daily.timer duopilot-daily.service duopilot-backup.timer duopilot-backup.service
sudo bash ~/duo-install-0.4.1/duo_pilot/deploy/install-ubuntu.sh
```

Si des travaux réels ont déjà commencé, utilise la procédure de pause et de sauvegarde avant toute intervention.

Si une première copie du kit s’arrête pendant l’installation de Codex avec `find: Failed to restore initial working directory: /home/ubuntu: Permission denied`, le processus a hérité du dossier privé de ton compte SSH. Le kit corrigé se place dans le dossier application avant de changer d’utilisateur. Avec l’ancienne copie déjà extraite, reprends simplement depuis un dossier accessible :

```bash
cd /tmp
sudo bash /home/ubuntu/duo-install-0.4.1/duo_pilot/deploy/install-ubuntu.sh
```

Les fichiers de configuration déjà créés sont conservés. Il n’est pas nécessaire de modifier les permissions de `/home/ubuntu`.

## Vérification et références

Le kit a été contrôlé localement sous Linux : tests de l’application, sauvegarde SQLite réelle, permissions et mise en file. Les services et le script sont vérifiés statiquement. **L’installation complète Ubuntu 26.04, les connexions à tes comptes et les conteneurs réels restent à vérifier sur ton VPS.** Voir `docs/VERIFICATION.md`.

- Docker et Ubuntu 26.04 : https://docs.docker.com/engine/install/ubuntu/
- Installation Codex : https://developers.openai.com/codex/cli
- Authentification Codex : https://developers.openai.com/codex/auth
- Installation Claude Code : https://code.claude.com/docs/en/setup
- Commandes de connexion Claude : https://code.claude.com/docs/en/cli-reference
- Flask et Gunicorn : https://flask.palletsprojects.com/en/stable/deploying/gunicorn/
