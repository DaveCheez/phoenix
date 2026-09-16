# To-do implementation notes

This revision implements the non-payment items from the current Phoenix Vanz backlog.

## Backend changes

- Cart mutations are transaction-safe and return the fresh cart in the same response.
- A database constraint prevents duplicate rows for the same product in one cart.
- The cart migration merges any existing duplicate rows before adding that constraint.
- Quantity `0` removes an item; valid stored quantities are `1` to `999`.
- `GET /api/cart/`, `POST /api/cart/add/`, `PATCH /api/cart/update/`,
  `DELETE /api/cart/remove/` and `DELETE /api/cart/clear/` now return consistent
  response shapes.
- `HomeSlide` is manageable in Django admin and exposed at `GET /api/slides/`.
- Automated API tests were added for cart quantity behaviour and slide scheduling.

## Frontend changes

- Shared cart state now uses Nuxt `useState` rather than module-global Vue refs.
- Cart creation and loading are single-flight; mutations are queued so older
  responses cannot overwrite newer cart state.
- Quantity controls, direct number entry, removal and line totals are available
  on the cart page.
- A global accessible toast component reports success and error feedback.
- Homepage slides are loaded from Django, with the existing local images used as
  a fallback when no active slides exist.
- The Stripe browser key is read from
  `NUXT_PUBLIC_STRIPE_PUBLISHABLE_KEY` and must begin with `pk_test_` or
  `pk_live_`.

## Required environment variable

For local frontend development:

```env
NUXT_DJANGO_API_BASE=http://127.0.0.1:8000/api
NUXT_PUBLIC_CHECKOUT_ENABLED=false
NUXT_PUBLIC_STRIPE_PUBLISHABLE_KEY=pk_test_replace_me
```

For DigitalOcean, keep the existing backend URL, leave
`NUXT_PUBLIC_CHECKOUT_ENABLED=false` until the order API is complete, and set
the public Stripe variable only when testing checkout. A Stripe secret key must
never be placed in the frontend.

## Local verification

Backend:

```bash
python manage.py migrate
python manage.py test cart store
python manage.py runserver
```

Frontend:

```bash
npm install
npm run dev
```

## Deployment

Commit and push the backend first so the pre-deploy migration creates the cart
constraints and `HomeSlide` table. After the backend deployment succeeds, push
and deploy the frontend.

Online checkout is still deliberately fail-closed because the repository does
not yet contain an order model, server-side Stripe Checkout/PaymentIntent
creation, or webhook handling. Product option selections are also not yet
persisted on cart items, so option prices cannot safely be charged. These should
be built as a separate payment milestone after the charging rules are confirmed.
