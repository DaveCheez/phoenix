# Public unpaid order API

Application authentication runs first. The guest bearer is the only customer
credential. An order reference, cart id, email address or review marker does
not prove ownership.

`cart.cart_review_token` is an opaque consistency marker for the basket being
reviewed. It is not an authentication credential. The server returns it on
the cart payload. The client sends that same value back when creating an
order. The client does not calculate prices into it.

Image URLs and unrelated timestamps are not part of the marker. There is no
quote table and no extra secret for this marker.

## Cart

Every successful cart payload includes `cart.cart_review_token`. The rest of
the cart object is unchanged. Read it from the cart response that shows the
basket the customer is reviewing. A later quantity, option or catalogue-price
change produces a different marker.

## Create

`POST /api/orders/`

Headers already required:

- `X-Phoenix-App-Credential`
- `X-Phoenix-Shopper-Address`
- `Authorization: Bearer <guest token>`
- `Idempotency-Key`: 8 to 80 characters from `A-Za-z0-9_-`

JSON body:

Required:

- `customer_name`
- `customer_email`
- `customer_phone`
- `vehicle_make`
- `vehicle_model`
- `vehicle_year` (JSON integer, not a string)
- `vehicle_wheelbase`
- `cart_review_token`

Optional:

- `vehicle_registration`
- `customer_notes`

Products, quantities and selected options are read from the guest's saved
`CartItem` and `CartItemOption` rows. The body must not include items,
totals, prices, deposit, tax, fitting charge, payment state or a shipping
address. Any other field is `400 ORDER_REQUEST_INVALID`.

A new attempt:

- A missing or malformed `cart_review_token` is `400 CART_REVIEW_REQUIRED`.
- A well-formed marker that does not match the locked, priced basket is
  `409 CART_CHANGED`.
- Those two rejections leave no order and no order-attempt row.
- The priced lines used for the comparison are the lines written into the
  snapshot. The request does not compare one price read and then store
  another.

The marker syntax the server emits is 64 lowercase hex characters. Anything
else, including uppercase hex, is malformed.

Successful create is `201`:

```json
{"success": true, "order": {}}
```

`order` keeps the existing public confirmation fields. It does not gain the
review marker.

## Retry

After the guest is authenticated, a committed attempt with the same
`Idempotency-Key` and the same customer fields returns the original order
before the current basket marker is compared. That response is `200` with
the same order envelope.

Changing the basket or the catalogue does not reprice or replace that saved
order. A retry may send the original marker, a stale marker, or omit the
marker; the stored order is returned when the customer fields match.

The stored attempt digest is still only the customer fields. Attempts saved
before this marker continue to match that digest.

The same key with different customer details is `409 IDEMPOTENCY_CONFLICT`.
The original order stays as it was.

`source_cart` is not unique. Creating an order does not clear the basket,
does not issue a new guest, and does not treat the order reference as
authorisation.

## Read

`GET /api/orders/<reference>/`

The creating guest's current bearer is required. Another guest, a revoked
bearer, an expired bearer, or the reference alone receives the existing
authentication or `404 ORDER_NOT_AVAILABLE` response. Those codes and bodies
are unchanged.

## Error envelope

Rejected order requests use:

```json
{"success": false, "code": "CART_CHANGED", "error": "The basket has changed. Review it again before placing the order."}
```

`CART_REVIEW_REQUIRED` uses the same shape with
`Review the basket before placing the order.`
Authentication and ownership failures stay on their existing codes.
Responses send `Cache-Control: no-store`.
