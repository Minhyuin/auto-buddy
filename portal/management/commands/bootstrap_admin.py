from getpass import getpass
from django.contrib.auth import get_user_model, password_validation
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

class Command(BaseCommand):
    help = 'Interactively create/reset the single administrator; no credentials in arguments or files.'
    def handle(self, *args, **options):
        username = input('管理员用户名: ').strip()
        if not username or len(username) > 150:
            raise CommandError('用户名无效')
        password = getpass('管理员密码（不回显）: ')
        if password != getpass('再次输入密码: '):
            raise CommandError('两次密码不一致')
        User = get_user_model()
        user = User(username=username)
        try:
            password_validation.validate_password(password, user)
        except ValidationError as exc:
            raise CommandError('；'.join(exc.messages)) from None
        with transaction.atomic():
            users = list(User.objects.select_for_update().all())
            if any(item.username != username for item in users):
                raise CommandError('系统已存在其他管理员，请先人工核查；不允许创建第二个用户')
            if users:
                user = users[0]
            user.is_superuser = user.is_staff = user.is_active = True
            user.set_password(password)
            user.save()
        self.stdout.write(self.style.SUCCESS('管理员已设置（未输出账号或密码）'))
