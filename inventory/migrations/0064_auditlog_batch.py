from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('inventory', '0063_background_job'),
    ]

    operations = [
        migrations.AddField(
            model_name='auditlog',
            name='batch_id',
            field=models.CharField(blank=True, db_index=True, max_length=32),
        ),
        migrations.AddField(
            model_name='auditlog',
            name='batch_count',
            field=models.PositiveIntegerField(
                default=0,
                help_text='0 = oddiy qator; >0 = partiya boshi (nechta qator)'),
        ),
    ]
