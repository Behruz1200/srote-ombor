"""Create a superuser if one with the given username doesn't exist.

Idempotent: safe to run on every deploy. Reads credentials from env:
  DJANGO_SUPERUSER_USERNAME
  DJANGO_SUPERUSER_PASSWORD
  DJANGO_SUPERUSER_EMAIL       (optional)

If USERNAME or PASSWORD are unset, the command is a no-op (you may
not want a superuser auto-created in every environment).
"""
import os
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Create a superuser from env vars if it doesn't already exist."

    def handle(self, *args, **opts):
        from inventory.models import User

        username = os.environ.get('DJANGO_SUPERUSER_USERNAME')
        password = os.environ.get('DJANGO_SUPERUSER_PASSWORD')
        email = os.environ.get('DJANGO_SUPERUSER_EMAIL', '') or ''

        if not username or not password:
            self.stdout.write(
                'ensure_superuser: DJANGO_SUPERUSER_USERNAME / '
                'DJANGO_SUPERUSER_PASSWORD not set — skipping.'
            )
            return

        if User.objects.filter(username=username).exists():
            self.stdout.write(f'ensure_superuser: {username!r} already exists — skipping.')
            return

        User.objects.create_superuser(
            username=username,
            password=password,
            email=email,
            role=User.Role.ADMIN,
        )
        self.stdout.write(self.style.SUCCESS(
            f'ensure_superuser: created superuser {username!r}.'))
