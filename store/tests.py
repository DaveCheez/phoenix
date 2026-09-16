from datetime import timedelta

from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APITestCase

from .models import HomeSlide


GIF_1X1 = (
    b"GIF89a\x01\x00\x01\x00\x80\x00\x00\x00\x00\x00"
    b"\xff\xff\xff!\xf9\x04\x01\x00\x00\x00\x00,\x00\x00"
    b"\x00\x00\x01\x00\x01\x00\x00\x02\x02D\x01\x00;"
)


class HomeSlideAPITests(APITestCase):
    def make_image(self, name):
        return SimpleUploadedFile(name, GIF_1X1, content_type="image/gif")

    def test_only_current_active_slides_are_returned_in_order(self):
        now = timezone.now()
        HomeSlide.objects.create(
            title="Second",
            image=self.make_image("second.gif"),
            display_order=20,
        )
        HomeSlide.objects.create(
            title="First",
            image=self.make_image("first.gif"),
            display_order=10,
        )
        HomeSlide.objects.create(
            title="Inactive",
            image=self.make_image("inactive.gif"),
            is_active=False,
        )
        HomeSlide.objects.create(
            title="Future",
            image=self.make_image("future.gif"),
            starts_at=now + timedelta(days=1),
        )
        HomeSlide.objects.create(
            title="Expired",
            image=self.make_image("expired.gif"),
            ends_at=now - timedelta(days=1),
        )

        response = self.client.get(reverse("home-slide-list-api"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            [slide["title"] for slide in response.data],
            ["First", "Second"],
        )
