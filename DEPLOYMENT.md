# Phoenix Vanz Django API - DigitalOcean App Platform

This package is prepared for a new deployment alongside the current live app.
Do not destroy the existing app or change production DNS until the temporary
DigitalOcean URL has passed every check below.

## Critical security action before deployment

The original source archive contained a DigitalOcean Spaces access key and
secret in `cdn/conf.py`. Those credentials have been removed from this package.
Create a replacement key, update any currently running app that still uses the
old key, verify media uploads, and then revoke the exposed key **before** using
this repository. Enter only the replacement credentials in encrypted App
Platform environment variables.

Because the original repository may have Git history containing the old secret,
removing it from the current file is not enough. Treat the old key as exposed.

## What was changed

- Removed embedded Spaces credentials and moved all secrets to environment variables.
- Pinned Python 3.11.15 in `runtime.txt`.
- Updated Django to the final 4.1 patch release and pinned deploy dependencies.
- Added CORS and WhiteNoise configuration.
- Corrected hosts, CSRF origins, proxy/TLS handling and `Europe/London` time zone.
- Added `/health/` for App Platform health checks.
- Added a product-list API used by the Nuxt frontend.
- Fixed absolute Spaces image URLs and cart responses.
- Added a pre-deploy migration job in the App Platform specs.

## Files

- `.do/app.yaml`: first deployment using only the temporary `.ondigitalocean.app` URL.
- `.do/app-production.example.yaml`: reference showing the production domain; do not upload it over configured secrets.
- `.env.example`: local environment-variable reference.

## Region check before creating the app

The App Platform template currently says `region: lon`. Confirm the region of the
**existing managed PostgreSQL database** before launch. If the database is not in
London, change `region:` in both app-spec files to the database region. Keeping the
new backend in the same region avoids unnecessary latency and prevents reliance on
cross-region/private-network assumptions. Use the same region for the Nuxt app
unless there is a clear reason not to.

## Required DigitalOcean values

Replace every `REPLACE_*` value before launch:

1. `DJANGO_SECRET_KEY`: generate a fresh value, for example:
   `python -c "import secrets; print(secrets.token_urlsafe(64))"`
2. `DATABASE_URL`: the connection string for the **existing** PostgreSQL database.
3. `AWS_ACCESS_KEY_ID`: a newly created Spaces key.
4. `AWS_SECRET_ACCESS_KEY`: the matching new Spaces secret.

Confirm the existing media bucket/endpoint values before launch. The template
uses the values found in the original project:

- bucket: `phoenixvanz`
- endpoint/region: `fra1`
- public domain: `phoenixvanz.fra1.digitaloceanspaces.com`

Do not paste secrets into GitHub or commit an edited app spec containing real
secret values. Enter secrets through DigitalOcean's encrypted environment
variable controls.

## Safe deployment sequence

1. Back up the existing PostgreSQL database.
2. Confirm current product images are present in the Spaces bucket.
3. Replace the exposed Spaces key in the live app, verify uploads, then revoke the old key.
4. Push this prepared source to `DaveCheez/pheonix` (or change the repository in the spec).
5. In DigitalOcean App Platform, create a new app from `.do/app.yaml`.
6. Enter the required secrets in the control panel before launch.
7. Let the `migrate` pre-deploy job complete.
8. Test the temporary backend URL:
   - `/health/` returns `{"status":"ok"}`
   - `/admin/` loads
   - `/api/categories/?type=product` returns data
   - `/api/products/` returns data and image URLs
   - `/api/reviews/` responds
   - cart create/add/read/remove calls work
9. Point the new frontend's `NUXT_DJANGO_API_BASE` to this temporary API URL.
10. Only after frontend testing, add
    `api.phoenixvanz.com` in the DigitalOcean Networking tab.
11. Keep the old API app available until production smoke tests and database
    writes have been verified.

## Local check

Create a `.env` from `.env.example`, set `DEBUG_SETTING=True`, then run:

```bash
python -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
python manage.py check
python manage.py migrate
python manage.py runserver
```

## Follow-up maintenance

Django 4.1 is retained for the lowest-risk redeployment, but it is no longer a
supported release line. After the site is stable, plan and test an upgrade to a
currently supported Django LTS release in a separate branch.

## Backlog feature migration (cart quantities and homepage slides)

This revision adds the following database migrations:

```text
cart/0002_cartitem_constraints_and_timestamps.py
store/0025_homeslide.py
```

The existing DigitalOcean `migrate` pre-deploy job applies both automatically.
The cart migration merges duplicate product rows before adding the unique
constraint, so it is safe for the existing Neon database.

After deployment, add slideshow entries under **Store > Home slides** in Django
admin. If there are no active entries, the frontend continues to use its local
fallback images.
