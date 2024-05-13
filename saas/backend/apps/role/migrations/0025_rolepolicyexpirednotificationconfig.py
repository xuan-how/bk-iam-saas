# Generated by Django 3.2.25 on 2024-04-07 08:02

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('role', '0024_auto_20240329_1055'),
    ]

    operations = [
        migrations.CreateModel(
            name='RolePolicyExpiredNotificationConfig',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('creator', models.CharField(max_length=64, verbose_name='创建者')),
                ('updater', models.CharField(max_length=64, verbose_name='更新者')),
                ('created_time', models.DateTimeField(auto_now_add=True)),
                ('updated_time', models.DateTimeField(auto_now=True)),
                ('role_id', models.IntegerField(unique=True, verbose_name='角色ID')),
                ('_config', models.TextField(db_column='config', default='{}', verbose_name='配置')),
            ],
            options={
                'verbose_name': '角色策略过期通知配置',
                'verbose_name_plural': '角色策略过期通知配置',
            },
        ),
    ]
