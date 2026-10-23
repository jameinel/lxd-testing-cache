#!/usr/bin/env python3
"""Local apt cache and snap store proxy (in an LXD container) for faster Juju/LXD tests."""

import argparse
import ipaddress
import json
import os
import re
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
SNAP_KEYS = ("snap-store-proxy", "snap-store-proxy-url", "snap-store-assertions")
DB_NAME = DB_USER = DB_PASS = "snapproxy"  # local-only database in the container

EPILOG = textwrap.dedent(
    """\
    environment variables:
      CACHE_NAME        cache container name           (default: apt-cache)
      CACHE_IMAGE       image for the container        (default: ubuntu:24.04)
      LXD_CLOUD_NAME    client-side Juju cloud name    (default: lxd)
    notes:
      Commands taking CONTROLLER default to Juju's current controller (which
      juju itself picks from JUJU_CONTROLLER or 'juju switch').
      Juju model-defaults only affect models created afterwards; use 'apply'
      for existing models.

    charmcraft:
      Charmcraft builds in its own LXD containers and ignores Juju settings.
      It forwards the host's http_proxy/no_proxy into the build container, so:

          eval "$(manage-cache.py charmcraft-env)"
          charmcraft pack

      charmcraft-env prints nothing if the container is stopped (disabled),
      missing or unreachable, so the eval is always safe.

      Only plain HTTP is cached (apt-cacher-ng); https_proxy is deliberately
      not set. pip and other HTTPS downloads are not cached.

    snap store proxy:
      'enable' also installs Canonical's snap-store-proxy (with PostgreSQL) in
      the same container, listening on port 80 inside it; pass --no-snap to
      skip it. If the container already exists (even stopped), it is reused
      and the existing apt cache is preserved; the snap proxy is just added. It must be registered once with an Ubuntu One account:

          manage-cache.py register-snap-proxy

      After that, 'enable' (or 'apply') sets Juju's snap-store-proxy,
      snap-store-proxy-url and snap-store-assertions so new machines fetch
      snaps through the proxy. Until it is registered only apt is configured.
    """
)


def run(*cmd, check=True, capture=False, quiet=False):
    return subprocess.run(
        cmd,
        check=check,
        text=True,
        stdout=subprocess.PIPE if capture else (subprocess.DEVNULL if quiet else None),
        stderr=subprocess.DEVNULL if quiet and not capture else None,
    )


def ok(*cmd):
    return run(*cmd, check=False, quiet=True).returncode == 0


def cache_ip():
    p = run("lxc", "list", NAME, "-c4", "-f", "csv", check=False, capture=True)
    out = (p.stdout or "").strip()
    return out.split(" ")[0] if out else ""


def lxd_cidrs():
    """IPv4/IPv6 CIDRs of every LXD-managed network (empty if lxc fails)."""
    p = run("lxc", "query", "/1.0/networks?recursion=1", check=False, capture=True)
    try:
        networks = json.loads(p.stdout)
    except (ValueError, TypeError):
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


def no_proxy_list(*extra):
    """Deduplicated no-proxy entries: defaults, extras, then all LXD CIDRs."""
    entries = []
    for e in [*NO_PROXY.split(","), *extra, *lxd_cidrs()]:
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
        sys.exit(f"error: no IPv4 address for container {NAME!r}; run 'enable' first")
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


def define_cloud(url, snap):
    """Define a client-side cloud equal to localhost LXD plus the proxies."""
    config = {"apt-http-proxy": url, "apt-no-proxy": no_proxy_list(), **snap}
    # JSON is valid YAML and handles the multi-line assertions safely.
    body = textwrap.dedent(
        f"""\
        clouds:
          {LXD_CLOUD}:
            type: lxd
            auth-types: [certificate]
            regions:
              localhost: {{}}
            config: {json.dumps(config)}
        """
    )
    # snap-confined juju cannot read /tmp
    with tempfile.NamedTemporaryFile(
        "w", dir=os.path.expanduser("~"), prefix="lxd-cloud-", suffix=".yaml"
    ) as f:
        f.write(body)
        f.flush()
        p = run("juju", "clouds", "--client", "--all", "--format", "json", capture=True)
        verb = "update-cloud" if f'"{LXD_CLOUD}":' in p.stdout else "add-cloud"
        run("juju", verb, LXD_CLOUD, "--client", "-f", f.name)


def controller_args(a):
    """`-c NAME` if given; otherwise juju uses its current controller."""
    return ["-c", a.controller] if a.controller else []


def controller_label(a):
    return a.controller or "the current controller"


def snap_args(a):
    if a.no_snap:
        return {}
    return snap_settings()


def cmd_enable(a):
    create_container()
    if not a.no_snap:
        setup_snap_proxy()
    url = proxy_url()
    snap = snap_args(a)
    run("juju", "model-defaults", *controller_args(a),
        f"apt-http-proxy={url}", f"apt-no-proxy={no_proxy_list()}",
        *[f"{k}={v}" for k, v in snap.items()])
    define_cloud(url, snap)
    print(f"enabled: apt proxy {url} for new models on {controller_label(a)}")
    if snap:
        print(f"enabled: snap store proxy {snap_url()}")
    elif not a.no_snap:
        print("snap store proxy is not registered yet; run 'register-snap-proxy' "
              "then 'enable' again")


def cmd_register_snap_proxy(a):
    if not snap_proxy_installed():
        sys.exit("error: snap-store-proxy is not installed; run 'enable' first")
    # Interactive: prompts for Ubuntu One credentials. Uses proxy.domain set by
    # 'enable' and generates the proxy keys itself.
    run("lxc", "exec", NAME, "--", "snap-store-proxy", "register")
    print("registered; run 'enable' (or 'apply') to configure Juju")


def cmd_disable(a):
    run("juju", "model-defaults", *controller_args(a), "--reset",
        ",".join(("apt-http-proxy", "apt-no-proxy", *SNAP_KEYS)))
    run("juju", "remove-cloud", LXD_CLOUD, "--client", check=False, quiet=True)
    # A stopped container is what marks the cache as disabled; the cache is kept.
    if ok("lxc", "info", NAME):
        run("lxc", "config", "set", NAME, "boot.autostart=false")
        run("lxc", "stop", NAME, check=False, quiet=True)
    print(f"disabled on {controller_label(a)}")


def cmd_destroy(a):
    try:
        cmd_disable(a)
    except subprocess.CalledProcessError:
        pass
    run("lxc", "delete", "-f", NAME)


def _os_upgrade(a, value):
    run("juju", "model-defaults", *controller_args(a),
        f"enable-os-upgrade={value}", f"enable-os-refresh-update={value}")


def cmd_skip_os_upgrade(a):
    _os_upgrade(a, "false")


def cmd_restore_os_upgrade(a):
    _os_upgrade(a, "true")


def cmd_apply(a):
    run("juju", "model-config", "-m", a.model,
        f"apt-http-proxy={proxy_url()}", f"apt-no-proxy={no_proxy_list()}",
        *[f"{k}={v}" for k, v in snap_args(a).items()])


def cmd_refresh_images(a):
    # Images with no recorded source are pinned and cannot be refreshed; re-import
    # them with: lxc image copy ubuntu:<version> local: [--vm] --auto-update
    p = run("lxc", "image", "list", "-c", "lf", "--format", "csv", capture=True)
    for line in p.stdout.splitlines():
        alias, _, fp = line.partition(",")
        if not alias.startswith("juju/"):
            continue
        info = run("lxc", "image", "info", fp, check=False, capture=True).stdout or ""
        if not any(l.startswith("    Server:") for l in info.splitlines()):
            print(f"WARNING: image {fp} has no update source (pinned)", file=sys.stderr)
        run("lxc", "image", "refresh", fp, check=False)


def cmd_status(a):
    run("lxc", "list", NAME, "-c", "ns4", check=False)
    p = run("juju", "model-defaults", *controller_args(a), check=False, capture=True)
    for line in (p.stdout or "").splitlines():
        if line.startswith(("apt-http-proxy", "apt-no-proxy", "enable-os-",
                            "snap-store-proxy")):
            print(line)
    ip = cache_ip()
    if ip and reachable(ip):
        print(f"cache reachable at {ip}:{PORT}")
    if ip and snap_proxy_installed():
        state = "registered" if snap_assertions() else "NOT registered"
        up = "reachable" if snap_reachable(ip) else "unreachable"
        print(f"snap store proxy {up} at {snap_url(ip)} ({state})")


def cmd_charmcraft_env(a):
    if a.unset:
        print("unset http_proxy HTTP_PROXY no_proxy NO_PROXY")
        return
    # stdout must stay empty unless the cache is usable, so it is safe to eval.
    ip = cache_ip()
    if not ip:
        print(f"apt cache container {NAME!r} is missing or stopped (run 'enable'); "
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

    def add(name, func, help_, controller=True):
        sp = sub.add_parser(name, help=help_, description=help_,
                            formatter_class=argparse.RawDescriptionHelpFormatter)
        sp.set_defaults(func=func)
        if controller:
            sp.add_argument("controller", nargs="?", default=None,
                            metavar="CONTROLLER",
                            help="Juju controller (default: the current controller)")
        return sp

    sp = add("enable", cmd_enable,
             "create the cache container (apt + snap proxy) and set Juju model-defaults")
    sp.add_argument("--no-snap", action="store_true",
                    help="only set up the apt cache, not the snap store proxy")
    add("disable", cmd_disable, "clear Juju model-defaults and stop the container (cache is kept)")
    add("destroy", cmd_destroy, "disable, then delete the container and its cache")
    add("status", cmd_status, "show container, Juju defaults and cache reachability")
    add("skip-os-upgrade", cmd_skip_os_upgrade,
        "skip apt update/upgrade on new Juju machines")
    add("restore-os-upgrade", cmd_restore_os_upgrade,
        "restore apt update/upgrade on new Juju machines")
    add("refresh-images", cmd_refresh_images,
        "refresh cached LXD images Juju uses", controller=False)
    add("register-snap-proxy", cmd_register_snap_proxy,
        "register the snap store proxy with Ubuntu One (interactive, once)",
        controller=False)
    sp = add("apply", cmd_apply, "point an existing Juju model at the cache",
             controller=False)
    sp.add_argument("--no-snap", action="store_true", help="skip the snap store proxy")
    sp.add_argument("model", metavar="CONTROLLER:MODEL", help="model to configure")
    sp = add("charmcraft-env", cmd_charmcraft_env,
             "print shell exports so charmcraft builds use the cache\n\n"
             "usage: eval \"$(manage-cache.py charmcraft-env)\"\n\n"
             "Prints nothing on stdout (and a note on stderr, exit 0) if the cache\n"
             "container is stopped, missing or unreachable, so it is safe to run\n"
             "unconditionally before a build.",
             controller=False)
    sp.add_argument("--unset", action="store_true",
                    help="print commands that remove the proxy variables instead")
    return p


def main():
    args = build_parser().parse_args()
    try:
        args.func(args)
    except subprocess.CalledProcessError as e:
        sys.exit(f"error: command failed ({e.returncode}): {' '.join(e.cmd)}")


if __name__ == "__main__":
    main()
