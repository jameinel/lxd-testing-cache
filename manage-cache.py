#!/usr/bin/env python3
"""Local apt cache and snap store proxy (in an LXD container) for faster Juju/LXD tests."""

import argparse
from dataclasses import dataclass
import ipaddress
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
import time
import urllib.error
import urllib.request

NAME = os.environ.get("CACHE_NAME", "apt-cache")
IMAGE = os.environ.get("CACHE_IMAGE", "ubuntu:24.04")
PORT = 3142
LXD_CLOUD = os.environ.get("LXD_CLOUD_NAME", "lxd")
NO_PROXY = "localhost,127.0.0.1"
# snap-store-proxy serves on port 80 inside the container (not configurable).
# apt reuses the http proxy for https unless told otherwise, and apt-cacher-ng
# refuses CONNECT; "DIRECT" makes https repos bypass the cache.
HTTPS_DIRECT = "DIRECT"
SNAP_KEYS = ("snap-store-proxy", "snap-store-proxy-url", "snap-store-assertions")
PROXY_KEYS = ("apt-http-proxy", "apt-https-proxy", "apt-no-proxy", *SNAP_KEYS)
DB_NAME = DB_USER = DB_PASS = "snapproxy"  # local-only database in the container
HOST_APT_CONF = "/etc/apt/apt.conf.d/01lxd-testing-cache"

EPILOG = textwrap.dedent(
    """\
    environment variables:
      CACHE_NAME        cache container name           (default: apt-cache)
      CACHE_IMAGE       image for the container        (default: ubuntu:24.04)
      LXD_CLOUD_NAME    client-side Juju cloud name    (default: lxd)
    notes:
      'setup' configures the local cache, host and client-side LXD cloud;
      it does not change existing Juju controllers or models. Use --no-cloud
      to set up without Juju and --no-host to leave the host alone.
      'snap on' and 'snap off' change the saved snap choice and update local
      destinations configured by setup, not existing controllers or models.
      Snap support is off until explicitly enabled with 'setup --snap' or
      'snap on'; registration is interactive when needed.

      'controller apply' configures model defaults and existing models.
      'controller remove' removes tool-managed settings. Both inspect all
      targets before changing them; --exclude-model may be repeated.
      CONTROLLER defaults to JUJU_CONTROLLER or Juju's current controller.

    charmcraft:
      Charmcraft builds in its own LXD containers and ignores Juju settings.
      It forwards the host's http_proxy/no_proxy into the build container, so:

          eval "$(manage-cache.py charmcraft env)"
          charmcraft pack

      charmcraft env prints nothing if the container is stopped (disabled),
      missing or unreachable, so the eval is always safe.

      Only plain HTTP is cached (apt-cacher-ng); https_proxy is deliberately
      not set. pip and other HTTPS downloads are not cached. apt https
      repositories are fetched directly (apt https proxy = DIRECT), since
      apt would otherwise send them through the HTTP-only cache and fail.

    host:
      'setup' points this host at the caches, using sudo: apt via
      """ + HOST_APT_CONF + """, and snapd via
      'snap ack' + 'snap set system proxy.store'. Use 'host remove' and
      'cloud remove' to remove managed local settings before stopping the cache.
      snapd has no fallback; other consumers may still depend on the cache.
    """
)


def run(*cmd, check=True, capture=False, quiet=False, input=None, capture_stderr=False):
    return subprocess.run(
        cmd,
        check=check,
        text=True,
        input=input,
        stdout=subprocess.PIPE if capture else (subprocess.DEVNULL if quiet else None),
        stderr=subprocess.PIPE if capture_stderr else (
            subprocess.DEVNULL if quiet and not capture else None),
    )


def ok(*cmd):
    return run(*cmd, check=False, quiet=True).returncode == 0


def cache_ip():
    p = run("lxc", "list", NAME, "-c4", "-f", "csv", check=False, capture=True)
    out = (p.stdout or "").strip()
    return out.split(" ")[0] if out else ""


def lxd_cidrs(strict=False):
    """IPv4/IPv6 CIDRs of every LXD-managed network (empty if lxc fails)."""
    p = run("lxc", "query", "/1.0/networks?recursion=1", check=strict, capture=True)
    try:
        networks = json.loads(p.stdout)
        if not isinstance(networks, list):
            raise ValueError("expected a list of networks")
    except (ValueError, TypeError) as e:
        if strict:
            sys.exit(f"error: cannot read LXD networks: {e}")
        return []
    cidrs = []
    for n in networks:
        if not n.get("managed"):
            continue
        for key in ("ipv4.address", "ipv6.address"):
            try:
                cidr = str(ipaddress.ip_network(n["config"][key], strict=False))
            except (KeyError, ValueError):  # unset, "none" or "auto"
                continue
            if cidr not in cidrs:
                cidrs.append(cidr)
    return cidrs


def no_proxy_list(*extra, strict=False):
    """Deduplicated no-proxy entries: defaults, extras, then all LXD CIDRs."""
    entries = []
    for e in [*NO_PROXY.split(","), *extra, *lxd_cidrs(strict=strict)]:
        e = e.strip()
        if e and e not in entries:
            entries.append(e)
    return ",".join(entries)


def reachable(ip):
    # Bypass any http_proxy in the environment, which may point at the cache itself.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        opener.open(f"http://{ip}:{PORT}/acng-report.html", timeout=5).close()
    except OSError:
        return False
    return True


def lxc_exec(*cmd, **kw):
    return run("lxc", "exec", NAME, "--", *cmd, **kw)


def pg(*cmd, **kw):
    return lxc_exec("runuser", "-u", "postgres", "--", *cmd, **kw)


def snap_url(ip=None):
    return f"http://{ip or cache_ip()}"


def snap_reachable(ip):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        opener.open(f"{snap_url(ip)}/_status/check", timeout=5).close()
    except urllib.error.HTTPError:
        return True  # it answered
    except OSError:
        return False
    return True


def snap_proxy_installed():
    return ok("lxc", "exec", NAME, "--", "snap", "list", "snap-store-proxy")


def snap_assertions():
    """(store_id, assertions) from the registered proxy, or None if not registered."""
    ip = cache_ip()
    if not ip or not snap_proxy_installed():
        return None
    # The proxy serves its store assertions over HTTP once registered.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(f"{snap_url(ip)}/v2/auth/store/assertions", timeout=10) as r:
            text = r.read().decode().strip()
    except OSError:
        return None
    m = re.search(r"^store:\s*(\S+)", text, re.M)
    if not m:
        return None
    return m.group(1), text + "\n"


def snap_settings():
    """Juju config for the snap proxy, or {} if it is not usable (not registered)."""
    found = snap_assertions()
    if not found:
        return {}
    store, assertions = found
    return {"snap-store-proxy": store,
            "snap-store-proxy-url": snap_url(),
            "snap-store-assertions": assertions}


def setup_snap_proxy():
    if snap_assertions():
        return  # already set up and registered; leave a working proxy alone
    # Safe on an existing apt-cache container: only adds packages, never touches
    # apt-cacher-ng or its cache. Every step is idempotent so an interrupted
    # setup can simply be re-run.
    if not ok("lxc", "exec", NAME, "--", "dpkg", "-s", "postgresql"):
        lxc_exec("apt-get", "update", quiet=True)
        lxc_exec("env", "DEBIAN_FRONTEND=noninteractive", "apt-get", "install", "-y",
                 "postgresql", quiet=True)
    role = textwrap.dedent(
        f"""\
        DO $$ BEGIN
          IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = '{DB_USER}') THEN
            CREATE ROLE {DB_USER} LOGIN CREATEROLE PASSWORD '{DB_PASS}';
          ELSE
            ALTER ROLE {DB_USER} LOGIN CREATEROLE PASSWORD '{DB_PASS}';
          END IF;
        END $$;
        """
    )
    pg("psql", "-c", role, quiet=True)
    exists = pg("psql", "-tAc", f"SELECT 1 FROM pg_database WHERE datname='{DB_NAME}'",
                capture=True).stdout or ""
    if not exists.strip():
        pg("createdb", "-O", DB_USER, DB_NAME)
    for ext in ("pg_trgm", "btree_gist"):
        pg("psql", "-d", DB_NAME, "-c", f"CREATE EXTENSION IF NOT EXISTS {ext}", quiet=True)
    if not snap_proxy_installed():
        lxc_exec("snap", "install", "snap-store-proxy")
    # proxy.domain is the address registered with the store and handed to
    # clients, so it must be the container's (pinned) IP, not 'localhost'.
    lxc_exec("snap-store-proxy", "config", f"proxy.domain={cache_ip()}",
             f"proxy.db.connection=postgresql://{DB_USER}:{DB_PASS}@localhost:5432/{DB_NAME}")


def proxy_url(ip=None):
    ip = ip or cache_ip()
    if not ip:
        sys.exit(f"error: no IPv4 address for container {NAME!r}; run 'setup' first")
    return f"http://{ip}:{PORT}"


def create_container():
    if ok("lxc", "info", NAME):
        run("lxc", "config", "set", NAME, "boot.autostart=true")
        run("lxc", "start", NAME, check=False, quiet=True)
    else:
        run("lxc", "launch", IMAGE, NAME, "-c", "boot.autostart=true")
    run("lxc", "exec", NAME, "--", "cloud-init", "status", "--wait", check=False, quiet=True)
    if not ok("lxc", "exec", NAME, "--", "dpkg", "-s", "apt-cacher-ng"):
        run("lxc", "exec", NAME, "--", "apt-get", "update", quiet=True)
        run(
            "lxc", "exec", NAME, "--", "env", "DEBIAN_FRONTEND=noninteractive",
            "apt-get", "install", "-y", "apt-cacher-ng", quiet=True,
        )
    run("lxc", "exec", NAME, "--", "systemctl", "enable", "--now", "apt-cacher-ng", quiet=True)
    ip = ""
    for _ in range(30):
        ip = cache_ip()
        if ip:
            break
        time.sleep(1)
    if not ip:
        sys.exit(f"error: no IPv4 address for {NAME}")
    # Pin the address so Juju defaults stay valid across restarts.
    p = run("lxc", "config", "device", "get", NAME, "eth0", "ipv4.address",
            check=False, capture=True)
    if not (p.stdout or "").strip():
        pin = f"ipv4.address={ip}"
        if not ok("lxc", "config", "device", "override", NAME, "eth0", pin):
            run("lxc", "config", "device", "set", NAME, "eth0", "ipv4.address", ip)
        run("lxc", "restart", NAME)
        time.sleep(3)


def local_state_path():
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(base, "lxd-testing-cache", "state.json")


def load_local_state():
    try:
        with open(local_state_path()) as f:
            data = json.load(f)
    except FileNotFoundError:
        return {"version": 1, "caches": {}}
    except (OSError, ValueError) as e:
        sys.exit(f"error: cannot read local state: {e}")
    if not isinstance(data, dict) or data.get("version") != 1 or not isinstance(
            data.get("caches"), dict):
        sys.exit("error: invalid local state; refusing to change configuration")
    return data


def save_local_state(state):
    path = local_state_path()
    directory = os.path.dirname(path)
    os.makedirs(directory, mode=0o700, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", dir=directory, delete=False) as f:
        name = f.name
        try:
            json.dump(state, f, indent=2, sort_keys=True)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
            os.replace(name, path)
        finally:
            if os.path.exists(name):
                os.unlink(name)


def local_cache_state(state):
    return state["caches"].setdefault(NAME, {})


def strict_host_snap_store():
    p = run("sudo", "snap", "get", "-d", "system", capture=True)
    try:
        config = json.loads(p.stdout)
        proxy = config.get("proxy", {})
        if not isinstance(proxy, dict) or not isinstance(proxy.get("store", ""), str):
            raise ValueError("invalid proxy.store")
        return proxy.get("store", "")
    except (ValueError, AttributeError) as e:
        sys.exit(f"error: cannot read host snap configuration: {e}")


def check_local_value(target, key, current, desired, managed):
    if current not in (None, "", desired, managed.get(key)):
        sys.exit(f"error: {target} {key} has an unmanaged value; "
                 "refusing to overwrite it")


def prepare_local_host(url, snap, managed):
    apt = f'Acquire::http::Proxy "{url}";\nAcquire::https::Proxy "{HTTPS_DIRECT}";\n'
    store = snap.get("snap-store-proxy", "")
    current_apt = host_apt_proxy()
    current_store = strict_host_snap_store()
    check_local_value("host", "apt", current_apt, apt, managed)
    check_local_value("host", "snap store", current_store, store, managed)
    return apt, current_apt, store, current_store


def apply_local_host(plan, snap, managed, state):
    apt, current_apt, store, current_store = plan
    if current_apt != apt:
        run("sudo", "tee", HOST_APT_CONF, input=apt, quiet=True)
        print("host: configured apt proxy")
    managed["apt"] = apt
    local_cache_state(state)["host_configured"] = True
    save_local_state(state)
    if store and current_store != store:
        with tempfile.NamedTemporaryFile("w", suffix=".assert") as f:
            f.write(snap["snap-store-assertions"])
            f.flush()
            run("sudo", "snap", "ack", f.name)
        run("sudo", "snap", "set", "system", f"proxy.store={store}")
        print("host: configured snap store proxy")
    elif not store and current_store:
        run("sudo", "snap", "unset", "system", "proxy.store")
        print("host: removed snap store proxy")
    if store:
        managed["snap store"] = store
    else:
        managed.pop("snap store", None)
    save_local_state(state)


def prepare_local_cloud(url, snap, managed, remove=False, force=False):
    p = run("juju", "clouds", "--client", "--all", "--format", "json", capture=True)
    try:
        clouds = json.loads(p.stdout)
        if not isinstance(clouds, dict):
            raise ValueError("expected an object")
        existing = clouds.get(LXD_CLOUD)
        if existing is not None and not isinstance(existing, dict):
            raise ValueError("invalid cloud definition")
    except ValueError as e:
        sys.exit(f"error: cannot read client clouds: {e}")
    if existing is None:
        if remove:
            sys.exit(f"error: client cloud {LXD_CLOUD!r} no longer exists")
        cloud = {"type": "lxd", "auth-types": ["certificate"],
                 "regions": {"localhost": {}}}
    else:
        if existing.get("type") != "lxd":
            sys.exit(f"error: client cloud {LXD_CLOUD!r} is not an LXD cloud")
        # Juju's JSON output sorts region names, losing the default-region order.
        if len(existing.get("regions", {})) > 1:
            sys.exit(f"error: cannot safely update multi-region cloud {LXD_CLOUD!r}; "
                     "Juju's JSON output does not preserve region order")
        # Juju's cloud listing contains display-only fields. Refuse unknown
        # fields rather than risk discarding part of a cloud definition.
        fields = {"type", "description", "auth-types", "endpoint",
                  "identity-endpoint", "storage-endpoint", "regions", "config",
                  "region-config", "ca-credentials", "skip-tls-verify"}
        display = {"defined", "credential-count", "default-region"}
        unknown = set(existing) - fields - display
        if unknown:
            sys.exit(f"error: cannot safely update cloud {LXD_CLOUD!r}; "
                     f"unrecognized fields: {', '.join(sorted(unknown))}")
        cloud = {k: v for k, v in existing.items() if k in fields}
        if "ca-credentials" in cloud:
            cloud["ca-certificates"] = cloud.pop("ca-credentials")
    config = cloud.setdefault("config", {})
    if not isinstance(config, dict):
        sys.exit(f"error: invalid config for cloud {LXD_CLOUD!r}")
    if remove:
        for key, previous in managed.items():
            if config.get(key) not in (None, "", previous) and not force:
                sys.exit(f"error: cloud {LXD_CLOUD!r} {key} has changed; "
                         "use --force to remove it")
            config.pop(key, None)
        return True, cloud, {}
    desired = {"apt-http-proxy": url, "apt-https-proxy": HTTPS_DIRECT,
               "apt-no-proxy": no_proxy_list(strict=True), **snap}
    regions = cloud.get("region-config", {})
    if not isinstance(regions, dict) or any(not isinstance(v, dict) for v in regions.values()):
        sys.exit(f"error: invalid region config for cloud {LXD_CLOUD!r}")
    for region, values in regions.items():
        for key in ("apt-http-proxy", "apt-https-proxy", "apt-no-proxy", *SNAP_KEYS):
            if values.get(key) not in (None, "", desired.get(key)):
                sys.exit(f"error: cloud {LXD_CLOUD!r} region {region!r} has a "
                         f"conflicting {key}; refusing to update the cloud")
    for key in ("apt-http-proxy", "apt-https-proxy", "apt-no-proxy", *SNAP_KEYS):
        current = config.get(key)
        value = desired.get(key)
        check_local_value(f"cloud {LXD_CLOUD!r}", key, current, value, managed)
        if value is None:
            config.pop(key, None)
        else:
            config[key] = value
    return existing is not None, cloud, desired


def apply_local_cloud(plan, managed, state, remove=False):
    exists, cloud, desired = plan
    with tempfile.NamedTemporaryFile(
        "w", dir=os.path.expanduser("~"), prefix="lxd-cloud-", suffix=".yaml"
    ) as f:
        json.dump({"clouds": {LXD_CLOUD: cloud}}, f)
        f.flush()
        run("juju", "update-cloud" if exists else "add-cloud", LXD_CLOUD,
            "--client", "-f", f.name)
    managed.clear()
    managed.update(desired)
    clouds = local_cache_state(state).setdefault("configured_clouds", {})
    if remove:
        clouds.pop(LXD_CLOUD, None)
    else:
        clouds[LXD_CLOUD] = True
    save_local_state(state)
    print(f"cloud: {'removed proxy settings from' if remove else 'configured'} "
          f"client-side {LXD_CLOUD}")


def registered_snap_settings():
    setup_snap_proxy()
    snap = snap_settings()
    if not snap:
        run("lxc", "exec", NAME, "--", "snap-store-proxy", "register")
        snap = snap_settings()
    if not snap:
        sys.exit("error: snap proxy registration did not produce usable assertions")
    return snap


def configure_local(state, cache, snap, host, cloud):
    url = proxy_url()
    host_managed = cache.setdefault("host", {})
    cloud_managed = cache.setdefault("clouds", {}).setdefault(LXD_CLOUD, {})
    host_plan = prepare_local_host(url, snap, host_managed) if host else None
    cloud_plan = prepare_local_cloud(url, snap, cloud_managed) if cloud else None
    if host_plan:
        apply_local_host(host_plan, snap, host_managed, state)
    if cloud_plan:
        apply_local_cloud(cloud_plan, cloud_managed, state)


def cmd_setup(a):
    state = load_local_state()
    cache = local_cache_state(state)
    create_container()
    enabled = a.snap or cache.get("snap_enabled", False)
    snap = registered_snap_settings() if enabled else {}
    cache["snap_enabled"] = enabled
    save_local_state(state)
    configure_local(state, cache, snap, not a.no_host, not a.no_cloud)
    print(f"local cache ready; snap proxy {'on' if enabled else 'off'}")


def cmd_snap(a):
    state = load_local_state()
    cache = local_cache_state(state)
    enabled = a.snap_action == "on"
    proxy_url()
    snap = registered_snap_settings() if enabled else {}
    cache["snap_enabled"] = enabled
    save_local_state(state)
    configure_local(state, cache, snap, cache.get("host_configured", False),
                    cache.get("configured_clouds", {}).get(LXD_CLOUD, False))
    print(f"snap proxy {'on' if enabled else 'off'}; "
          "existing Juju controllers and models were not changed")
    if not enabled:
        print("warning: existing controllers or models may still use the snap proxy; "
              "the proxy service remains available", file=sys.stderr)


@dataclass
class JujuTarget:
    identity: str
    label: str
    command: list
    values: dict


def juju_json(*args, strict_stderr=False):
    p = run("juju", *args, "--format", "json", capture=True,
            capture_stderr=strict_stderr, check=not strict_stderr)
    if strict_stderr and p.returncode:
        raise ValueError(f"juju {' '.join(args)} failed: {p.stderr.strip()}")
    if strict_stderr and p.stderr and p.stderr.strip():
        raise ValueError(f"juju {' '.join(args)} reported: {p.stderr.strip()}")
    try:
        return json.loads(p.stdout)
    except (TypeError, ValueError) as e:
        raise ValueError(f"invalid JSON from juju {' '.join(args)}: {e}") from e


def resolve_controller(name):
    data = juju_json("controllers")
    if not isinstance(data, dict) or not isinstance(data.get("controllers"), dict):
        raise ValueError("invalid controller list from Juju")
    name = name or os.environ.get("JUJU_CONTROLLER") or data.get("current-controller")
    info = data["controllers"].get(name)
    if not name or not isinstance(info, dict) or not info.get("uuid"):
        raise ValueError(f"cannot identify controller {name or '(current)'}")
    return name, info["uuid"]


def controller_models(controller):
    data = juju_json("models", "-c", controller, "--all", strict_stderr=True)
    if not isinstance(data, dict) or not isinstance(data.get("models"), list):
        raise ValueError("invalid model list from Juju")
    models = data["models"]
    if any(not isinstance(m, dict) or not m.get("model-uuid") or not m.get("name")
           for m in models):
        raise ValueError("model listing is incomplete")
    return models


def select_models(models, excluded):
    skip = set()
    for selector in excluded:
        matches = [m for m in models if selector in
                   (m["name"], m.get("short-name"), m["model-uuid"])]
        if len(matches) != 1:
            raise ValueError(f"excluded model {selector!r} matches {len(matches)} models; "
                             "use a fully qualified name or UUID")
        skip.add(matches[0]["model-uuid"])
    return [m for m in models if m["model-uuid"] not in skip]


def model_target(controller, model_uuid, label):
    command = ["model-config", "-m", f"{controller}:{model_uuid}"]
    data = juju_json(*command)
    if not isinstance(data, dict):
        raise ValueError(f"{label}: invalid model configuration")
    values = {}
    for key in PROXY_KEYS:
        entry = data.get(key)
        if entry is None:
            values[key] = (None, "")
            continue
        if not isinstance(entry, dict):
            raise ValueError(f"{label}: invalid metadata for {key}")
        value = entry.get("Value", entry.get("value"))
        source = entry.get("Source", entry.get("source"))
        if source not in ("default", "controller", "region", "model"):
            raise ValueError(f"{label}: unknown source for {key}: {source!r}")
        if value is not None and not isinstance(value, str):
            raise ValueError(f"{label}: invalid value for {key}")
        values[key] = (value, source)
    return JujuTarget(f"model:{model_uuid}", label, command, values)


def defaults_targets(controller, controller_uuid, cloud):
    command = ["model-defaults", "-c", controller, "--cloud", cloud]
    data = juju_json(*command)
    if not isinstance(data, dict):
        raise ValueError(f"cloud {cloud}: invalid model defaults")
    values = {}
    region_values = {}
    for key in PROXY_KEYS:
        entry = data.get(key, {})
        if not isinstance(entry, dict):
            raise ValueError(f"cloud {cloud}: invalid default for {key}")
        base = entry.get("controller")
        if base is None:
            base, source = entry.get("default"), "default"
        else:
            source = "controller"
        if base is not None and not isinstance(base, str):
            raise ValueError(f"cloud {cloud}: invalid value for {key}")
        values[key] = (base, source)
        regions = entry.get("regions", [])
        if not isinstance(regions, list):
            raise ValueError(f"cloud {cloud}: invalid regions for {key}")
        for region in regions:
            if (not isinstance(region, dict) or not isinstance(region.get("name"), str)
                    or not isinstance(region.get("value"), str)):
                raise ValueError(f"cloud {cloud}: invalid region default for {key}")
            region_values.setdefault(region["name"], {})[key] = (region["value"], "region")
    targets = [JujuTarget(f"defaults:{controller_uuid}:{cloud}",
                          f"controller {controller}, cloud {cloud}", command, values)]
    for region, values in sorted(region_values.items()):
        targets.append(JujuTarget(
            f"defaults:{controller_uuid}:{cloud}:{region}",
            f"controller {controller}, cloud {cloud}, region {region}",
            [*command, "--region", region], values))
    return targets


def proxy_settings(cache):
    snap = snap_settings() if cache.get("snap_enabled", False) else {}
    if cache.get("snap_enabled", False) and not snap:
        raise ValueError("snap proxy is enabled but unavailable or unregistered; "
                         "run 'setup' first")
    return {"apt-http-proxy": proxy_url(), "apt-https-proxy": HTTPS_DIRECT,
            "apt-no-proxy": no_proxy_list(strict=True), **snap}


def display_proxy_value(key, value):
    if key == "snap-store-assertions" and value:
        return "<assertions>"
    return repr(value)


def plan_juju_changes(targets, managed, desired, remove, force):
    plans = []
    conflicts = []
    for target in targets:
        previous = managed.get(target.identity, {})
        if not isinstance(previous, dict):
            raise ValueError(f"invalid saved state for {target.label}")
        changes = []
        keys = target.values if not remove else previous
        for key in keys:
            if key not in PROXY_KEYS:
                raise ValueError(f"invalid saved key for {target.label}: {key}")
            current, source = target.values.get(key, (None, ""))
            if remove:
                if current not in (None, "", previous[key]):
                    conflicts.append((target.label, key, current, "remove"))
                if source in ("model", "controller", "region"):
                    changes.append((key, None))
                else:
                    changes.append((key, None, "forget"))
                continue
            value = desired.get(key, "")
            if current not in (None, "", value, previous.get(key)):
                conflicts.append((target.label, key, current, value))
            # Do not introduce empty overrides where none exist.
            if value == "" and current in (None, "") and key not in previous:
                continue
            changes.append((key, value))
        plans.append((target, changes))
    for label, key, current, desired_value in conflicts:
        print(f"conflict: {label}: {key} is {display_proxy_value(key, current)}; "
              f"requested {display_proxy_value(key, desired_value)}", file=sys.stderr)
    if conflicts and not force:
        raise ValueError("preflight found conflicts; use --force to override them")
    if conflicts:
        print("warning: --force permits overriding the conflicts listed above", file=sys.stderr)
    return plans


def execute_juju_changes(plans, managed, state, remove=False):
    completed = []
    for target, changes in plans:
        entry = managed.setdefault(target.identity, {})
        for change in changes:
            key, value = change[:2]
            try:
                if len(change) == 2:
                    args = ["--reset", key] if value is None else [f"{key}={value}"]
                    run("juju", *target.command, *args)
                if value is None:
                    entry.pop(key, None)
                else:
                    entry[key] = value
                save_local_state(state)
            except (subprocess.CalledProcessError, OSError) as e:
                detail = (f"Juju exited with status {e.returncode}"
                          if isinstance(e, subprocess.CalledProcessError) else str(e))
                print(f"error: failed while updating {target.label}, {key}: {detail}",
                      file=sys.stderr)
                print(f"completed targets: {', '.join(completed) or 'none'}; "
                      f"{target.label} may be partially updated", file=sys.stderr)
                sys.exit("error: Juju operation incomplete; inspect the target and rerun")
        if not entry:
            managed.pop(target.identity, None)
            save_local_state(state)
        completed.append(target.label)
        print(f"{'removed managed settings from' if remove else 'configured'}: {target.label}")


def cmd_controller(a):
    state = load_local_state()
    cache = local_cache_state(state)
    managed = cache.setdefault("juju_targets", {})
    try:
        if a.defaults_only and a.exclude_model:
            raise ValueError("--exclude-model cannot be combined with --defaults-only")
        controller, controller_uuid = resolve_controller(a.controller)
        desired = {} if a.action == "remove" else proxy_settings(cache)
        clouds = juju_json("clouds", "-c", controller, "--all")
        if not isinstance(clouds, dict) or not clouds:
            raise ValueError(f"no clouds available on controller {controller}")
        targets = []
        errors = []
        for cloud in sorted(clouds):
            try:
                targets.extend(defaults_targets(controller, controller_uuid, cloud))
            except (ValueError, subprocess.CalledProcessError) as e:
                errors.append(f"cloud {cloud}: {e}")
        if not a.defaults_only:
            models = select_models(controller_models(controller), a.exclude_model)
            for model in models:
                label = f"model {controller}:{model['name']}"
                try:
                    targets.append(model_target(controller, model["model-uuid"], label))
                except (ValueError, subprocess.CalledProcessError) as e:
                    errors.append(f"{label}: {e}")
        try:
            plans = plan_juju_changes(targets, managed, desired, a.action == "remove", a.force)
        except ValueError as e:
            errors.append(str(e))
        if errors:
            raise ValueError("preflight failed:\n  " + "\n  ".join(errors))
        execute_juju_changes(plans, managed, state, remove=a.action == "remove")
    except ValueError as e:
        sys.exit(f"error: {e}")


def cmd_model(a):
    state = load_local_state()
    cache = local_cache_state(state)
    managed = cache.setdefault("juju_targets", {})
    try:
        if ":" not in a.model:
            raise ValueError("model must be specified as CONTROLLER:MODEL")
        controller, name = a.model.split(":", 1)
        controller, _ = resolve_controller(controller)
        data = juju_json("show-model", f"{controller}:{name}")
        if not isinstance(data, dict) or len(data) != 1:
            raise ValueError(f"cannot identify model {a.model}")
        info = next(iter(data.values()))
        if not isinstance(info, dict) or not info.get("model-uuid"):
            raise ValueError(f"cannot identify model {a.model}")
        target = model_target(controller, info["model-uuid"], f"model {a.model}")
        desired = {} if a.action == "remove" else proxy_settings(cache)
        plans = plan_juju_changes([target], managed, desired, a.action == "remove", a.force)
        execute_juju_changes(plans, managed, state, remove=a.action == "remove")
        if a.action == "remove":
            print("model settings now inherit any controller or region defaults")
    except ValueError as e:
        sys.exit(f"error: {e}")


def controller_args(a):
    """`-c NAME` if given; otherwise juju uses its current controller."""
    return ["-c", a.controller] if a.controller else []


def host_apt_proxy():
    try:
        with open(HOST_APT_CONF) as f:
            return f.read()
    except FileNotFoundError:
        return None


def cmd_register_snap_proxy(a):
    if not snap_proxy_installed():
        sys.exit("error: snap-store-proxy is not installed; run 'snap on' first")
    run("lxc", "exec", NAME, "--", "snap-store-proxy", "register")
    if not snap_settings():
        sys.exit("error: snap proxy registration did not produce usable assertions")
    print("registered; run 'snap on' to update local settings")


def cmd_cache_lifecycle(a):
    print("warning: host, cloud, controllers or models may still depend on this cache; "
          "this command does not remove their settings", file=sys.stderr)
    if not a.force:
        sys.exit(f"error: use 'cache {a.action} --force' to acknowledge this risk")
    if a.action == "stop":
        p = run("lxc", "list", NAME, "--format", "json", capture=True)
        try:
            instances = json.loads(p.stdout)
            instance = next(i for i in instances if i["name"] == NAME)
            status = instance["status"]
        except (ValueError, KeyError, TypeError, StopIteration):
            sys.exit(f"error: cannot find cache container {NAME!r}")
        run("lxc", "config", "set", NAME, "boot.autostart=false")
        if status != "Stopped":
            run("lxc", "stop", NAME)
        print(f"cache {NAME} stopped")
    else:
        run("lxc", "delete", "-f", NAME)
        print(f"cache {NAME} destroyed")


def cmd_host_remove(a):
    state = load_local_state()
    cache = local_cache_state(state)
    managed = cache.get("host", {})
    apt = host_apt_proxy() if "apt" in managed else None
    store = strict_host_snap_store() if "snap store" in managed else ""
    for key, current in (("apt", apt), ("snap store", store)):
        if key in managed and current not in (None, "", managed[key]) and not a.force:
            sys.exit(f"error: host {key} has changed; use --force to remove it")
    if apt is not None:
        run("sudo", "rm", HOST_APT_CONF)
        print("host: removed apt proxy")
    managed.pop("apt", None)
    save_local_state(state)
    if store:
        run("sudo", "snap", "unset", "system", "proxy.store")
        print("host: removed snap store proxy")
    managed.pop("snap store", None)
    cache["host_configured"] = False
    save_local_state(state)


def cmd_cloud_remove(a):
    state = load_local_state()
    cache = local_cache_state(state)
    managed = cache.get("clouds", {}).get(LXD_CLOUD, {})
    if not managed:
        print(f"cloud: no managed proxy settings recorded for {LXD_CLOUD}")
        return
    plan = prepare_local_cloud(None, {}, managed, remove=True, force=a.force)
    apply_local_cloud(plan, managed, state, remove=True)


def _os_upgrade(a, value):
    run("juju", "model-defaults", *controller_args(a),
        f"enable-os-upgrade={value}", f"enable-os-refresh-update={value}")


def cmd_skip_os_upgrade(a):
    _os_upgrade(a, "false")


def cmd_restore_os_upgrade(a):
    _os_upgrade(a, "true")


def cmd_refresh_images(a):
    # Images with no recorded source are pinned and cannot be refreshed; re-import
    # them with: lxc image copy ubuntu:<version> local: [--vm] --auto-update
    p = run("lxc", "image", "list", "-c", "lf", "--format", "csv", capture=True)
    for line in p.stdout.splitlines():
        alias, _, fp = line.partition(",")
        if not alias.startswith("juju/"):
            continue
        info = run("lxc", "image", "info", fp, capture=True).stdout or ""
        if not any(l.startswith("    Server:") for l in info.splitlines()):
            print(f"WARNING: image {fp} has no update source (pinned)", file=sys.stderr)
        run("lxc", "image", "refresh", fp)


def cmd_status(a):
    cache = local_cache_state(load_local_state())
    print(f"saved snap proxy choice: {'on' if cache.get('snap_enabled') else 'off'}")
    print(f"host managed by setup: {'yes' if cache.get('host_configured') else 'no'}")
    clouds = cache.get("configured_clouds", {})
    print(f"client clouds managed by setup: {', '.join(sorted(clouds)) or 'none'}")
    run("lxc", "list", NAME, "-c", "ns4", check=False)
    if shutil.which("juju"):
        p = run("juju", "model-defaults", *controller_args(a), check=False, capture=True)
        if p.returncode:
            print("warning: could not read controller defaults", file=sys.stderr)
        for line in (p.stdout or "").splitlines():
            if line.startswith(("apt-http-proxy", "apt-https-proxy", "apt-no-proxy", "enable-os-",
                                "snap-store-proxy")):
                print(line)
    else:
        print("Juju not installed; controller defaults not shown", file=sys.stderr)
    ip = cache_ip()
    if ip and reachable(ip):
        print(f"cache reachable at {ip}:{PORT}")
    if ip and snap_proxy_installed():
        state = "registered" if snap_assertions() else "NOT registered"
        up = "reachable" if snap_reachable(ip) else "unreachable"
        print(f"snap store proxy {up} at {snap_url(ip)} ({state})")
    print(f"host apt proxy: {'set' if host_apt_proxy() is not None else 'not set'}")
    try:
        store = strict_host_snap_store()
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        print(f"host snap proxy.store: unknown ({e})", file=sys.stderr)
    else:
        print(f"host snap proxy.store: {store or 'not set'}")


def cmd_charmcraft_env(a):
    try:
        charmcraft_env(a)
    except OSError as e:
        print(f"cannot determine charmcraft cache settings: {e}; not using a cache",
              file=sys.stderr)


def charmcraft_env(a):
    if a.unset:
        print("unset http_proxy HTTP_PROXY no_proxy NO_PROXY")
        return
    # stdout must stay empty unless the cache is usable, so it is safe to eval.
    ip = cache_ip()
    if not ip:
        print(f"apt cache container {NAME!r} is missing or stopped (run 'setup'); "
              "not using a cache", file=sys.stderr)
        return
    url = f"http://{ip}:{PORT}"
    current = os.environ.get("http_proxy") or os.environ.get("HTTP_PROXY")
    if current == url:
        print(f"http_proxy is already set to the apt cache {url}; nothing to do",
              file=sys.stderr)
        return
    if current:
        print(f"http_proxy is already set to {current}; not overriding it "
              "with the apt cache", file=sys.stderr)
        return
    if not reachable(ip):
        print(f"apt cache at {url} is unreachable; not using a cache", file=sys.stderr)
        return
    # Keep the user's existing exclusions and never proxy the cache itself, the
    # LXD bridge subnet (CIDR; not every tool supports it) or container names.
    existing = os.environ.get("no_proxy") or os.environ.get("NO_PROXY") or ""
    no_proxy = no_proxy_list(*existing.split(","), "::1", ip, ".lxd")
    for k, v in (("http_proxy", url), ("HTTP_PROXY", url),
                 ("no_proxy", no_proxy), ("NO_PROXY", no_proxy)):
        print(f"export {k}={v}")


def build_parser():
    p = argparse.ArgumentParser(
        prog="manage-cache.py",
        description=__doc__,
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = p.add_subparsers(dest="command", metavar="COMMAND", required=True)

    def add(name, func, help_, controller=False):
        sp = sub.add_parser(name, help=help_, description=help_,
                            formatter_class=argparse.RawDescriptionHelpFormatter)
        sp.set_defaults(func=func)
        if controller:
            sp.add_argument("controller", nargs="?", default=None,
                            metavar="CONTROLLER",
                            help="Juju controller (default: the current controller)")
        return sp

    sp = add("setup", cmd_setup, "set up the local cache, host and client-side LXD cloud")
    sp.add_argument("--snap", action="store_true",
                    help="enable and register the snap proxy if needed")
    sp.add_argument("--no-host", action="store_true",
                    help="do not configure this host's apt and snapd")
    sp.add_argument("--no-cloud", action="store_true",
                    help="do not configure the Juju client cloud")
    add("status", cmd_status, "show cache, local settings and Juju defaults", controller=True)

    def group(name, description, dest="action"):
        parser = sub.add_parser(name, help=description, description=description)
        return parser.add_subparsers(dest=dest, metavar="ACTION", required=True)

    def action(group_parser, name, func, help_):
        sp = group_parser.add_parser(name, help=help_, description=help_)
        sp.set_defaults(func=func)
        return sp

    snaps = group("snap", "manage snap proxy use and registration", dest="snap_action")
    action(snaps, "on", cmd_snap, "enable snap proxy use locally")
    action(snaps, "off", cmd_snap, "disable snap proxy use locally")
    action(snaps, "register", cmd_register_snap_proxy, "register the installed proxy")

    controllers = group("controller", "manage controller defaults and existing models")
    for verb in ("apply", "remove"):
        sp = action(controllers, verb, cmd_controller, f"{verb} cache settings on a controller")
        sp.add_argument("controller", nargs="?", help="controller (default: current controller)")
        sp.add_argument("--exclude-model", action="append", default=[], metavar="MODEL",
                        help="exclude a model by name or UUID; may be repeated")
        sp.add_argument("--defaults-only", action="store_true",
                        help="change defaults without updating existing models")
        sp.add_argument("--force", action="store_true", help="override conflicting settings")

    models = group("model", "manage cache settings for one model")
    for verb in ("apply", "remove"):
        sp = action(models, verb, cmd_model, f"{verb} cache settings on a model")
        sp.add_argument("model", metavar="CONTROLLER:MODEL", help="model to configure")
        sp.add_argument("--force", action="store_true", help="override conflicting settings")

    caches = group("cache", "manage the cache container")
    for verb in ("stop", "destroy"):
        sp = action(caches, verb, cmd_cache_lifecycle, f"{verb} the cache container")
        sp.add_argument("--force", action="store_true",
                        help="acknowledge that clients may still depend on the cache")

    for name, func in (("host", cmd_host_remove), ("cloud", cmd_cloud_remove)):
        actions = group(name, f"manage {name} proxy settings")
        sp = action(actions, "remove", func, f"remove managed {name} settings")
        sp.add_argument("--force", action="store_true", help="remove changed managed settings")

    upgrades = group("upgrade", "manage OS upgrade defaults on a controller")
    for verb, func in (("skip", cmd_skip_os_upgrade), ("restore", cmd_restore_os_upgrade)):
        sp = action(upgrades, verb, func, f"{verb} OS upgrades for new Juju machines")
        sp.add_argument("controller", nargs="?", help="controller (default: current controller)")

    images = group("images", "manage cached Juju LXD images")
    action(images, "refresh", cmd_refresh_images, "refresh cached Juju LXD images")

    charmcraft = group("charmcraft", "manage the charmcraft shell environment")
    sp = action(charmcraft, "env", cmd_charmcraft_env,
                "print shell exports; use eval \"$(manage-cache.py charmcraft env)\"")
    sp.add_argument("--unset", action="store_true", help="remove the proxy variables instead")
    return p


def main():
    args = build_parser().parse_args()
    try:
        args.func(args)
    except subprocess.CalledProcessError as e:
        sys.exit(f"error: command failed ({e.returncode}): {' '.join(e.cmd)}")


if __name__ == "__main__":
    main()
