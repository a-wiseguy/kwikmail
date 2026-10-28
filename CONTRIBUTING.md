# Contribute

Bug reports and small, focused pull requests are welcome. For larger changes, open an issue first.
Include your Python version, terminal, and steps to reproduce. Remove addresses, message content, and credentials from logs.

## Set up

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/).

```sh
git clone https://github.com/a-wiseguy/kwikmail.git kwikmail
cd kwikmail
uv sync
uv run kwikmail --demo
```

## Check your changes

```sh
uv run pytest -q
uv run ruff check src tests scripts
uv run ruff format --check src tests scripts
uv run python scripts/verify.py
uv build
```

Tests use the demo UI and local TLS servers. They do not send external email.
See [architecture](docs/architecture.md) and [benchmarking](docs/benchmarking.md) for implementation details.

## Refresh the readme screenshots

```sh
uv run python scripts/verify.py --output .agent-work/screenshots
cp .agent-work/screenshots/demo.svg docs/screenshots/inbox.svg
cp .agent-work/screenshots/compose.svg docs/screenshots/compose.svg
```

The screenshots use demo accounts. Keep run reports and other captures under `.agent-work/`.
