# Vérification de la version 0.1.1

Vérification de cette mise à jour le 26 septembre 2026, sous Python 3.12 et Linux : **28 tests automatisés réussis**, sans appel à un modèle ni à GitHub.

Les quatre nouveaux tests couvrent :

- Deux ouvreurs différents pour les deux sujets, échange des rôles le lendemain, trois messages par IA et par sujet, rôle du développeur identique à celui de l'ouvreur et contrôle attribué à l'autre IA.
- Migration d'une base au format 0.1 : tous les anciens champs sont comparés avant/après ; messages, contextes, décisions, votes et réservations quotidiennes sont conservés. Les rôles historiques correspondent aux messages réels, y compris quand la même IA avait ouvert les deux sujets.
- Démarrage simultané de deux processus sur une ancienne base, sans double migration ni perte de données.
- Consultation d'un historique dépassant 30 cycles, accès au plus ancien et export intact de sa conversation.

L'affichage et les exports ont été vérifiés avec le client de test Flask. Les nouvelles règles sont testées avec le fournisseur de démonstration ; les connexions aux abonnements ne sont pas rejouées ici. Les connecteurs et dépendances n'ont pas été modifiés. La phase d'exécution des tâches reste à construire.

## Vérification initiale de la version 0.1

Vérification effectuée le 26 septembre 2026, avec Python 3.12 sous Linux.

- 24 tests automatisés réussis (`python -m pytest -q`).
- Compilation de tous les modules Python réussie.
- Parcours Flask vérifié : accueil, configuration, démo à douze messages, détail des décisions, export JSON, page introuvable.
- Limites vérifiées : deux déclenchements simultanés ne doublent pas les appels, cycle unique par jour et par mode, interruption sans rejeu, ancien cycle réel non rattrapé.
- Accord vérifié : trois interventions par agent et par sujet ; un vote visant un autre identifiant de plan est refusé ; aucune action est une décision possible.
- Connecteurs vérifiés avec doublures : arguments des CLI, environnement sans clés, lecture GitHub seule, réponses et délais bornés, rejet d'un JSON invalide, erreurs sans contenu sensible.
- Aucun appel à un modèle réel, aucun abonnement connecté, aucun dépôt utilisateur interrogé ou modifié pendant ces tests.

Les CLI officiels n'étaient pas installés ni authentifiés dans l'environnement de vérification. La compatibilité de leurs versions et de tes connexions reste à vérifier sur ta machine. Windows n'a pas été testé ; `tzdata` est inclus pour le fuseau Europe/Paris.

La vérification de l'interface a porté sur les réponses et le rendu HTML des modèles Flask. Aucune capture dans un navigateur n'a pu être réalisée, le téléchargement du moteur de navigateur ayant échoué.

Cette version livre le coordinateur et la délibération. Elle ne réalise pas encore l'action choisie, ne crée pas de PR et n'active pas d'auto-modification.
