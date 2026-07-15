# Contributing

Thanks for your interest! A few things to know up front:

- **This is a hobby-scale project** run by one person for a real community deployment.
  Issues and PRs are welcome; response times are best-effort.
- The reference deployment tracks this repo, so changes are held to "would I run this
  for my friends tonight" quality.

## Dev setup

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
cp .env.example .env
alembic upgrade head
pytest
```

## Before you open a PR

- `pytest` — the suite is hermetic (no Proxmox/network needed) and must stay that way:
  new tests mock at the `ProxmoxClient`/service boundary like the existing ones.
- `ruff check .` **and** `ruff format --check .` — CI runs both.
- Schema changes need an Alembic migration (`alembic revision -m "..."`), and
  migrations must be no-ops on data they don't own (fresh installs run the whole chain).
- One in-flight idea per PR. For anything architectural, open an issue first —
  the security model (docs/ARCHITECTURE.md) is deliberate and some "missing features"
  (e.g. platform SSH access to tenants) are missing on purpose.

## Security

Found a vulnerability? Please **don't** open a public issue — use GitHub's private
vulnerability reporting on this repo.
