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

## HTTP enforcement

Every cart read and mutation first requires the separate application
credential in `X-Phoenix-App-Credential` and one shopper address in
`X-Phoenix-Shopper-Address`. Anonymous start means there is no guest bearer
yet. It still requires those two application headers. A missing, malformed
or unconfigured application credential, or a missing or malformed address,
returns `503 CART_APPLICATION_REJECTED` with `Cache-Control: no-store`.
That response is not the guest `401`, and it does not set a guest challenge.
Django accepts the address only because the application credential matched.
It cannot prove that the authorised server supplied the shopper's true
address. `REMOTE_ADDR`, `do-connecting-ip`, `X-Forwarded-For` and body or
query fields are not a fallback. The local frontend must send these headers
before browser cart calls will succeed. The guard is not relaxed for the
older transport.

Cart rate limits are implemented and off by default. ``CART_RATE_LIMIT_ENABLED``
must be the boolean true, with ``CART_RATE_LIMIT_POLICIES`` naming issuance,
failed_access and authenticated_cart, and with ``CART_RATE_LIMIT_HMAC_KEY``
set. A bad enabled configuration is ``503 CART_TEMPORARILY_UNAVAILABLE`` and
does not open the fail-open path. Production quotas are not filled in yet.

When enforcement is on, accounting commits before the cart transaction. An
explicit anonymous start counts issuance for the accepted shopper address. A
bad guest credential counts failed_access for that address and still returns
the guest ``401``. A resolved guest session counts authenticated_cart for its
UUID, including a missing-cart ``409`` and ordinary validation errors. A
``400 START_REQUIRED`` does not count. Application rejection does not count
and does not invent an address. ``GET`` may insert counter rows and does not
change the cart or session. A denial is ``429 CART_RATE_LIMITED`` with
``Retry-After`` and ``Cache-Control: no-store``. It does not mint another
guest. If issuance accounting is unavailable, the start is the existing
``503`` and creates nothing. If failed-access accounting is unavailable, the
guest ``401`` still stands. If authenticated accounting is unavailable, the
existing guest checks continue and a fixed log category is recorded.
``RateLimitStoreError`` is the only outage that uses those rules.
Authenticated cart still continues when only that counter store is down.
CSRF and reset do not use that fail-open rule.

``POST /api/cart/budget/`` is a separate application-only check for a later
Nuxt handler. It is publicly reachable and requires
``X-Phoenix-App-Credential`` and ``X-Phoenix-Shopper-Address``. It does not
accept a guest bearer instead, and it does not read one when the caller
sends it. The body is exactly ``{"operation": "csrf"}`` or
``{"operation": "reset"}``. Anything else is ``400 CART_BUDGET_REQUEST_INVALID``
and writes no counter. Unsupported media types stay the framework ``415``.
``OPTIONS`` does not account. Other methods stay framework errors. Every
cart response, including those errors, is ``Cache-Control: no-store``.

A valid request while enforcement is off returns ``200 CART_BUDGET_ALLOWED``
without reading the counter table, the HMAC key, or
``CART_FRONTEND_RATE_LIMIT_POLICIES``. When enforcement is on, that setting
must be exactly the ``csrf`` and ``reset`` window lists, and the existing
HMAC key must be valid. Ordinary cart routes keep the three-scope
``CART_RATE_LIMIT_POLICIES`` object and do not require the frontend object.
A bad enabled configuration is ``503 CART_TEMPORARILY_UNAVAILABLE``. A
counter-store failure is that same ``503`` for both operations. A denial is
``429 CART_RATE_LIMITED`` with a positive ``Retry-After``. Neither response
is a guest challenge, a credential, or a reusable permit. The attempt stays
counted if Nuxt later fails. This route does not select, issue, rotate, or
clear a guest or a cart.

Reversing ``cart/migrations/0006_frontend_budget_scopes.py`` restores the
older scope check. That reverse is not safe while ``csrf`` or ``reset`` rows
exist. Do not delete those rows to make the reverse succeed.

Nuxt integration is not done. The later order is:

CSRF: the existing host, Fetch Metadata and Origin or Referer checks, then
this authenticated budget call with ``operation=csrf``, and only then
bootstrap-cookie creation or reuse and token issuance. That call cannot
require the CSRF token it is about to issue.

Reset: the existing origin, JSON and session-bound CSRF checks, then this
budget call with ``operation=reset``, then the existing guest-access probe
and the explicit local reset. A denied or unavailable budget stops the
protected Nuxt action before any cookie change or token issuance.

The reset's later guest-cart probe is a different request. It may consume
``authenticated_cart`` or ``failed_access``. That is not a second charge for
the budget call. Nuxt omits the guest bearer on budget calls and sends the
application credential with the server-selected address. Only a validated
``200 CART_BUDGET_ALLOWED`` continues. Do not cache that decision and do not
retry the budget call automatically.

This adds a Django request to CSRF operations that used to stay inside Nuxt.
That changes their availability and latency. Update source request accounting
when the frontend calls it. Do not treat this backend route as a finished
storefront control.

A keyed digest is a pseudonym, not an anonymous identifier. IPv6 /64 grouping
is a counter policy, not proof of one household. Changing the HMAC key would
make new digests and can reset effective budgets. Row expiry is storage
grace only and does not delete rows until cleanup is scheduled. A committed
counter attempt is not refunded when a later cart write rolls back, and a
lost connection around commit is not exactly-once. A counter-store failure
is not the same as the whole application database being down. Requests that
never pass the application gate are outside these budgets, so edge protection
is still required for that flood.

On this branch, every cart read and mutation requires the guest bearer.
`POST /api/cart/create/` with no `Authorization` header and a body of exactly
`{"action": "start"}` is the only issuance path. It returns `guest_access.token`
and `guest_access.expires_at` once, with the normal cart payload. Django does
not set a guest cookie. This Django route is reachable by whoever can call the
API. It is not a private network endpoint. Nuxt must copy the token into its
own cookie and remove `guest_access` before any browser JSON is sent.

A missing header without that exact start body returns `400 START_REQUIRED`.
A present invalid header returns `401`, including when `action` is `start`.
A valid bearer returns the existing cart with `200` and does not rotate the
token or move `expires_at`. `action=start` does not replace that session.

`GET`, add, update, remove and clear resolve the cart from the bearer. A
supplied `cart_id` is only a consistency check. If body and query disagree, or
the value does not match the session cart, the response is the same `401`.
An active session with no cart returns `409 GUEST_CART_MISSING`.
`{"action": "replace_missing"}` with a valid bearer creates one empty cart on
that same session, or returns the current cart unchanged. It does not rotate,
extend or revoke the credential, and it does not clear an existing basket.

Failed guest access is `401` with `WWW-Authenticate: Bearer realm="cart"` and
`Cache-Control: no-store`. The body is `CART_ACCESS_UNAVAILABLE`. Database
availability failures are `503 CART_TEMPORARILY_UNAVAILABLE`. Validation and
conflict errors stay their existing responses after the bearer has been
accepted. A programming error is not turned into `503`.

The scheme name is case-insensitive. The token is not trimmed or lowercased.
Django sees one `HTTP_AUTHORIZATION` string. A comma-joined duplicate is
rejected. A web server that keeps only one of two raw duplicate headers does
not show the discarded value to this process.

Clear deletes items only. Reads do not extend expiry. Add is not idempotent:
a response can be lost after the commit, and repeating Add can insert another
quantity. Token expiry and revocation reject later requests. They do not
delete the stored cart or session rows.

Anonymous start keeps session creation, cart creation, payload construction
and serialization of that same 201 response in one transaction. A failure
while that response is being prepared rolls the new session and cart back,
and the new credential is not returned. A response lost after that commit, a
failure in later middleware, or an uncertain database commit is not
recoverable and is not made exactly-once. An unsuccessful client request does
not always mean that no session was stored. The server does not retry it.

The current production storefront still sends a `localStorage` cart id and
will not satisfy this contract. Do not deploy this branch until the matching
Nuxt change and the abuse limits below are in place. The basket cutover
decision is already approved.

## Release gates

Nuxt must remove issuance credentials before browser JSON. Browser CSRF needs
a value bound to this guest session. Rotating two unrelated cookies is not
that binding. The no-session start and the later reset both need an explicit
CSRF treatment. Origin checks must use the trusted configured site origins,
with apex and `www` treated as different origins when both serve the shop.

There is no browser `HttpOnly` cookie compare-and-set. Two tabs can both start
before either cookie is stored, and an in-process Nuxt map does not coordinate
multiple Nuxt instances. Concurrent starts have to be coordinated before
issuance. A mutation must not be replayed after an uncertain failure.

A limiter that only wraps Nuxt does not cover a direct call to this public
Django start route. The Django counters can be enabled only with approved
numerical policies. ``POST /api/cart/budget/`` is the Django check Nuxt must
call for CSRF and reset; the Nuxt handler itself is still pending. Still
required before release: those quotas, the Nuxt calls in the order above,
forwarding ``Retry-After`` through the frontend, cleanup scheduling and
backlog monitoring, and a coordinated cutover. A request rejected by the
application gate is not in these budgets.

The business owner has approved the basket cutover. Historical anonymous
carts remain stored and unbound. There is no UUID-based claim and no
ownership backfill. A customer explicitly starts a new empty basket. There
is no silent reset or deletion.

Rolling this enforcement back reopens UUID-only access to
carts that already have guest sessions, so a rollback has to be an explicit
security decision rather than a quiet restore of the old routes.
