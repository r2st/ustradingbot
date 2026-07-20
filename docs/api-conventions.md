# API response conventions

This documents a deliberate asymmetry in the dashboard JSON API so it reads as an
intentional back-compat choice rather than accidental drift (audit B-8).

## Errors — fully standardized

**Every** non-2xx response — a raised `HTTPException`, a request-validation
failure, or an unexpected bug — is rendered through the single `error_body()`
envelope by the exception handlers in `dashboard/middleware.py`:

```json
{
  "ok": false,
  "error": { "code": "validation_error", "message": "…", "status": 422 },
  "detail": "…",            // legacy alias, kept for the current UI
  "error_code": "…",        // legacy alias
  "request_id": "…"         // correlation id, when present
}
```

Clients can rely on `ok === false` and `error.message` for **all** failures.

## Success — bare dicts today, opt-in envelope for new endpoints

Success responses are **intentionally not** retrofitted onto a uniform envelope:

- Existing read endpoints (`/health`, `/api/mode`, analytics, history, …) return
  **bare dicts** shaped for the field the caller reads directly. The shipped
  single-file UI reads these fields inline, so changing their shape would break
  it silently.
- New endpoints may opt in to the `ok()` success envelope
  (`{ "ok": true, "data": … }`, `dashboard/http_util.py`) where it adds clarity.

### Why not migrate everything now?

The client is a 7,300-line single-file SPA that reads dozens of endpoint shapes
inline. A big-bang success-envelope migration is a high-risk, low-reward change
against a UI that already works. If/when uniform success responses are wanted,
they land behind an **`/api/v2`** version bump so `/api/v1`-era readers keep
working — not by mutating the current endpoints in place.

### Probes are deliberately bare

`/health`, `/readyz`, `/version`, and `/metrics` return their own operator- and
scraper-oriented shapes (Prometheus text for `/metrics`) and are **not** wrapped
in the envelope — that's expected.
