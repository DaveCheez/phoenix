from datetime import timedelta

from django.db import IntegrityError, transaction
from django.utils import timezone
from rest_framework.throttling import BaseThrottle

from .models import ContactRateLimitBucket


class ContactEnquiryThrottle(BaseThrottle):
    request_limit = 5
    window_duration = timedelta(hours=1)

    def allow_request(self, request, view):
        identity_hash = getattr(request, "contact_identity_hash", None)
        if not identity_hash:
            return False

        now = timezone.now()
        with transaction.atomic():
            try:
                bucket = ContactRateLimitBucket.objects.select_for_update().get(
                    identity_hash=identity_hash
                )
            except ContactRateLimitBucket.DoesNotExist:
                try:
                    # Use a savepoint so a concurrent unique-key collision does
                    # not leave the surrounding transaction unusable.
                    with transaction.atomic():
                        ContactRateLimitBucket.objects.create(
                            identity_hash=identity_hash,
                            window_started_at=now,
                            request_count=1,
                        )
                    return True
                except IntegrityError:
                    bucket = ContactRateLimitBucket.objects.select_for_update().get(
                        identity_hash=identity_hash
                    )

            if now >= bucket.window_started_at + self.window_duration:
                bucket.window_started_at = now
                bucket.request_count = 1
                bucket.save(
                    update_fields=[
                        "window_started_at",
                        "request_count",
                        "updated_at",
                    ]
                )
                return True

            if bucket.request_count >= self.request_limit:
                return False

            bucket.request_count += 1
            bucket.save(update_fields=["request_count", "updated_at"])
            return True
