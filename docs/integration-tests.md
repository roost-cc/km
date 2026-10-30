# Running the integration tests

`test/ensure_int_test.py` brings up its own Infisical; three more test
modules talk to the Infisical instance `INFISICAL_HOST` names.

## The throwaway-instance test

`test/ensure_int_test.py` checks that `km publish` takes an Infisical
from an empty database to complete, with no UI step (F-54 S-958):

1. It starts `infisical/infisical:v0.165.16` with its postgres and
   redis from `test/infisical-empty.compose.yml`, under its own compose
   project name (`km-ensure-<pid>`) on a free `127.0.0.1` port, and
   waits for `/api/status`.
2. In a fixture project whose target `t` is a rendered client file
   followed by that Infisical, `km publish t dev test --all --execute`
   bootstraps the instance, creates the project, the `test`
   environment, the folders, the `km` and `services` identities and
   their tokens, mints the empty generated secrets and writes
   `targets/t.yaml`; the client file is re-rendered with the new
   project id and services token.
3. Every manifest address resolves, read with the services' token, and
   `km validate t dev test` is green.
4. A second publish creates nothing, leaves `targets/t.yaml`
   byte-identical, and validate is still green.
5. Teardown removes the compose project with its volumes
   (`docker compose down -v`), pass or fail.

It needs only Docker with the compose plugin: no `test/.env`, no
running Infisical, no token. It takes about 20 seconds once the images are pulled.

```sh
uv run pytest test/ensure_int_test.py
```

## The live-instance tests

Three test modules talk to a live Infisical instance over the network:

- `test/infisical_int_test.py` — exercises the `InfisicalClient`
  HTTP layer directly (set/get/list/delete round-trips).
- `test/publishers_infisical_int_test.py` — exercises
  `InfisicalPublisher.publish` + `collect` end-to-end.
- `test/bootstrap_int_test.py` — exercises the full `km rotate → km
publish` chain through `_rotate.run` / `_publish.run` against a
  one-destination target whose `targets/t.yaml` holds the test
  project id and token, then verifies the values landed in Infisical.

These tests are auto-marked `integration`. To skip them and keep test
runs offline:

```sh
uv run pytest -m "not integration"
```

All three skip unless `INFISICAL_HOST` is set. To run them, you need an
Infisical instance, a throwaway project on it the tests are allowed to
mutate, and a machine-identity token with read/write/delete on that
project. The setup is one-time per developer.

---

## 1. Point `INFISICAL_HOST` at an Infisical instance

Any Infisical instance works: a self-hosted one, or one you start
locally (for example from Infisical's own docker-compose, or from
`test/infisical-empty.compose.yml`). `INFISICAL_HOST` is its base URL,
e.g. `http://localhost:8080/`. You set it in `test/.env` in step 4.

Sanity-check it's reachable:

```sh
curl -sf "$INFISICAL_HOST/api/status" >/dev/null && echo OK
```

Once `INFISICAL_HOST` is set, an unreachable instance makes the tests
error on connection rather than skip: setting it is the opt-in.

---

## 2. Create a throwaway test project

(The live-instance tests predate `ensure`; steps 2 and 3 are the manual
setup they still use. `test/ensure_int_test.py` above needs neither.)

In the Infisical UI (at `INFISICAL_HOST`):

1. **Organization → Projects → New Project**.
2. Name it something obvious — e.g. `km-test`. Anything works; the
   name isn't read by the tests.
3. Open the new project and copy its **Project ID** from the URL or
   the project settings page. It's a UUID, e.g.
   `ff547714-3a94-4ad5-bd4f-1522a62bd4a9`. You'll paste this into
   `INFISICAL_TEST_PROJECT_ID` in step 4.
4. Confirm the project has a `dev` environment — it's the default
   environment for new projects, so usually nothing to do here. The
   tests target `dev` and won't fall back.

> ⚠️ **Never reuse a real project for this.** `bootstrap_int_test.py`
> writes under `/s432-bootstrap/` and deletes everything it wrote on
> teardown. The `infisical_int_test.py` and
> `publishers_infisical_int_test.py` round-trips do the same under
> their own time-stamped paths. Pre-flight checks refuse to run if the
> target folders are non-empty, but a misconfigured token pointed at a
> real project is one typo away from data loss.

---

## 3. Mint a machine-identity token

The tests authenticate as a project-scoped machine identity. Create it
and issue its token entirely from inside the test project — no
organization-level steps needed.

### 3a. Create the project machine identity

1. Open your test project, then **Access Control → Machine Identities
   → Add Machine Identity to Project**.
2. On the dialog, leave the toggle on **Create New**, name it
   something obvious (e.g. `testing identity`), and pick the
   `Developer` role — that grants read/write/delete on secrets, which
   is what the tests need.
3. Click **Create**. The identity lands in the project's machine
   identities list with **Managed by: Project**.

### 3b. Add Token Auth

1. Click into the new identity.
2. In the **Authentication** card, click **Add Auth Method** and pick
   **Token Auth**.

### 3c. Issue a token

1. Expand the new **Token Auth** row and click **Add Token**.
2. Give it a name (e.g. `Testing Token`) and click **Create**.
3. Set **Access Token TTL** and **Access Token Max TTL** to `0` to
   make tokens non-expiring (`0` is Infisical's never-expires
   sentinel). Leave **Max Number of Uses** at `0` (unlimited). Click
   **Add**.
4. Copy the JWT (`eyJhbGc…`). **You only see it once.**

You'll paste this into `INFISICAL_TEST_TOKEN` in step 4.

> A non-expiring dev token is fine for a throwaway project that holds
> no real secrets. Don't reuse this pattern for production identities.

If you'd rather not mint a never-expiring token, pick a TTL you're
willing to manage — e.g. 30 days — and re-issue when `test/.env`
starts erroring out.

---

## 4. Populate `test/.env`

Copy the template and paste in the three values:

```sh
cp test/example.env test/.env
$EDITOR test/.env
```

Fill in:

```ini
INFISICAL_HOST=<your-infisical-base-url>
INFISICAL_TEST_PROJECT_ID=<your-throwaway-project-uuid>
INFISICAL_TEST_TOKEN=<your-machine-identity-jwt>
```

`test/.env` is gitignored — it lives only on your machine.

The pytest harness loads this file automatically at session start
(`test/conftest.py`'s `_load_dotenv`). Existing shell exports win over
the file, so you can override a single variable for one run without
editing it:

```sh
INFISICAL_TEST_TOKEN=other-token uv run pytest test/bootstrap_int_test.py
```

---

## 5. Run the suite

```sh
uv run pytest
```

Expected with `test/.env` populated and the instance up: every
integration test runs.

Without `INFISICAL_HOST` (no `test/.env`, or the value empty), all three
live-instance modules skip at module level:

```
============= N passed, 3 skipped =============
```

With `INFISICAL_HOST` set but a token or project id missing:

- `infisical_int_test.py` and `publishers_infisical_int_test.py`
  skip (their module-level gate sees the empty value).
- `bootstrap_int_test.py` errors loudly: once `INFISICAL_HOST` opts in,
  a missing token surfaces as a hard `RuntimeError` during fixture
  setup rather than a silent skip.

If you want to run only the offline suite while iterating:

```sh
uv run pytest -m "not integration"
```

---

## Troubleshooting

| Symptom                                                                     | Likely cause                                                                                                   |
| --------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------- |
| `RuntimeError: INFISICAL_TEST_TOKEN is unset`                               | `test/.env` missing or the variable is empty.                                                                  |
| `httpx.ConnectError` / connection refused                                   | The instance at `INFISICAL_HOST` isn't up, or the URL is wrong. Check it with the `curl` in step 1.            |
| `RuntimeError: Failed to list secrets: 401`                                 | Token expired or wrong identity. Re-issue.                                                                     |
| `RuntimeError: Failed to list secrets: 403`                                 | Identity's role doesn't have read/write/delete on secrets. Re-check step 3a — `Developer` or higher is needed. |
| `pytest.fail: Refusing to run bootstrap test: /s432-bootstrap is not empty` | A previous run died before cleanup. Delete the folder in the Infisical UI (or via `mc rb` equivalent), retry.  |
| Tests pass locally but a teammate can't run them                            | Each developer needs their own `test/.env` — the file is gitignored and creds are per-developer.               |
