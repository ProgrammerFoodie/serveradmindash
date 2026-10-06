#!/usr/bin/env python3
"""Fill the @PLACEHOLDERS@ of a deploy template from this server's private values.

The repository never contains real domains or addresses. They live in two untracked files next to this
script: local.env (DOMAIN, TAILSCALE_IP) and allowed-devices.txt (the Tailscale devices allowed in).

    render.py TEMPLATE [--env FILE] [--devices FILE]     print the filled template
    render.py --get KEY [--env FILE]                      print one value
"""

import argparse
import ipaddress
import os
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
HOSTNAME = re.compile(r"(?=.{1,253}\Z)([A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z]{2,63}")


class RenderError(Exception):
    pass


def load_env(path: Path) -> dict[str, str]:
    if not path.is_file():
        raise RenderError(f"{path} is missing: copy local.env.example to local.env and fill it in")
    values = {}
    for n, line in enumerate(path.read_text().splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, sep, value = line.partition("=")
        if not sep or not re.fullmatch(r"[A-Z_]+", key.strip()):
            raise RenderError(f"{path}:{n}: expected KEY=value")
        values[key.strip()] = value.strip().strip("\"'")
    domain, ip = values.get("DOMAIN", ""), values.get("TAILSCALE_IP", "")
    if not HOSTNAME.fullmatch(domain):
        raise RenderError(f"{path}: DOMAIN is not a valid host name: {domain!r}")
    try:
        if ipaddress.ip_address(ip).version != 4:
            raise ValueError
    except ValueError:
        raise RenderError(f"{path}: TAILSCALE_IP must be an IPv4 address: {ip!r}") from None
    return values


def device_lines(path: Path) -> str:
    if not path.is_file():
        raise RenderError(f"{path} is missing: list the devices that may open the dashboard, one per line")
    out = []
    for n, line in enumerate(path.read_text().splitlines(), 1):
        body, _, label = line.partition("#")
        body = body.strip()
        if not body:
            continue
        try:
            addr = str(ipaddress.ip_address(body))
        except ValueError:
            raise RenderError(f"{path}:{n}: not an IP address: {body!r}") from None
        label = re.sub(r"[^\w .()/-]", "", label).strip()           # a label can never smuggle nginx syntax in
        out.append(f"    {addr:<28} 1;" + (f"   # {label}" if label else ""))
    if not out:
        raise RenderError(f"{path}: no devices listed; nobody could open the dashboard")
    return "\n".join(out)


def render(template: Path, env: dict[str, str], devices_file: Path) -> str:
    text = template.read_text()
    text = text.replace("@DOMAIN@", env["DOMAIN"]).replace("@TAILSCALE_IP@", env["TAILSCALE_IP"])
    if "@ALLOWED_DEVICES@" in text:
        text = text.replace("@ALLOWED_DEVICES@", device_lines(devices_file))
    leftover = re.findall(r"@[A-Z_]+@", text)
    if leftover:
        raise RenderError(f"{template.name}: unfilled placeholder(s) {sorted(set(leftover))}")
    return text


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("template", nargs="?")
    p.add_argument("--env", default=os.environ.get("DASHBOARD_LOCAL_ENV", HERE / "local.env"))
    p.add_argument("--devices", default=os.environ.get("DASHBOARD_ALLOWED_DEVICES", HERE / "allowed-devices.txt"))
    p.add_argument("--get", metavar="KEY")
    args = p.parse_args()
    try:
        env = load_env(Path(args.env))
        if args.get:
            if args.get not in env:
                raise RenderError(f"{args.get} is not set in {args.env}")
            print(env[args.get])
        elif args.template:
            sys.stdout.write(render(Path(args.template), env, Path(args.devices)))
        else:
            p.error("give a template or --get KEY")
    except RenderError as e:
        print(f"render.py: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
