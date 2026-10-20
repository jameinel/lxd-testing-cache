# lxd-testing-cache

Speed up Juju/LXD (and charmcraft) testing with a local apt cache.
`manage-apt-cache.py` runs [apt-cacher-ng](https://www.unix-ag.uni-kl.de/~bloch/acng/)
in an LXD container and points Juju model-defaults at it.

## Requirements

- Python 3
- LXD (`lxc`) and, for the Juju commands, `juju`

## Usage

```
./manage-apt-cache.py enable              # create the cache, set Juju model-defaults
./manage-apt-cache.py status              # container, Juju defaults, reachability
./manage-apt-cache.py disable             # clear defaults, stop container (cache kept)
./manage-apt-cache.py destroy             # disable, then delete container and cache
./manage-apt-cache.py apply CTRL:MODEL    # point an existing model at the cache
./manage-apt-cache.py skip-os-upgrade     # skip apt update/upgrade on new machines
./manage-apt-cache.py restore-os-upgrade
./manage-apt-cache.py refresh-images      # refresh cached LXD images
```

Commands taking a controller default to the current Juju controller.
Model-defaults only affect models created afterwards; use `apply` for existing ones.

### charmcraft

```
eval "$(./manage-apt-cache.py charmcraft-env)"
charmcraft pack
```

Prints nothing if the cache is stopped or unreachable, so it is always safe.

Only plain HTTP is cached; snap, pip and other HTTPS downloads are not.

## Configuration

| Variable         | Default        | Purpose                    |
|------------------|----------------|----------------------------|
| `CACHE_NAME`     | `apt-cache`    | cache container name       |
| `CACHE_IMAGE`    | `ubuntu:24.04` | container image            |
| `LXD_CLOUD_NAME` | `lxd`          | client-side Juju cloud     |

Run `./manage-apt-cache.py --help` for details.
