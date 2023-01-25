# Generated by Django 3.2.14 on 2022-12-09 16:09
from django.db import migrations
from django.db import models


class Migration(migrations.Migration):

    replaces = [('django_celery_results', '0140_delete_task'),]

    def __init__(self, name, app_label):
        super(Migration, self).__init__(name, 'django_celery_results')

    dependencies = [
        ('contentcuration', '0140_delete_task'),
        ('django_celery_results', '0011_taskresult_periodic_task_name'),
    ]

    operations = [
        migrations.AddField(
            model_name='taskresult',
            name='signature',
            field=models.CharField(max_length=32, null=True),
        ),
        migrations.AddIndex(
            model_name='taskresult',
            index=models.Index(condition=models.Q(('status__in', frozenset(['STARTED', 'REJECTED', 'RETRY', 'RECEIVED', 'PENDING']))), fields=['signature'], name='task_result_signature_idx'),
        ),
    ]
