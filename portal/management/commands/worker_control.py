from django.core.management.base import BaseCommand, CommandError
from portal.models import Run, WorkerState

class Command(BaseCommand):
    help = 'Pause/resume scheduling, or report whether running tasks have drained.'
    def add_arguments(self, parser):
        parser.add_argument('action', choices=['pause', 'resume', 'drained'])
    def handle(self, *args, **options):
        if options['action'] == 'drained':
            if Run.objects.filter(status='running').exists():
                raise CommandError('Worker is still processing a task')
            self.stdout.write('drained')
            return
        state, _ = WorkerState.objects.get_or_create(key='main')
        state.paused = options['action'] == 'pause'
        state.save(update_fields=['paused'])
        self.stdout.write(options['action'])
