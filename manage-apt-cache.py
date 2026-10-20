#!/usr/bin/env python3
"""Local apt cache (apt-cacher-ng in an LXD container) for faster Juju/LXD tests."""

import argparse
import ipaddress
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import time
import urllib.request

NAME = os.environ.get("CACHE_NAME", "apt-cache")
IMAGE = os.environ.get("CACHE_IMAGE", "ubuntu:24.04")
PORT = 3142
LXD_CLOUD = os.environ.get("LXD_CLOUD_NAME", "lxd")
DEFAULT_CONTROLLER = os.environ.get("JUJU_CONTROLLER", "lxd29")
NO_PROXY = "localhost,127.0.0.1"

EPILOG = textwrap.dedent(
    """\
    environment variables:
      CACHE_NAME        cache container name           (default: apt-cache)
      CACHE_IMAGE       image for the container        (default: ubuntu:24.04)
      LXD_CLOUD_NAME    client-side Juju cloud name    (default: lxd)
      JUJU_CONTROLLER   default CONTROLLER argument    (default: lxd29)

    notes:
      Juju model-defaults only affect models created afterwards; use 'apply'
      for existing models.

    charmcraft:
      Charmcraft builds in its own LXD containers and ignores Juju settings.
      It forwards the host's http_proxy/no_proxy into the build container, so:

          eval "$(manage-apt-cache.py charmcraft-env)"
          charmcraft pack

      charmcraft-env prints nothing if the container is stopped (disabled),
      missing or unreachable, so the eval is always safe.

      Only plain HTTP is cached (apt-cacher-ng); https_proxy is deliberately
      not set. Snap, pip and other HTTPS downloads are not cached.
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


def bridge_cidr():
    """CIDR of the LXD network the cache container is attached to, or ''."""
    p = run("lxc", "query", f"/1.0/instances/{NAME}", check=False, capture=True)
    try:
        net = json.loads(p.stdout)["expanded_devices"]["eth0"]["network"]
    except (ValueError, KeyError, TypeError):
        return ""
    p = run("lxc", "network", "get", net, "ipv4.address", check=False, capture=True)
    try:
        return str(ipaddress.ip_network((p.stdout or "").strip(), strict=False))
    except ValueError:
        return ""


def reachable(ip):
    # Bypass any http_proxy in the environment, which may point at the cache itself.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        opener.open(f"http://{ip}:{PORT}/acng-report.html", timeout=5).close()
    except OSError:
        return False
    return True


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


def define_cloud(url):
    """Define a client-side cloud equal to localhost LXD plus the apt proxy."""
    body = textwrap.dedent(
        f"""\
        clouds:
          {LXD_CLOUD}:
            type: lxd
            auth-types: [certificate]
            regions:
              localhost: {{}}
            config:
              apt-http-proxy: {url}
              apt-no-proxy: {NO_PROXY}
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


def cmd_enable(a):
    create_container()
    url = proxy_url()
    run("juju", "model-defaults", "-c", a.controller,
        f"apt-http-proxy={url}", f"apt-no-proxy={NO_PROXY}")
    define_cloud(url)
    print(f"enabled: apt proxy {url} for new models on {a.controller}")


def cmd_disable(a):
    run("juju", "model-defaults", "-c", a.controller, "--reset", "apt-http-proxy,apt-no-proxy")
    run("juju", "remove-cloud", LXD_CLOUD, "--client", check=False, quiet=True)
    # A stopped container is what marks the cache as disabled; the cache is kept.
    if ok("lxc", "info", NAME):
        run("lxc", "config", "set", NAME, "boot.autostart=false")
        run("lxc", "stop", NAME, check=False, quiet=True)
    print(f"disabled on {a.controller}")


def cmd_destroy(a):
    try:
        cmd_disable(a)
    except subprocess.CalledProcessError:
        pass
    run("lxc", "delete", "-f", NAME)


def _os_upgrade(a, value):
    run("juju", "model-defaults", "-c", a.controller,
        f"enable-os-upgrade={value}", f"enable-os-refresh-update={value}")


def cmd_skip_os_upgrade(a):
    _os_upgrade(a, "false")


def cmd_restore_os_upgrade(a):
    _os_upgrade(a, "true")


def cmd_apply(a):
    run("juju", "model-config", "-m", a.model,
        f"apt-http-proxy={proxy_url()}", f"apt-no-proxy={NO_PROXY}")


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
    p = run("juju", "model-defaults", "-c", a.controller, check=False, capture=True)
    for line in (p.stdout or "").splitlines():
        if line.startswith(("apt-http-proxy", "apt-no-proxy", "enable-os-")):
            print(line)
    ip = cache_ip()
    if ip and reachable(ip):
        print(f"cache reachable at {ip}:{PORT}")


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
    entries = []
    for e in [*existing.split(","), *NO_PROXY.split(","), "::1", ip,
              bridge_cidr(), ".lxd"]:
        e = e.strip()
        if e and e not in entries:
            entries.append(e)
    no_proxy = ",".join(entries)
    for k, v in (("http_proxy", url), ("HTTP_PROXY", url),
                 ("no_proxy", no_proxy), ("NO_PROXY", no_proxy)):
        print(f"export {k}={v}")


def build_parser():
    p = argparse.ArgumentParser(
        prog="manage-apt-cache.py",
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
            sp.add_argument("controller", nargs="?", default=DEFAULT_CONTROLLER,
                            metavar="CONTROLLER",
                            help=f"Juju controller (default: {DEFAULT_CONTROLLER})")
        return sp

    add("enable", cmd_enable, "create the cache container and set Juju model-defaults")
    add("disable", cmd_disable, "clear Juju model-defaults and stop the container (cache is kept)")
    add("destroy", cmd_destroy, "disable, then delete the container and its cache")
    add("status", cmd_status, "show container, Juju defaults and cache reachability")
    add("skip-os-upgrade", cmd_skip_os_upgrade,
        "skip apt update/upgrade on new Juju machines")
    add("restore-os-upgrade", cmd_restore_os_upgrade,
        "restore apt update/upgrade on new Juju machines")
    add("refresh-images", cmd_refresh_images,
        "refresh cached LXD images Juju uses", controller=False)
    sp = add("apply", cmd_apply, "point an existing Juju model at the cache",
             controller=False)
    sp.add_argument("model", metavar="CONTROLLER:MODEL", help="model to configure")
    sp = add("charmcraft-env", cmd_charmcraft_env,
             "print shell exports so charmcraft builds use the cache\n\n"
             "usage: eval \"$(manage-apt-cache.py charmcraft-env)\"\n\n"
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
