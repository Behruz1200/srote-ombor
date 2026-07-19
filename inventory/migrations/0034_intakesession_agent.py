from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('inventory', '0033_cashpayout'),
    ]

    operations = [
        migrations.AddField(
            model_name='intakesession',
            name='agent_name',
            field=models.CharField(
                blank=True, max_length=120,
                help_text='Fakturani olib kelgan agent / ekspeditor'),
        ),
        migrations.AddField(
            model_name='intakesession',
            name='agent_phone',
            field=models.CharField(
                blank=True, max_length=40, help_text='Agent telefoni'),
        ),
    ]
