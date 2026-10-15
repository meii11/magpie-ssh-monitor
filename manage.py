#!/usr/bin/env python3
"""Manage independent SSH Codex monitors on macOS, using only the standard library."""
import argparse
import datetime as dt
import fcntl
import json
import os
from pathlib import Path
import plistlib
import re
import shutil
import subprocess
import sys
from urllib.parse import urlsplit

from collector import atomic_json

SOURCE = Path(__file__).resolve().parent
DEFAULT_ROOT = Path.home() / "Library/Application Support/MagpieSSHMonitor"
RUNTIME = ("manage.py", "collector.py", "remote_reader.py", "index.mjs", "package.json", "README.md", "README.zh-CN.md", "LICENSE")


def validate_host(host):
    if not isinstance(host, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,62}", host):
        raise ValueError("Use an explicit SSH alias (1–63 letters, digits, dots, underscores or hyphens).")
    return host


def validate_gateway(value):
    url = urlsplit(value)
    if (url.scheme != "http" or url.hostname != "127.0.0.1" or url.username or url.password
            or url.path or url.query or url.fragment or url.port is None or not 1 <= url.port <= 65535):
        raise ValueError("Gateway must be http://127.0.0.1:PORT, without a path or credentials.")
    return value


def locations(root, host):
    validate_host(host)
    # Reversible encoding prevents collisions between aliases such as a-b, a.b and A-b.
    identity = host.encode("ascii").hex()
    state = root / "servers" / identity
    label = "io.github.meii11.magpie-ssh-monitor." + identity
    return {"state": state, "program": state / "program", "plugin": state / "plugin",
            "provider": "codex-ssh-" + identity, "label": label,
            "plist": Path.home() / "Library/LaunchAgents" / (label + ".plist")}


def registry(root):
    path = root / "registry.json"
    if not path.exists():
        return {"version": 1, "servers": []}
    value = json.loads(path.read_text())
    if value.get("version") != 1 or not isinstance(value.get("servers"), list):
        raise ValueError("Invalid monitor registry.")
    for host in value["servers"]:
        validate_host(host)
    if len(set(value["servers"])) != len(value["servers"]):
        raise ValueError("Duplicate SSH aliases in registry.")
    return value


def magpie_path(explicit=None):
    value = explicit or shutil.which("magpie")
    if value is None:
        value = "/Applications/magpie.app/Contents/MacOS/magpie"
    path = Path(value).expanduser().resolve()
    if not path.is_file() or not os.access(path, os.X_OK):
        raise ValueError("Magpie CLI not found. Install Magpie or pass --magpie /path/to/magpie.")
    return str(path)


def make_config(args):
    validate_host(args.host)
    validate_gateway(args.gateway)
    if not 5 <= args.interval <= 86400:
        raise ValueError("Interval must be between 5 and 86400 seconds.")
    if not 1 <= args.days <= 3650:
        raise ValueError("Initial history must be between 1 and 3650 days.")
    if not args.remote_dir.startswith(("/", "~/")) or "\x00" in args.remote_dir or "\n" in args.remote_dir:
        raise ValueError("Remote log directory must be an absolute path or start with ~/.")
    name = args.name if args.name is not None else args.host
    if not name or len(name) > 80 or any(ord(c) < 32 or ord(c) == 127 for c in name):
        raise ValueError("Display name must contain 1–80 printable characters.")
    return {"host": args.host, "title": name, "agent": "SSH-" + args.host,
            "gateway": args.gateway, "remote_dir": args.remote_dir, "interval": args.interval,
            "since": (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=args.days)).isoformat(),
            "magpie": magpie_path(args.magpie), "manual": args.manual}


def copy_atomic(source, destination):
    if source.resolve() == destination.resolve():
        return
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    shutil.copyfile(source, temporary)
    os.chmod(temporary, 0o600)
    os.replace(temporary, destination)


def deploy(paths, config):
    for folder in (paths["state"], paths["program"], paths["plugin"]):
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    for name in RUNTIME:
        copy_atomic(SOURCE / name, paths["program"] / name)
    copy_atomic(SOURCE / "index.mjs", paths["plugin"] / "index.mjs")
    package = json.loads((SOURCE / "package.json").read_text())
    atomic_json(paths["plugin"] / "package.json", {"name": paths["provider"], "version": package["version"],
                "type": "module", "main": "index.mjs", "license": "MIT"})
    atomic_json(paths["plugin"] / "monitor.json", {"version": 1, "host": config["host"],
                "title": config["title"], "state_dir": str(paths["state"]), "provider": paths["provider"],
                "stale_after_seconds": max(180, config["interval"] * 3)})


def run_sync(paths):
    subprocess.run([sys.executable, str(paths["program"] / "collector.py"), "sync", "--state", str(paths["state"]), "--drain"], check=True)
    value = json.loads((paths["state"] / "snapshot.json").read_text())
    if value.get("error") or value["unresolved_models"]:
        raise RuntimeError(value.get("error") or "Unresolved model identities; setup stopped.")
    return value


def backup_magpie(root):
    config = Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))) / "magpie"
    backup = root / "backups" / dt.datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    backup.mkdir(parents=True, mode=0o700)
    for name in ("plugins.json", "plugin-auth.json", "settings.json"):
        source = config / name
        if source.is_file():
            copy_atomic(source, backup / name)


def stop_job(paths):
    target = f"gui/{os.getuid()}/{paths['label']}"
    result = subprocess.run(["/bin/launchctl", "print", target], capture_output=True, text=True)
    if result.returncode == 0:
        if str(paths["plist"]) not in result.stdout or str(paths["state"]) not in result.stdout:
            raise RuntimeError("A different installation owns this LaunchAgent; refusing to stop it.")
        subprocess.run(["/bin/launchctl", "bootout", target], check=True)


def job_definition(paths, config):
    return {"Label": paths["label"], "ProgramArguments": [sys.executable, str(paths["program"] / "collector.py"),
                "sync", "--state", str(paths["state"]), "--quiet"],
            "StartInterval": config["interval"], "RunAtLoad": True, "ProcessType": "Background",
            "WorkingDirectory": str(paths["program"]), "EnvironmentVariables": {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin"},
            "StandardOutPath": str(paths["state"] / "launchd.stdout.log"),
            "StandardErrorPath": str(paths["state"] / "launchd.stderr.log")}


def start_job(paths, config):
    if config["manual"]:
        return
    paths["plist"].parent.mkdir(parents=True, exist_ok=True)
    temporary = paths["plist"].with_suffix(".plist.tmp")
    with temporary.open("wb") as stream:
        plistlib.dump(job_definition(paths, config), stream)
    os.chmod(temporary, 0o600)
    os.replace(temporary, paths["plist"])
    subprocess.run(["/bin/launchctl", "bootstrap", f"gui/{os.getuid()}", str(paths["plist"])], check=True)


def register_plugin(root, paths, config):
    backup_magpie(root)
    subprocess.run([config["magpie"], "plugin", "add", str(paths["plugin"])], check=True)
    subprocess.run([config["magpie"], "plugin", "login", paths["provider"]], input=config["host"] + "\n", text=True, check=True)


def add(root, args):
    config = make_config(args)
    value = registry(root)
    if args.host in value["servers"]:
        raise ValueError("Server already exists. Use update, sync or remove.")
    paths = locations(root, args.host)
    config_file = paths["state"] / "config.json"
    if config_file.exists():
        previous = json.loads(config_file.read_text())
        if previous.get("host") != args.host:
            raise ValueError("Existing state belongs to another server.")
        config["since"] = previous["since"]  # Keep the same history boundary when re-adding.
    paths["state"].mkdir(parents=True, exist_ok=True, mode=0o700)
    atomic_json(config_file, config)
    stop_job(paths)
    deploy(paths, config)
    snapshot = run_sync(paths)
    register_plugin(root, paths, config)
    start_job(paths, config)
    value["servers"].append(args.host)
    atomic_json(root / "registry.json", value)
    return {"host": args.host, "agent": config["agent"], "provider": paths["provider"],
            "state": str(paths["state"]), "records": snapshot["records"], "sessions": snapshot["sessions"]}


def existing(root, host):
    if host not in registry(root)["servers"]:
        raise ValueError("Unknown server. Run: python3 manage.py add " + validate_host(host))
    paths = locations(root, host)
    return paths, json.loads((paths["state"] / "config.json").read_text())


def update(root, host):
    paths, config = existing(root, host)
    stop_job(paths)
    deploy(paths, config)
    snapshot = run_sync(paths)
    register_plugin(root, paths, config)
    start_job(paths, config)
    return {"host": host, "records": snapshot["records"], "updated": True}


def remove(root, host):
    paths, config = existing(root, host)
    stop_job(paths)
    backup_magpie(root)
    subprocess.run([config["magpie"], "plugin", "logout", paths["provider"]], check=True)
    subprocess.run([config["magpie"], "plugin", "remove", str(paths["plugin"])], check=True)
    if paths["plist"].exists():
        paths["plist"].unlink()
    value = registry(root)
    value["servers"].remove(host)
    atomic_json(root / "registry.json", value)
    return {"host": host, "removed": True, "retained_data": str(paths["state"])}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT, help="private local state directory")
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("add", help="add an SSH alias and install its Magpie monitor")
    create.add_argument("host")
    create.add_argument("--name", help="name displayed in Magpie")
    create.add_argument("--remote-dir", default="~/.codex/sessions")
    create.add_argument("--gateway", default="http://127.0.0.1:3425")
    create.add_argument("--interval", type=int, default=60, help="poll interval in seconds (default: 60)")
    create.add_argument("--days", type=int, default=7, help="initial rolling history in days (default: 7)")
    create.add_argument("--magpie", help="Magpie CLI path")
    create.add_argument("--manual", action="store_true", help="do not install a LaunchAgent; run sync manually")
    commands.add_parser("list", help="list configured SSH servers")
    for name in ("sync", "status", "remove", "update"):
        command = commands.add_parser(name)
        command.add_argument("host")
    args = parser.parse_args()
    root = args.root.expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    if root.stat().st_uid != os.getuid() or root.stat().st_mode & 0o077:
        parser.error("State root must be a private directory owned by you (permissions 700).")
    if sys.platform != "darwin" and args.command in ("add", "remove", "update"):
        parser.error("Installation and scheduling require macOS. SSH servers may run any OS with Python 3.")
    with (root / "manage.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            if args.command == "add":
                result = add(root, args)
            elif args.command == "remove":
                result = remove(root, args.host)
            elif args.command == "update":
                result = update(root, args.host)
            elif args.command == "list":
                result = []
                for host in registry(root)["servers"]:
                    paths, config = existing(root, host)
                    result.append({"host": host, "name": config["title"], "agent": config["agent"],
                                   "interval": config["interval"], "state": str(paths["state"])})
            else:
                paths, _ = existing(root, args.host)
                result = run_sync(paths) if args.command == "sync" else json.loads((paths["state"] / "snapshot.json").read_text())
            print(json.dumps(result, ensure_ascii=False, indent=2))
        except (ValueError, RuntimeError, OSError, subprocess.SubprocessError) as error:
            parser.exit(1, f"magpie-ssh-monitor: {error}\n")


if __name__ == "__main__":
    main()
