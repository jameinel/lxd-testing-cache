# lxd-testing-cache

Speed up Juju/LXD (and charmcraft) testing with a local apt cache and snap store
proxy. `manage-cache.py` runs [apt-cacher-ng](https://www.unix-ag.uni-kl.de/~bloch/acng/)
and `snap-store-proxy` in one LXD container and configures local and Juju clients.

## Requirements

- Python 3
- LXD (`lxc`) and, for the Juju commands, `juju`
- An Ubuntu One account (the snap store proxy must be registered once)

## Usage

```
./manage-cache.py setup --snap     # configure locally; register snap if needed
./manage-cache.py controller apply # current controller's defaults and models
./manage-cache.py status           # container, local settings, Juju defaults
./manage-cache.py snap off         # stop using the snap proxy locally
./manage-cache.py controller apply # apply the changed snap choice to Juju
```

`setup` preserves an existing container and its cache. It configures this host
using `sudo` and updates the client-side LXD cloud for future controller
bootstraps. Use `--no-host` or `--no-cloud` to skip either destination;
`--no-cloud` does not require Juju. The snap proxy is initially off.
`setup --snap` installs it and prompts for one-time registration when needed.
The saved choice is used on later setup runs.

`snap on` and `snap off` update local destinations previously configured by
`setup`; they do not change existing Juju controllers or models. Disabling snap
use retains its registration and does not stop the proxy service. Local
configuration refuses to overwrite proxy settings it cannot identify as managed
by this tool. It also refuses to update multi-region clouds because Juju's JSON
output does not preserve their default-region order. State is stored under
`${XDG_CONFIG_HOME:-~/.config}/lxd-testing-cache`.

### Juju controllers and models

`controller apply [CONTROLLER]` updates defaults for each cloud on the selected
controller and all existing models. It preflights every target before making
changes. Use `--exclude-model MODEL` (repeatable) to skip models,
`--defaults-only` to leave existing models alone, or `--force` to override
reported conflicts. Model listing requires access to all models on the
controller. Juju does not provide a cross-model transaction, so a later command
failure can still leave partial changes; the tool reports completed targets.

`controller remove [CONTROLLER]` removes settings recorded as managed by the
tool. `model apply CONTROLLER:MODEL` and `model remove CONTROLLER:MODEL` target
one model instead. Removing model settings resets them to inherited controller
or region defaults, which may still point at a proxy. The controller defaults
and individual models must be removed separately if they were configured
separately. Existing proxy settings from older versions are not assumed to be
managed; apply reports conflicts instead of silently overwriting them.

### Cleanup and other commands

`host remove` and `cloud remove` remove recorded local settings without
touching controllers. `cache stop --force` stops the container without
removing client settings; `cache destroy --force` also deletes its cache and
registration. Remove settings from every consumer first: the tool cannot
discover every client that depends on the cache.

Other commands use resource/action names: `snap register`, `upgrade skip`,
`upgrade restore`, and `images refresh`. The previous standalone `enable`,
`disable`, `destroy`, `apply`, and hyphenated commands have been removed.

### charmcraft

```
eval "$(./manage-cache.py charmcraft env)"
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
