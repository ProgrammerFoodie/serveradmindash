"""Collector registry.

Every collector is an object with a `cadence` ("fast" = 5s, "medium" = 60s,
"slow" = 6h) and a `collect(cfg) -> dict` method. Rate-based collectors keep
their previous sample, so their first call returns rates as None.

run() wraps every collector so one broken source can never take down the page:
an exception becomes {"error": "..."} in that section only.
"""

from . import agents, cpu, disks, logs, memory, network, processes, security, services, system, users


def create(cfg: dict | None = None) -> dict:
    """Fresh collector instances (each keeps its own rate/log state).

    With a config, collectors that belong to an admin tool are created only if config.json switches the tool on.
    Without one (tests, development) every collector is created."""
    from ..config import admin_switches
    instances = {
        # fast
        "cpu": cpu.Cpu(),
        "memory": memory.Memory(),
        "disk_io": disks.DiskIO(),
        "net_io": network.NetIO(),
        "processes": processes.Processes(),
        "cgroups": processes.Cgroups(),
        # medium
        "system": system.System(),
        "disks": disks.DiskUsage(),
        "systemd": services.Systemd(),
        "supervisor": services.Supervisor(),
        "sockets": network.Sockets(),
        "tailscale": network.Tailscale(),
        "fail2ban": security.Fail2ban(),
        "logins": security.Logins(),
        "ssh_auth": security.SshAuth(),
        "journal": logs.Journal(),
        "nginx": logs.Nginx(),
        "agents": agents.Agents(),
        # slow
        "updates": security.Updates(),
        "ssl": logs.Ssl(),
    }
    if cfg is None or admin_switches(cfg)["users"]:
        instances["users"] = users.Users()
    return instances


def run(collector, cfg: dict) -> dict:
    try:
        return collector.collect(cfg)
    except Exception as e:  # noqa: BLE001 - deliberate isolation boundary
        return {"error": f"{type(e).__name__}: {e}"}
