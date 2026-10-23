# lxd-testing-cache

Speed up Juju/LXD (and charmcraft) testing with a local apt cache and snap store
proxy. `manage-cache.py` runs [apt-cacher-ng](https://www.unix-ag.uni-kl.de/~bloch/acng/)
and `snap-store-proxy` in one LXD container and points Juju model-defaults at them.

## Requirements

- Python 3
- LXD (`lxc`) and, for the Juju commands, `juju`
- An Ubuntu One account (the snap store proxy must be registered once)

## Usage

```
./manage-cache.py enable     # create/reuse the cache, set Juju model-defaults
./manage-cache.py status     # container, Juju defaults, reachability
./manage-cache.py disable    # clear defaults, stop container (cache kept)
```

`enable` reuses an existing container and keeps its apt cache. It also points this
host's apt and snapd at the caches (via `sudo`); `disable`/`destroy` undo that. The
snap proxy needs a one-time `register-snap-proxy`, then `enable` again. See `--help`
for the other commands and options (`--no-snap`, `--no-host`, `apply`, ...).

### charmcraft

```
eval "$(./manage-cache.py charmcraft-env)"
charmcraft pack
```

Prints nothing if the cache is stopped or unreachable, so it is always safe.
Only plain HTTP is cached by apt-cacher-ng; charmcraft does not use the snap proxy.

## Configuration

| Variable         | Default        | Purpose                    |
|------------------|----------------|----------------------------|
| `CACHE_NAME`     | `apt-cache`    | cache container name       |
| `CACHE_IMAGE`    | `ubuntu:24.04` | container image            |
| `LXD_CLOUD_NAME` | `lxd`          | client-side Juju cloud     |

Run `./manage-cache.py --help` for details.
