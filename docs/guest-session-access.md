# Guest session access

A guest session is an anonymous checkout credential. It is not a customer
account and it does not verify an email address.

## Token

Issuance uses `secrets.token_urlsafe(32)`. The raw token is 43 case-sensitive
characters from `A-Za-z0-9_-`. A presented token is hashed only when its type,
length and syntax already match. It is not trimmed or lowercased.

The database stores SHA-256 of the exact UTF-8 token as a lowercase
64-character hex digest. The raw token is returned once to the internal
caller and is not stored.

The raw token is intended for a future `HttpOnly` cookie set by Nuxt. Nuxt
would send it to Django as `Authorization: Bearer`. The browser-facing Nuxt
route still needs CSRF protection. A shared application secret would not
prove that a particular guest owns a particular cart.

## Lifetime and revocation

`created_at` and `expires_at` are set from the same issuance time. The
lifetime is 30 days. Reads do not extend it. The server rejects a session
unless `expires_at` is strictly later than the current time and `revoked_at`
is empty.

Revocation locks the guest session and sets `revoked_at`. Doing so again is
harmless. It does not delete the cart or any order. An operation that already
passed its access check may still finish. Later operations see the revocation.
Revocation does not undo that earlier work.

There is no recovery after the raw token is lost.

## Cart and later orders

A guest session has at most one current cart. Existing carts stay unbound.
Presenting an old cart UUID does not attach a new session to it.

Deleting the cart does not delete the guest session. `GuestSession` is the
stable identity. Later order access must check the current guest session,
including its expiry and revocation. Copying the token hash onto an order is
not enough, because that copy would not change when the session is revoked
or expires.

An order record can outlive the browser credential. A 30-day guest session
does not grant permanent order access, and this phase does not define how a
customer recovers an order after the credential is gone.

One guest session may later have more than one order, including more than one
order from the same source cart. This checkpoint does not add that link.

## What this checkpoint does not do

The public cart routes still accept a cart UUID alone. These helpers are not
wired into HTTP yet.
