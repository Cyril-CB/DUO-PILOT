import time
import click
from .db import get_db
from .orchestrator import enqueue, run_cycle, mark_interrupted

def register_commands(app):
    @app.cli.command('daily')
    @click.option('--mode', type=click.Choice(['demo', 'live']), default='demo', show_default=True)
    def daily(mode):
        """Create and process today's cycle once (Europe/Paris)."""
        if mode == 'live':
            from .web import readiness
            errors = readiness()
            if errors:
                raise click.ClickException('\n'.join(errors))
        cycle_id, created = enqueue(mode)
        done = run_cycle(cycle_id)
        status = get_db().execute('SELECT status FROM cycles WHERE id=?', (cycle_id,)).fetchone()['status']
        click.echo(f'Cycle {cycle_id} : {status}. ' + ('Traité.' if done else 'Aucun nouvel appel.'))
        if status == 'failed':
            raise click.ClickException('Cycle en échec. Consulter le journal dans l’interface.')

    @app.cli.command('worker')
    @click.option('--once', is_flag=True, help='Traiter une entrée puis quitter.')
    def worker(once):
        """Process queued cycles outside the web server."""
        click.echo('Worker démarré. Ctrl+C pour arrêter.')
        try:
            while True:
                row = get_db().execute("SELECT id FROM cycles WHERE status='queued' ORDER BY id LIMIT 1").fetchone()
                if row:
                    run_cycle(row['id'])
                    click.echo(f"Cycle {row['id']} terminé ; consulter son statut dans le journal.")
                if once:
                    break
                time.sleep(2)
        except KeyboardInterrupt:
            click.echo('Worker arrêté. Un cycle interrompu n’est pas relancé automatiquement.')

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
