# Property Revenue Dashboard — Debugging Walkthrough

This document summarizes the bugs identified in the Property Revenue Dashboard system and the fixes applied. Three issues were reported by clients and the finance team; each is documented below with **symptom**, **root cause**, and **fix**.

---

## Reported Issues

1. **Sunset Properties (Client A)** — *"The revenue numbers on your dashboard don't match our internal records. We're showing different totals for March, and we're worried about accuracy for our board meeting next week."*
2. **Ocean Rentals (Client B)** — *"Something strange is happening — sometimes when we refresh the page, we see revenue numbers that look like they belong to another company. This is a serious privacy concern."*
3. **Finance team** — *"Some revenue totals seem 'slightly off' by a few cents here and there, but [we] couldn't pin down exactly when or why."*

## Summary of Fixes

| # | Issue | Root cause | File | Commit |
|---|-------|-----------|------|--------|
| 1 | Sunset's March mismatch | DB pool init failure → mock data fallback | `backend/app/core/database_pool.py` | `1969aaaf7` |
| 2 | Ocean's cross-tenant leak | Cache key omitted `tenant_id` | `backend/app/services/cache.py` | `f038f4715` |
| 3 | Finance's cents drift (latent) | `float()` at API boundary trades precision for IEEE 754 | `backend/app/api/v1/dashboard.py` | `0ea26988f` |
| – | (Prerequisite) Frontend env blocker | `.dockerignore` excluded `.env` from Vite build | `frontend/.dockerignore` | `68d8cd439` |

---

## Issue 1 — Sunset Properties: March Revenue Mismatch

### Symptom
Revenue totals shown on the dashboard did not match Sunset's internal records for March. Time-sensitive because of an upcoming board meeting.

### Investigation
The initial hypothesis was a timezone boundary issue: the seed contains a reservation at `2024-02-29 23:30+00` UTC, which falls on `2024-03-01 00:30` in `Europe/Paris` (Sunset's timezone). If our system bucketed it by UTC while Sunset bucketed it by Paris time, the totals would diverge by exactly that reservation's amount.

Code inspection ruled this out:
- `/api/v1/dashboard/summary` accepts only `property_id` — there is no `month` parameter; the endpoint sums all reservations for the property.
- `calculate_monthly_revenue` in `reservations.py:5` (which would make timezone relevant) is dead code: the body returns `Decimal('0')` and no caller exists.

The actual signal was in the backend logs — every revenue request produced:
```
ERROR:app.core.database_pool:❌ Database pool initialization failed:
  'Settings' object has no attribute 'supabase_db_user'
Database error for prop-001 (tenant: tenant-a): Database pool not available
```

The pool failed to initialize on every call, and `calculate_total_revenue` fell back to hardcoded mock data in `reservations.py:88-109`. The mock for `prop-001` returned `{'total': '1000.00', 'count': 3}`, but Sunset's actual `prop-001` has **four** reservations summing to `2250.000`. The mock had been crafted to exclude the timezone-trap reservation `res-tz-1` — producing exactly the mismatch Sunset reported.

### Root cause — three stacked issues in `database_pool.py`

1. **Non-existent settings fields** — `initialize()` constructed the URL from `settings.supabase_db_user`, `supabase_db_password`, `supabase_db_host`, `supabase_db_port`, and `supabase_db_name`. None of these are defined on the `Settings` class; only `settings.database_url` exists.
2. **Sync pool on async engine** — `poolclass=QueuePool` was passed to `create_async_engine`. SQLAlchemy 2.0's async engine rejects sync `QueuePool` and requires `AsyncAdaptedQueuePool` (which it selects by default).
3. **Async signature with sync body** — `get_session()` was declared `async def` but its body was a sync `return self.session_factory()`. Callers using `async with db_pool.get_session() as session:` were trying to enter a coroutine, which is not an async context manager.

### Fix
`backend/app/core/database_pool.py` (commit `1969aaaf7`):
- Construct the URL from `settings.database_url` with an asyncpg driver prefix.
- Drop the explicit `poolclass=QueuePool`; the default `AsyncAdaptedQueuePool` is correct for the async engine.
- Change `get_session` from `async def` to `def`. It returns an `AsyncSession`, which is itself an async context manager.
- Correct `get_db_session`'s return annotation to `AsyncIterator[AsyncSession]` to match its yield-based shape.

### Verification

| | Sunset → prop-001 response | Startup log |
|---|---|---|
| Before fix | `{"total_revenue": 1000.0, "reservations_count": 3}` (mock) | `❌ Database pool initialization failed` |
| After fix  | `{"total_revenue": 2250.0, "reservations_count": 4}` (real DB) | `✅ Database connection pool initialized` |

After the fix, Sunset's dashboard matches their internal record of 2250.00 across four reservations.

---

## Issue 2 — Ocean Rentals: Cross-Tenant Data Leak

### Symptom
Ocean reported that *sometimes* on refresh, the dashboard showed revenue numbers that "looked like they belonged to another company". Flagged as a privacy concern.

### Investigation
The intermittency ("sometimes") was the decisive clue — deterministic bugs show on every request, while intermittent behavior almost always points to caching or timing.

The `properties` table uses a composite primary key:
```sql
CREATE TABLE properties (
    id TEXT NOT NULL,
    tenant_id TEXT REFERENCES tenants(id),
    ...
    PRIMARY KEY (id, tenant_id)
);
```

So the same `property_id` can exist for multiple tenants as different properties. In the seed:
- `prop-001` for `tenant-a` is "Beach House Alpha" (`Europe/Paris`).
- `prop-001` for `tenant-b` is "Mountain Lodge Beta" (`America/New_York`).

But in `backend/app/services/cache.py:13` the cache key was:
```python
cache_key = f"revenue:{property_id}"   # tenant_id missing
```

Whichever tenant requested first populated `revenue:prop-001` for the 5-minute TTL; the other tenant's subsequent request hit the cache and received the wrong tenant's data.

This also accounts for Sunset's "March totals don't match" complaint as a symmetric symptom — if Ocean's `prop-001` (which has zero reservations) was cached first, Sunset would see `$0` instead of their actual `$2250`. One root cause, two distinct user-reported symptoms.

### Root cause
The cache key did not include `tenant_id`, while the underlying data model required `(id, tenant_id)` for unique identification of a property.

### Fix
`backend/app/services/cache.py:13` (commit `f038f4715`):

```diff
- cache_key = f"revenue:{property_id}"
+ cache_key = f"revenue:{tenant_id}:{property_id}"
```

Existing keys in the old format expire on their own within the TTL; `redis-cli FLUSHALL` can be used for an immediate effect after deployment.

### Verification
Tested three scenarios (Redis flushed before each):

| Scenario | Sunset response | Ocean response | Cache keys after |
|----------|-----------------|----------------|------------------|
| Sunset first → Ocean (prop-001) | 2250.0 / 4 | 0.0 / 0 | `revenue:tenant-a:prop-001`, `revenue:tenant-b:prop-001` |
| Ocean first → Sunset (prop-001) | 2250.0 / 4 | 0.0 / 0 | same (order-independent) |
| Sunset → Ocean (prop-002, Ocean does not own) | 4975.5 / 4 | 0.0 / 0 | isolated |

Each tenant now sees only its own revenue.

---

## Issue 3 — Finance Team: Cents Drift (Latent)

### Symptom
Revenue totals appeared "slightly off by a few cents" intermittently. No clear reproduction path; finance could not pin down when or why.

### Investigation
`total_amount` is stored as `NUMERIC(10, 3)` — three decimal places, sub-cent precision preserved. The seed contains three reservations of `333.333`, `333.333`, and `333.334`. In `Decimal` arithmetic these sum to exactly `1000.000`; in Python's binary `float` they sum to `999.9999999999999`.

Tracing the data flow:

| Step | Form | Example value | Precision |
|------|------|---------------|-----------|
| Postgres `SUM(NUMERIC)` | `Decimal` | `Decimal('2250.000')` | Exact |
| `reservations.py:68` (`Decimal(str(...))`) | `Decimal` | `Decimal('2250.000')` | Preserved |
| `reservations.py:72` (`str(...)`) | `str` | `"2250.000"` | Preserved |
| Redis (JSON string) | `str` | `"2250.000"` | Preserved |
| **`dashboard.py:18` (`float(...)`)** | **`float`** | **`2250.0`** | **Fragile boundary** |

With the current seed every SUM happens to land on a clean cent (`2250.000`, `4975.50`, etc.), all of which `float` represents exactly — so the bug does not currently surface. However, the fragile path is real: if a future timezone fix excluded `res-tz-1` (leaving only the three `333.333` reservations), or if currency conversion were introduced, the drift would appear immediately.

### Root cause
Direct `float()` conversion of a `Decimal` string at the API output boundary, with no rounding to the currency's natural precision (USD cents = two decimal places).

### Fix
`backend/app/api/v1/dashboard.py:18` (commit `0ea26988f`):

```python
from decimal import Decimal, ROUND_HALF_UP

# Sum exactly in the database (Decimal), round to USD cents at the API
# output boundary, then convert. Sum-then-round preserves precision that
# round-then-sum would have lost.
total_decimal = Decimal(revenue_data['total'])
total_cents = total_decimal.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
return {
    "total_revenue": float(total_cents),
    ...
}
```

Two-decimal cent values are always exactly representable in IEEE 754, so the subsequent `float()` is now safe.

### Verification
Response values are identical before and after the fix for the current seed (all sums already land on clean cents). The change is defensive — it eliminates the fragile boundary where drift could occur given different data. A targeted unit test (e.g. round-tripping a sum of three `333.333` reservations and asserting the response is exactly `1000.00`) would catch regressions.

---

## Prerequisite — Frontend Environment Blocker

Before the bug fixes above could be observed end-to-end, a separate build-configuration issue had to be resolved.

`frontend/.dockerignore` excluded `.env` from the Docker build context. Because the frontend SPA is built inside Docker and Vite inlines `import.meta.env.VITE_*` at build time, `VITE_BACKEND_URL` was inlined as `undefined`. The bundle shipped with `"undefined/api/v1"` as its API base URL; every request resolved to `localhost:3000/undefined/...` and hit nginx's SPA fallback, which returned `index.html` (HTML). Client-side JSON parsing then failed with `Unexpected token '<', "<!DOCTYPE "...`.

Fix (commit `68d8cd439`): comment out the `.env` exclusion in `frontend/.dockerignore`.

---

## How to Verify Locally

1. Build and start the stack:
   ```bash
   docker-compose up --build
   ```
2. Open the frontend at http://localhost:3000 and sign in:
   - **Sunset Properties**: `sunset@propertyflow.com` / `client_a_2024`
   - **Ocean Rentals**: `ocean@propertyflow.com` / `client_b_2024`
3. Select each property in the dropdown. Expected per-tenant totals:

   | Property | tenant-a (Sunset) | tenant-b (Ocean) |
   |----------|-------------------|------------------|
   | prop-001 | 2250.00 / 4       | 0.00 / 0 *(Mountain Lodge Beta has no bookings)* |
   | prop-002 | 4975.50 / 4       | 0.00 / 0 *(not owned by Ocean)* |
   | prop-003 | 6100.50 / 2       | 0.00 / 0 |
   | prop-004 | 0.00 / 0          | 1776.50 / 4 |
   | prop-005 | 0.00 / 0          | 3256.00 / 3 |

4. To confirm cache isolation, inspect Redis:
   ```bash
   docker exec $(docker ps -qf "ancestor=redis:alpine") redis-cli KEYS "*"
   # revenue:tenant-a:prop-001
   # revenue:tenant-b:prop-001
   ```

---

## Notes & Limitations

- **Bug 3 is latent.** The fix removes a fragile precision boundary but does not change current response values, because every seed-data SUM coincidentally lands on a clean cent. The change is defensive — it prevents future drift if upstream data or processing changes.
- **`calculate_monthly_revenue` in `reservations.py` remains dead code.** A timezone-aware monthly endpoint was not in scope for this debugging exercise; if and when wired up, it would need to slice month boundaries in each property's local timezone and use `Decimal` arithmetic end-to-end.
- **Stale cache entries** in the pre-fix key format (`revenue:{property_id}` without tenant) expire automatically within the 5-minute TTL after the fix deploys. `redis-cli FLUSHALL` produces an immediate clean state if needed.
- **Each fix is an isolated commit** so the git history reads as a clean per-issue trail. See `git log --oneline` for the sequence.
