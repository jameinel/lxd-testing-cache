# Cache CLI design

## Goal

Make the local apt cache and optional snap store proxy easy to use with Juju
controllers, while separating local configuration from changes to existing
controllers and models.

The normal workflow should be short, require only one setup command, and apply
the cache to all models on a controller by default. Configuration changes must
not silently overwrite settings that the tool does not recognize as its own.

## Intended workflow

```sh
./manage-cache.py setup --snap
./manage-cache.py controller apply
```

`setup --snap` creates or reuses the cache, enables the snap proxy, registers it
interactively if necessary, and configures the host and the client-side LXD
cloud. It should not require a separate registration command or a second setup
run. A later run reuses an existing registration and cache.

`controller apply` targets the current Juju controller. It updates that
controller's model defaults for future models and applies the settings to
existing models. An optional controller name selects another controller:

```sh
./manage-cache.py controller apply my-controller
```

The operation applies the saved snap choice: apt caching is enabled, while
snap proxy use is enabled or disabled according to that choice. Rerunning it
reconciles the controller after a snap toggle.

`controller remove [CONTROLLER]` removes tool-managed proxy settings from the
controller defaults and targeted existing models. It does not stop the cache
or change local settings. For the less common case of changing one existing
model, provide `model apply CONTROLLER:MODEL` and
`model remove CONTROLLER:MODEL`.

## Local setup and snap mode

- `setup` creates or reuses the cache and configures the host and the local
  client-side LXD cloud by default. It does not modify existing controllers.
- `setup --no-cloud` skips cloud configuration and must work without Juju.
  `setup --no-host` leaves host settings alone.
- Snap use is a persistent desired choice, initially off. Plain `setup` uses
  the saved choice; `setup --snap` enables it.
- If snap is enabled and registration is needed, setup installs the required
  components and runs the interactive registration as part of the same
  workflow. It configures local consumers only after registration succeeds.
  Failure must be visible and must not be reported as a successful snap setup.
- `snap on` installs and registers the proxy if needed, records the desired
  choice, and updates the host and client cloud previously managed by setup.
  `snap off` records the disabled choice and removes tool-managed snap settings
  from those local destinations. Neither command changes existing controllers
  or models; run `controller apply` or `model apply` to apply the choice there.
- Turning snap use off retains the proxy's registration and cached data. It
  must not silently interrupt known consumers that still use the proxy; warn
  about them and distinguish disabling client configuration from stopping the
  proxy service.
- A separately invocable registration operation may remain available, but it
  is not part of the normal setup workflow.

An existing cloud definition must be preserved and merged rather than replaced
wholesale. Preserve unrelated cloud fields and check for conflicting proxy
settings before modifying it. Local configuration should likewise not silently
replace unrelated host settings. Destinations skipped during setup should not
be unexpectedly configured by a later snap toggle.

Only HTTP is cached by apt-cacher-ng. Keep HTTPS direct for apt and do not set
`https_proxy`.

## Controller and model safety

The default `controller apply` operation sets defaults for new models and
updates existing models. It must first discover and inspect every target,
including the controller defaults, before applying any changes.

- Treat an unset value, the desired value, or a value recorded as previously
  managed by this tool for that target as safe to update. Other existing values
  are conflicts and require an explicit override.
- Report the affected targets, settings, and conflicts clearly. An inaccessible
  model blocks the operation unless explicitly excluded.
- Allow selected models to be excluded, so remaining models can be updated
  after a successful preflight. Provide an explicit override for conflicts and a
  defaults-only option for users who do not want existing models changed.
- Apply the same preflight and conflict protection to removal and to
  single-model operations. Turning snap off and applying that choice must
  clear snap settings, not merely omit new values. Empty model overrides may
  be needed to disable a snap proxy inherited from controller defaults.
- Track successful changes per target so later runs can distinguish managed
  values from user changes. Existing settings from older versions that cannot
  be identified as managed are conflicts, not presumed safe.

Preflight prevents known conflicts or inaccessible targets from causing
partial application. It cannot provide a transaction across Juju models:
permissions, network failures, or other errors can occur after preflight.
Report exactly which targets succeeded or failed, retain accurate state for
successful changes, and make rerunning safe. Do not claim an all-or-nothing
guarantee.

## Cleanup and visibility

Provide explicit ways to remove tool-managed settings from each destination,
without resetting unrelated settings. Stopping or destroying the cache should
warn that controllers or models may still depend on it; the tool cannot assume
it knows every consumer. Removing local settings and stopping the service must
not silently reset unrelated controllers.

Status should distinguish the saved snap choice, proxy installation and
registration, service reachability, and configured destinations. It should
make it clear when a change to the desired snap mode has not yet been applied
to a known controller or model.

## Command structure

`setup` and `status` remain short top-level commands. Other operations use
resource/action names: `snap on/off/register`, `controller apply/remove`,
`model apply/remove`, `host remove`, `cloud remove`, `cache stop/destroy`,
`upgrade skip/restore`, `images refresh`, and `charmcraft env`.

The old standalone and hyphenated spellings are removed rather than retained
as aliases. `cache stop` and `cache destroy` require `--force` to acknowledge
that clients may still depend on the cache; neither resets client settings.
Proxy settings from older versions without recorded ownership are not presumed
safe to overwrite.

## Implementation decisions still to resolve

- Verify what cloud settings a newly bootstrapped controller actually
  inherits, and whether multi-region client cloud definitions can be updated
  without losing their default-region order.
- Improve status reporting of pending controller and model changes.
