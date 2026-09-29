from django.conf import settings
from django.core.mail import EmailMessage
from django.utils import timezone


class EmailDeliveryError(RuntimeError):
    pass


def build_enquiry_email_subject(enquiry):
    return f"Phoenix Vanz enquiry {enquiry.reference} from {enquiry.name}"


def build_enquiry_email_body(enquiry):
    submitted = timezone.localtime(enquiry.created_at).strftime(
        "%d %b %Y, %H:%M %Z"
    )
    phone = enquiry.phone.strip() if enquiry.phone else "(not provided)"
    source_url = enquiry.source_url.strip() if enquiry.source_url else "(not provided)"

    return (
        "Phoenix Vanz website enquiry\n\n"
        f"Reference: {enquiry.reference}\n"
        f"Submitted: {submitted}\n"
        f"Name: {enquiry.name}\n"
        f"Email: {enquiry.email}\n"
        f"Phone: {phone}\n"
        f"Source URL: {source_url}\n\n"
        "Message:\n"
        f"{enquiry.message}\n"
    )


def send_enquiry_email(enquiry):
    recipient = (getattr(settings, "CONTACT_RECIPIENT_EMAIL", "") or "").strip()
    if not recipient:
        raise RuntimeError("CONTACT_RECIPIENT_EMAIL is not configured")

    email = EmailMessage(
        subject=build_enquiry_email_subject(enquiry),
        body=build_enquiry_email_body(enquiry),
        from_email=settings.DEFAULT_FROM_EMAIL,
        to=[recipient],
        reply_to=[enquiry.email],
    )
    sent_count = email.send()
    if sent_count != 1:
        raise EmailDeliveryError("The email backend did not accept one message.")
