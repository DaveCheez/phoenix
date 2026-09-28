# Phoenix Vanz Backend Instructions

## Repository

This is the Django backend for Phoenix Vanz.

The Nuxt frontend is a separate repository. Do not assume frontend files are
available in this workspace.

## Safe workflow

- Inspect existing models, serializers, views, URLs and tests before editing.
- Work only on the current feature branch.
- Make small, reviewable changes.
- Do not alter unrelated models or migrations.
- Do not modify GitHub workflows unless explicitly requested.
- Do not commit, push, merge, deploy or run production backfills without
  explicit approval.

## Required checks

Run:

- `python manage.py check`
- `python manage.py makemigrations --check --dry-run`
- focused tests for the changed feature
- `python manage.py test`
- `git diff --check`

If a migration is intentionally required, explain it before creating it.

## Data and storage safety

- Never delete old DigitalOcean Spaces files automatically.
- Backfills must support dry-run and selected-record testing.
- Keep production credentials out of source control.
- Do not use the production database for local development unless explicitly
  authorised.
- New image processing must not repeatedly recompress committed files.

## Current project decisions

- The full Google Reviews sync/API replacement is deferred.
- The contact enquiry feature should save enquiries and email Phoenix Vanz.
- Email failure must not silently discard the enquiry.
- Checkout is intended to charge a one-third deposit, with balance due on
  completion.

  