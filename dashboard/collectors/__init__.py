"""Collector registry.

Every collector is an object with a `cadence` ("fast" = 5s, "medium" = 60s,
"slow" = 6h) and a `collect(cfg) -> dict` method. Rate-based collectors keep
their previous sample, so their first call returns rates as None.

run() wraps every collector so one broken source can never take down the page:
an exception becomes {"error": "..."} in that section only.
"""

from . import cpu, disks, logs, memory, network, processes, security, services, system


def create() -> dict:
    """Fresh collector instances (each keeps its own rate/log state)."""
    return {
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
        # slow
        "updates": security.Updates(),
        "ssl": logs.Ssl(),
    }


def run(collector, cfg: dict) -> dict:
    try:
        return collector.collect(cfg)
    except Exception as e:  # noqa: BLE001 - deliberate isolation boundary
        return {"error": f"{type(e).__name__}: {e}"}
