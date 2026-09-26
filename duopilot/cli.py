import time
from pathlib import Path
import click
from .db import get_db
from .orchestrator import enqueue, run_cycle, mark_interrupted

def register_commands(app):
    @app.cli.command('inspect-repo')
    @click.option('--topic', type=click.Choice(['cspilot', 'self']), required=True)
    def inspect_repo(topic):
        """Check initial documentation reading without an AI call or daily cycle."""
        from .repository import build_discussion_context, RepositoryContext
        from .github import GitHubError
        try:
            reader = build_discussion_context(app.config, topic, 'live')
        except GitHubError as exc:
            raise click.ClickException(str(exc)) from None
        if not isinstance(reader, RepositoryContext):
            raise click.ClickException('Plafond de contexte insuffisant pour la lecture des fichiers.')
        click.echo(f'Dépôt : {reader.repo} · Commit : {reader.commit or "indisponible"}')
        click.echo(f'{len(reader.files)} fichiers texte accessibles dans cet instantané.')
        for item in reader.events:
            lines = f" · lignes {item['start_line']}–{item['end_line']}" if item['status'] == 'read' else ''
            click.echo(f"- {item['path']} : {item['status']}{lines}")
        click.echo(reader.notice)
        click.echo('Aucun appel IA, aucun cycle créé. Les lectures ci-dessus ne sont pas un audit du dépôt.')
        if not reader.available:
            raise click.ClickException('Lecture des fichiers indisponible.')

    @app.cli.command('daily')
    @click.option('--mode', type=click.Choice(['demo', 'live']), default='demo', show_default=True)
    @click.option('--enqueue-only', is_flag=True, help='Mettre en file uniquement ; laisser le worker traiter le cycle.')
    def daily(mode, enqueue_only):
        """Create and process today's cycle once (Europe/Paris)."""
        if mode == 'live':
            from .web import readiness
            errors = readiness()
            if errors:
                raise click.ClickException('\n'.join(errors))
        cycle_id, created = enqueue(mode)
        if enqueue_only:
            click.echo(f'Cycle {cycle_id} : ' + ('mis en file.' if created else 'déjà présent, aucun doublon.'))
            return
        done = run_cycle(cycle_id)
        status = get_db().execute('SELECT status FROM cycles WHERE id=?', (cycle_id,)).fetchone()['status']
        click.echo(f'Cycle {cycle_id} : {status}. ' + ('Traité.' if done else 'Aucun nouvel appel.'))
        if status == 'failed':
            raise click.ClickException('Cycle en échec. Consulter le journal dans l’interface.')

    @app.cli.command('worker')
    @click.option('--once', is_flag=True, help='Traiter une entrée puis quitter.')
    def worker(once):
        """Process discussions and execution jobs outside the web server."""
        from .executor import schedule_ready_tasks, run_execution, process_sync_request, queue_pr_syncs
        click.echo('Worker démarré. Ctrl+C pour arrêter.')
        next_sync = 0
        try:
            while True:
                if time.monotonic()>=next_sync:
                    queue_pr_syncs()
                    next_sync = time.monotonic()+300
                synced = process_sync_request()
                row = get_db().execute("SELECT id FROM cycles WHERE status='queued' ORDER BY id LIMIT 1").fetchone()
                if synced and once:
                    break
                if row:
                    run_cycle(row['id'])
                    click.echo(f"Cycle {row['id']} terminé ; consulter son statut dans le journal.")
                schedule_ready_tasks()
                job = get_db().execute("SELECT id FROM executions WHERE status='queued' ORDER BY id LIMIT 1").fetchone()
                if job and (not once or not row):
                    run_execution(job['id'])
                    click.echo(f"Exécution {job['id']} traitée ; consulter les résultats dans l’application.")
                if once:
                    break
                time.sleep(2)
        except KeyboardInterrupt:
            click.echo('Worker arrêté. Un cycle interrompu n’est pas relancé automatiquement.')

    @app.cli.command('execute')
    @click.argument('task_id',type=int)
    def execute(task_id):
        """Queue and run one accepted task (uses real accounts in live mode)."""
        from .executor import enqueue_task,run_execution,detail
        from .execution_io import ExecutionError
        try:
            job_id,_ = enqueue_task(task_id)
            run_execution(job_id)
            click.echo(f"Exécution {job_id} : {detail(job_id)['status']}")
        except ExecutionError as exc:
            raise click.ClickException(str(exc)) from None

    @app.cli.command('prepare-tests')
    @click.option('--topic',type=click.Choice(['cspilot','self']),required=True)
    @click.option('--requirements',type=click.Path(exists=True,dir_okay=False,path_type=Path))
    @click.option('--write-only',is_flag=True,help='Préparer le dossier Docker sans construire l’image.')
    def prepare_tests(topic,requirements,write_only):
        """Build a local Python test image from reviewed dependency files (network during build)."""
        from .test_images import prepare_image
        from .execution_io import ExecutionError
        try:
            folder,image = prepare_image(topic,requirements,build=not write_only)
            click.echo(f'Dossier de construction : {folder}')
            click.echo(f'Image : {image}. '+('Construction à lancer manuellement.' if write_only else 'Construction terminée.'))
        except ExecutionError as exc:
            raise click.ClickException(str(exc)) from None

    @app.cli.command('doctor')
    def doctor():
        """Check local configuration, without AI calls or credential access."""
        from .web import readiness
        errors = readiness()
        if errors:
            for error in errors:
                click.echo('- ' + error)
        else:
            click.echo('Outils et dépôts renseignés. Authentification à vérifier dans chaque CLI officiel.')
        click.echo('Aucun appel IA effectué. La démo fonctionne sans connexion.')

    @app.cli.command('mark-interrupted')
    @click.argument('cycle_id', type=int)
    def interrupted(cycle_id):
        """After stopping worker: close a stuck run without retrying messages."""
        click.echo('Cycle marqué interrompu.' if mark_interrupted(cycle_id) else 'Aucun cycle actif correspondant.')
