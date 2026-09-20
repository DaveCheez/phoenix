# Phoenix Vanz Stripe deposit system

Orders use a 1/3 deposit and the remainder is due on completion.

## Production environment

Set these backend variables on DigitalOcean:

- `STRIPE_SECRET_KEY=sk_test_...` for testing, then `sk_live_...` for production.
- `STRIPE_WEBHOOK_SECRET=whsec_...` from the Stripe webhook endpoint.
- `SITE_URL=https://phoenixvanz.com`

No Stripe secret belongs in the frontend.

## Webhook

Register:

`https://api.phoenixvanz.com/api/orders/stripe/webhook/`

Subscribe to:

- `checkout.session.completed`
- `checkout.session.async_payment_succeeded`

The webhook changes a paid deposit order to `balance_due` and clears the cart. It changes a paid balance order to `paid`.

## Customer flow

1. Customer reaches checkout with a populated cart.
2. Django snapshots the cart prices into an Order.
3. Django calculates a 1/3 deposit rounded to pennies; the balance is the remainder.
4. Django creates a Stripe-hosted Checkout Session for the deposit.
5. Stripe redirects the customer back to the order confirmation page.
6. The webhook is the authoritative source for payment confirmation.
7. Staff can copy the balance payment URL from Django admin when the job is complete.
8. The customer can use `/balance/<token>` to pay the remaining balance via Stripe-hosted Checkout.

## Important pricing note

The order snapshot currently prices the Django cart's product base prices. Product option selections are not yet persisted in `CartItem`, so option-specific surcharges are not included in the order total by this implementation. That should be the next pricing task before taking live payments for products whose options change price.
