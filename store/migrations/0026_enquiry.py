from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("store", "0025_homeslide"),
    ]

    operations = [
        migrations.CreateModel(
            name="Enquiry",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                (
                    "reference",
                    models.CharField(editable=False, max_length=32, unique=True),
                ),
                ("name", models.CharField(max_length=120)),
                ("email", models.EmailField(max_length=254)),
                ("phone", models.CharField(blank=True, max_length=50)),
                ("message", models.TextField()),
                ("source_url", models.URLField(blank=True, max_length=500)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "email_status",
                    models.CharField(
                        choices=[
                            ("pending", "Pending"),
                            ("sent", "Sent"),
                            ("failed", "Failed"),
                        ],
                        default="pending",
                        max_length=16,
                    ),
                ),
                ("email_sent_at", models.DateTimeField(blank=True, null=True)),
                ("email_error", models.CharField(blank=True, max_length=120)),
            ],
            options={
                "verbose_name_plural": "Enquiries",
                "ordering": ["-created_at"],
            },
        ),
        migrations.CreateModel(
            name="ContactRateLimitBucket",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                (
                    "identity_hash",
                    models.CharField(
                        db_index=True,
                        max_length=64,
                        unique=True,
                    ),
                ),
                ("window_started_at", models.DateTimeField()),
                ("request_count", models.PositiveIntegerField(default=0)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
        ),
    ]
