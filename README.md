# Streamers Hub Vault (API)

FastAPI + async SQLAlchemy + PostgreSQL. Serves the website, Hub OS, the Investor Vault, the Designer Studio and the Clipper Desk.
Built like Ideation Vault: `app/core` (config, database, security, access), `app/models`, `app/services` (business rules),
`app/api/v1/endpoints`. Email goes through AWS SES exactly like the school systems (`services/SesEmailService.py`).

## Run it

```bash
python3.12 -m venv venv && ./venv/bin/python -m pip install -r requirements-dev.txt
cp .env.streamershub.example .env.streamershub       # then edit; .env is the shared school file
./venv/bin/python -m scripts.seed --reset             # SAMPLE DATA ONLY, refuses in production
./venv/bin/uvicorn app.main:app --port 8010 --reload
```

Or, to change code without restarting (the server keeps its state and connections), run it under jurigged instead of
`--reload`. It patches functions in place as you save; anything created at startup (new routes, new models or columns,
settings) still needs a restart.

```bash
./venv/bin/python -m jurigged -v -w 'app/**/*.py' -m uvicorn app.main:app --port 8010
```

The seed prints the sample password, three investors' Holder IDs and two onboarding links. Sample accounts are
`<name>@streamershub.test` (founder, philip, bernard, aaron, ahmed, certified, kingmille, c9per, trigger, storeops,
galacta, editor, ama_draws, kofi_inks, kwesi_cuts, …). Mail to `.test` addresses is never sent; their one-time codes
appear in the API log.

## Going live: the first account

A real database starts empty, and every account is invited from inside, so the Founder comes first:

```bash
./venv/bin/python -m scripts.create_founder --name "Your Name" --email you@streamershubmedia.com
./venv/bin/python -m scripts.create_founder --resend     # if the 7-day link expired unused
```

It creates the database, tables and departments if needed, makes the Founder account (refusing if one already
exists) and emails a set-password link through SES. The Founder then signs in, does staff onboarding, and invites the
co-founders from Hub OS → People (role "Co-founder"). Add `--print-link` only if SES isn't ready yet.

## YouTube view counts for clips (optional)

Set `SH_YOUTUBE_API_KEY` (Google Cloud → enable "YouTube Data API v3" → Credentials → API key; restrict it to that API).
YouTube Shorts clips then get YouTube's public view count when submitted and once a day until their month's payout run
is approved, using a small part of the free daily quota. The Social Media Manager still verifies every clip: the count is
shown as a guide, with a warning when the clipper reported much more. Other platforms are checked by hand, as before.

## Hubby, the Hub OS helper (AI optional)

Hubby answers "where do I…" questions in Hub OS, the Designer Studio, the Clipper Desk and the Investor Vault. It always
works: without an Anthropic API key it answers from its built-in guide (`app/services/hubby_guide.py`), searched in the
browser. To turn on AI answers, put `SH_ANTHROPIC_API_KEY` in `.env.streamershub` (`ANTHROPIC_API_KEY` is read too) and
restart. AI answers stream to the page as they're written (`POST /hubby/ask` is server-sent events, so a proxy in front
must not buffer it). If the AI errors, sends no words within `SH_HUBBY_TIMEOUT_SECONDS` (8), or breaks off mid-answer,
that question is answered from the guide; after 3 failures in a row it stays on the guide for 5 minutes. The Founder turns AI on or off, picks the model and sets a monthly
spending cap and a daily limit per person in Settings → Hubby. Update the guide when a page or button is renamed.

## Shared `.env`, separate data

`.env` is a copy of the school server's file (SES, S3, JWT secret). Anything that must not be shared is under an `SH_`
name in `.env.streamershub`, which overrides `.env`:

- **Database**: `SH_DATABASE_URL`, or the school's Postgres server with database `streamers_hub`. Never a school database.
- **Files**: the shared bucket under the `streamers-hub/` prefix, private, served by signed URLs.
- **Payments**: only `SH_PAYSTACK_SECRET_KEY`. Without it the store runs in test mode (never in production).
- **Sender**: `SH_FROM_EMAIL` must be a verified SES identity for streamershubmedia.com.

## Schema changes

Tables come from the models; later columns go in `COLUMN_MIGRATIONS` in `app/core/database.py` as idempotent SQL.
No Alembic, same as the school systems and Ideation Vault.

## Tests

```bash
SH_TEST_DATABASE_URL=postgresql+asyncpg://user@localhost:5432/streamers_hub_test SH_BCRYPT_ROUNDS=4 ./venv/bin/python -m pytest
```

The test database is wiped and seeded per module. Covers every rule in section 7, permissions for every role,
the Holder ID (format, hashing, lockout, step-up, shown-once, reissue), and one end-to-end test per workflow.

## Background jobs

`services/Scheduler.py` runs in the API process (like the schools' schedulers): commission becomes payable after the
refund window (daily), salary tiers are set on the 1st, the payout run is drafted on the 25th, and Clip Table ranks are
saved on Sundays. Set `SH_RUN_SCHEDULER=false` on all but one instance.
