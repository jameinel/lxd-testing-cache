# AGENTS.md

## Project

`manage-apt-cache.py` is a single-file Python 3 CLI (stdlib only) that manages an
apt-cacher-ng LXD container and Juju model-defaults for faster Juju/LXD/charmcraft tests.
See `README.md` for usage.

## Conventions

- Keep it a single stdlib-only script; no third-party dependencies.
- Shell out to `lxc` and `juju` via the `run()`/`ok()` helpers.
- Each subcommand is a `cmd_*` function registered in `build_parser()`; update the
  `EPILOG` text and `README.md` when adding or changing commands or env vars.
- `charmcraft-env` must stay safe to `eval`: emit shell only on stdout, notes on stderr, exit 0.
- Only HTTP is cached; do not set `https_proxy`.
- Project name is `lxd-testing-cache` (not `lxd-test-cache`).

## Checking changes

- `python3 -m py_compile manage-apt-cache.py`
- `./manage-apt-cache.py --help` and `<command> --help`
- Don't run `enable`/`destroy` against the user's LXD/Juju without being asked.
