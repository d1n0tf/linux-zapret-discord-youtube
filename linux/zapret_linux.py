#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path
from typing import Iterable


ROOT = Path(__file__).resolve().parent.parent
LISTS_DIR = ROOT / "lists"
UTILS_DIR = ROOT / "utils"
SERVICE_DIR = ROOT / ".service"
STATE_DIR = ROOT / ".runtime"
STATE_FILE = STATE_DIR / "state.json"
LOG_FILE = STATE_DIR / "nfqws.log"
PID_FALLBACK = STATE_DIR / "nfqws.pid"

LOCAL_VERSION_FILE = SERVICE_DIR / "version.txt"
GAME_FILTER_FILE = UTILS_DIR / "game_filter.enabled"
CHECK_UPDATES_FILE = UTILS_DIR / "check_updates.enabled"
IPSET_NONE_SENTINEL = "203.0.113.113/32"

ORIGINAL_REPO = "Flowseal/zapret-discord-youtube"
FORK_REPO = "d1n0tf/linux-zapret-discord-youtube"

REMOTE_VERSION_URL = f"https://raw.githubusercontent.com/{ORIGINAL_REPO}/main/.service/version.txt"
REMOTE_IPSET_URL = f"https://raw.githubusercontent.com/{ORIGINAL_REPO}/refs/heads/main/.service/ipset-service.txt"
REMOTE_HOSTS_URL = f"https://raw.githubusercontent.com/{ORIGINAL_REPO}/refs/heads/main/.service/hosts"

NFT_TABLE = "zapret_linux"
NFT_CHAIN = "post"
NFQUEUE_NUM = 200
SYSTEMD_UNIT_NAME = "zapret-discord-youtube-linux.service"

USER_LIST_DEFAULTS = {
    LISTS_DIR / "ipset-exclude-user.txt": f"{IPSET_NONE_SENTINEL}\n",
    LISTS_DIR / "list-general-user.txt": "# Never leave this file empty\ndomain.example.abc\n",
    LISTS_DIR / "list-exclude-user.txt": "domain.example.abc\n",
}


def fail(message: str, code: int = 1) -> int:
    print(f"[ERROR] {message}", file=sys.stderr)
    return code


def run_command(
    args: list[str],
    *,
    check: bool = True,
    capture_output: bool = False,
    text: bool = True,
    input_text: str | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        check=check,
        capture_output=capture_output,
        text=text,
        input=input_text,
    )


def natural_key(path: Path) -> list[object]:
    return [int(part) if part.isdigit() else part.lower() for part in re.split(r"(\d+)", path.name)]


def list_strategies() -> list[Path]:
    return sorted(
        [
            path
            for path in ROOT.glob("*.bat")
            if not path.name.lower().startswith("service")
        ],
        key=natural_key,
    )


def ensure_user_lists() -> None:
    LISTS_DIR.mkdir(parents=True, exist_ok=True)
    for path, content in USER_LIST_DEFAULTS.items():
        if not path.exists():
            path.write_text(content, encoding="utf-8")


def require_root() -> None:
    if os.geteuid() != 0:
        raise RuntimeError("запускай эту команду через sudo/root: нужны nftables и nfqueue")


def command_exists(name: str) -> bool:
    return shutil.which(name) is not None


def require_commands(*names: str) -> None:
    missing = [name for name in names if not command_exists(name)]
    if missing:
        raise RuntimeError(f"не найдены нужные команды: {', '.join(missing)}")


def resolve_nfqws_bin() -> str:
    env_value = os.environ.get("NFQWS_BIN")
    if env_value:
        candidate = Path(env_value).expanduser()
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
        raise RuntimeError(f"NFQWS_BIN указывает на неисполняемый файл: {candidate}")

    for candidate in (ROOT / "bin/linux/nfqws", Path("/opt/zapret/nfq/nfqws")):
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)

    path_candidate = shutil.which("nfqws")
    if path_candidate:
        return path_candidate

    raise RuntimeError(
        "не найден nfqws. Установи нативный zapret для Linux и добавь nfqws в PATH, "
        "или укажи путь через NFQWS_BIN"
    )


def resolve_nfqws_user_arg() -> str:
    env_user = os.environ.get("NFQWS_USER", "").strip()
    if env_user:
        if env_user in {"root", "0"}:
            return "--uid=0"
        return f"--user={env_user}"

    env_uid = os.environ.get("NFQWS_UID", "").strip()
    if env_uid:
        return f"--uid={env_uid}"

    return "--uid=0"


def detect_wan_interface() -> str:
    try:
        result = run_command(["ip", "-json", "route", "show", "default"], capture_output=True)
        routes = json.loads(result.stdout or "[]")
        for route in routes:
            dev = route.get("dev")
            if dev:
                return str(dev)
    except (subprocess.CalledProcessError, json.JSONDecodeError):
        pass

    result = run_command(["ip", "route", "show", "default"], capture_output=True)
    for line in result.stdout.splitlines():
        parts = line.split()
        if "dev" in parts:
            idx = parts.index("dev")
            if idx + 1 < len(parts):
                return parts[idx + 1]
    raise RuntimeError("не удалось определить WAN-интерфейс через `ip route show default`")


def load_game_filter() -> dict[str, str]:
    if not GAME_FILTER_FILE.exists():
        return {
            "mode": "disabled",
            "status": "disabled",
            "tcp": "12",
            "udp": "12",
        }

    mode = GAME_FILTER_FILE.read_text(encoding="utf-8", errors="replace").strip().lower()
    if mode == "all":
        return {
            "mode": "all",
            "status": "enabled (TCP and UDP)",
            "tcp": "1024-65535",
            "udp": "1024-65535",
        }
    if mode == "tcp":
        return {
            "mode": "tcp",
            "status": "enabled (TCP)",
            "tcp": "1024-65535",
            "udp": "12",
        }
    return {
        "mode": "udp",
        "status": "enabled (UDP)",
        "tcp": "12",
        "udp": "1024-65535",
    }


def set_game_filter(mode: str) -> None:
    if mode == "disable":
        GAME_FILTER_FILE.unlink(missing_ok=True)
        return
    GAME_FILTER_FILE.write_text(f"{mode}\n", encoding="utf-8")


def ipset_status() -> str:
    list_file = LISTS_DIR / "ipset-all.txt"
    if not list_file.exists():
        return "missing"

    entries = ipset_entries(list_file)
    if not entries:
        return "any"
    if entries == [IPSET_NONE_SENTINEL]:
        return "none"
    return "loaded"


def ipset_entries(path: Path | None = None) -> list[str]:
    list_file = path or LISTS_DIR / "ipset-all.txt"
    if not list_file.exists():
        return []
    return [
        line
        for line in (
            raw_line.strip()
            for raw_line in list_file.read_text(encoding="utf-8", errors="replace").splitlines()
        )
        if line and not line.startswith("#")
    ]


def ipset_status_label() -> str:
    return format_ipset_status(ipset_status(), ipset_entry_count())


def format_ipset_status(status: object, count: object | None = None) -> str:
    if status == "missing":
        return "missing"
    if status == "any":
        return "any (empty list)"
    if status == "none":
        return f"none ({IPSET_NONE_SENTINEL})"
    if status == "loaded":
        if count is None:
            return "loaded"
        return f"loaded ({count} entries)"
    return str(status)


def ipset_entry_count() -> int:
    return len(ipset_entries())


def set_ipset_mode(mode: str) -> None:
    list_file = LISTS_DIR / "ipset-all.txt"
    backup_file = LISTS_DIR / "ipset-all.txt.backup"
    current = ipset_status()

    if mode == "status":
        print(current)
        return

    if mode == "loaded":
        if current == "loaded":
            return
        if backup_file.exists():
            list_file.unlink(missing_ok=True)
            shutil.copyfile(backup_file, list_file)
            return
        raise RuntimeError("не найден backup ipset-all.txt.backup. Сначала обнови ipset-лист")

    if mode == "none":
        if current == "loaded":
            if backup_file.exists():
                backup_file.unlink()
            list_file.rename(backup_file)
        list_file.write_text(f"{IPSET_NONE_SENTINEL}\n", encoding="utf-8")
        return

    if mode == "any":
        if current == "loaded":
            if backup_file.exists():
                backup_file.unlink()
            list_file.rename(backup_file)
        list_file.write_text("", encoding="utf-8")
        return

    raise RuntimeError(f"неизвестный режим ipset: {mode}")


def check_updates_enabled() -> bool:
    return CHECK_UPDATES_FILE.exists()


def set_update_check(enabled: bool) -> None:
    if enabled:
        CHECK_UPDATES_FILE.write_text("ENABLED\n", encoding="utf-8")
    else:
        CHECK_UPDATES_FILE.unlink(missing_ok=True)


def download_text(url: str, *, timeout: int = 10) -> str:
    request = urllib.request.Request(
        url,
        headers={
            "Cache-Control": "no-cache",
            "User-Agent": "zapret-discord-youtube-linux",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read().decode("utf-8")


def read_local_version() -> str:
    return LOCAL_VERSION_FILE.read_text(encoding="utf-8", errors="replace").strip()


def normalize_github_repo(raw: str) -> str:
    value = raw.strip()
    value = re.sub(r"^https://github\.com/", "", value)
    value = value.removesuffix(".git").strip("/")
    if not re.match(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$", value):
        raise RuntimeError(
            "ZAPRET_FORK_REPO должен быть в формате owner/repo "
            "или https://github.com/owner/repo"
        )
    return value


def fork_repo() -> str:
    return normalize_github_repo(os.environ.get("ZAPRET_FORK_REPO", FORK_REPO))


def github_raw_url(repo: str, path: str) -> str:
    return f"https://raw.githubusercontent.com/{repo}/main/{path.lstrip('/')}"


def github_releases_url(repo: str, version: str | None = None) -> str:
    if version:
        return f"https://github.com/{repo}/releases/tag/{version}"
    return f"https://github.com/{repo}/releases/latest"


def fetch_version(label: str, url: str, *, quiet: bool) -> str | None:
    try:
        return download_text(url, timeout=5).strip()
    except (urllib.error.URLError, TimeoutError):
        if not quiet:
            print(f"[WARN] не удалось получить версию {label}")
        return None


def check_updates(*, quiet: bool = False, open_browser: bool = False) -> dict[str, object]:
    local_version = read_local_version()
    current_fork_repo = fork_repo()
    original_version = fetch_version("оригинала", REMOTE_VERSION_URL, quiet=quiet)
    fork_version = fetch_version(
        "форка",
        github_raw_url(current_fork_repo, ".service/version.txt"),
        quiet=quiet,
    )

    fork_update_available = fork_version is not None and local_version != fork_version
    fork_release_url = github_releases_url(current_fork_repo, fork_version)
    fork_latest_url = github_releases_url(current_fork_repo)

    if not quiet:
        print(f"Установленная версия форка: {local_version}")
        if original_version:
            print(f"Последняя версия оригинала: {original_version}")
        if fork_version:
            print(f"Последняя версия форка: {fork_version}")

        if fork_version is None:
            print(f"Релизы форка: {fork_latest_url}")
        elif fork_update_available:
            print(f"Доступно обновление форка: {fork_version}")
            print(f"Релизы форка: {fork_release_url}")
            if open_browser:
                opened = webbrowser.open(fork_latest_url)
                print("Открываю страницу релизов форка" if opened else "Не удалось открыть браузер")
        else:
            print("Установлена последняя версия форка")

        if original_version and fork_version and original_version != fork_version:
            print(
                "Версия оригинального проекта отличается от версии форка. "
                "Это нормально, если форк обновляется отдельными релизами."
            )

    return {
        "local": local_version,
        "original": original_version,
        "fork": fork_version,
        "fork_repo": current_fork_repo,
        "fork_update_available": fork_update_available,
        "fork_release_url": fork_release_url,
        "fork_latest_url": fork_latest_url,
    }


def parse_strategy_path(raw: str | None) -> Path:
    strategies = list_strategies()
    if not strategies:
        raise RuntimeError("в репозитории не найдено ни одного general*.bat")

    if raw is None:
        default = ROOT / "general.bat"
        if default.exists():
            return default
        return strategies[0]

    candidate = Path(raw)
    if candidate.is_file():
        return candidate.resolve()
    rooted = ROOT / raw
    if rooted.is_file():
        return rooted.resolve()
    if not raw.lower().endswith(".bat"):
        rooted_bat = ROOT / f"{raw}.bat"
        if rooted_bat.is_file():
            return rooted_bat.resolve()

    lowered = raw.lower()
    matches = [
        path
        for path in strategies
        if path.name.lower() == lowered
        or path.stem.lower() == lowered
    ]
    if len(matches) == 1:
        return matches[0].resolve()
    if len(matches) > 1:
        names = ", ".join(path.name for path in matches)
        raise RuntimeError(f"несколько стратегий подходят под `{raw}`: {names}")
    raise RuntimeError(f"стратегия `{raw}` не найдена")


def parse_strategy(strategy_path: Path) -> dict[str, object]:
    game_filter = load_game_filter()
    lines = strategy_path.read_text(encoding="utf-8-sig", errors="replace").splitlines()

    command_lines: list[str] = []
    capture = False
    for raw_line in lines:
        line = raw_line.strip()
        if not capture and "winws.exe" not in line.lower():
            continue
        capture = True
        has_continuation = line.rstrip().endswith("^")
        cleaned = line.rstrip()
        if has_continuation:
            cleaned = cleaned[:-1].rstrip()
        command_lines.append(cleaned)
        if capture and not has_continuation:
            break

    if not command_lines:
        raise RuntimeError(f"не удалось найти строку запуска winws в `{strategy_path.name}`")

    command = " ".join(part for part in command_lines if part)
    command = re.sub(r'^.*?winws\.exe"?\s*', "", command, count=1, flags=re.IGNORECASE)
    replacements = {
        "%BIN%": f"{(ROOT / 'bin').as_posix()}/",
        "%LISTS%": f"{LISTS_DIR.as_posix()}/",
        "%GameFilterTCP%": game_filter["tcp"],
        "%GameFilterUDP%": game_filter["udp"],
        "%GameFilter%": game_filter["tcp"] if game_filter["tcp"] == game_filter["udp"] else "1024-65535",
        "^!": "!",
        "%%BIN%%": f"{(ROOT / 'bin').as_posix()}/",
        "%%LISTS%%": f"{LISTS_DIR.as_posix()}/",
    }
    for old, new in replacements.items():
        command = command.replace(old, str(new))

    tokens = shlex.split(command, posix=True)
    nfqws_args: list[str] = []
    wf_tcp = ""
    wf_udp = ""
    for token in tokens:
        if token.startswith("--wf-tcp="):
            wf_tcp = token.split("=", 1)[1]
            continue
        if token.startswith("--wf-udp="):
            wf_udp = token.split("=", 1)[1]
            continue
        nfqws_args.append(token)

    nfqws_args = apply_linux_profile_overrides(nfqws_args)

    if not wf_tcp and not wf_udp:
        raise RuntimeError(f"не найдены --wf-tcp/--wf-udp в `{strategy_path.name}`")

    if not any(token == "--qnum" or token.startswith("--qnum=") for token in nfqws_args):
        nfqws_args = [f"--qnum={NFQUEUE_NUM}", *nfqws_args]
    if not any(
        token in {"--user", "--uid"} or token.startswith("--user=") or token.startswith("--uid=")
        for token in nfqws_args
    ):
        nfqws_args = [resolve_nfqws_user_arg(), *nfqws_args]

    return {
        "strategy": strategy_path.name,
        "strategy_path": str(strategy_path),
        "wf_tcp": wf_tcp,
        "wf_udp": wf_udp,
        "nfqws_args": nfqws_args,
    }


def split_profiles(args: list[str]) -> list[list[str]]:
    profiles: list[list[str]] = [[]]
    for token in args:
        if token == "--new":
            profiles.append([])
            continue
        profiles[-1].append(token)
    return profiles


def join_profiles(profiles: list[list[str]]) -> list[str]:
    joined: list[str] = []
    for index, profile in enumerate(profiles):
        if index:
            joined.append("--new")
        joined.extend(profile)
    return joined


def udp_filter_contains_443(token: str) -> bool:
    if not token.startswith("--filter-udp="):
        return False
    value = token.split("=", 1)[1]
    return "443" in {item.strip() for item in value.split(",")}


def apply_linux_profile_overrides(args: list[str]) -> list[str]:
    auto_google_quic = os.environ.get("LINUX_AUTO_GOOGLE_QUIC", "1")
    if auto_google_quic.strip().lower() in {"0", "false", "no"}:
        return args

    list_google = str((LISTS_DIR / "list-google.txt").resolve())
    if not Path(list_google).exists():
        return args

    profiles = split_profiles(args)
    updated = False
    for profile in profiles:
        has_udp_443 = any(udp_filter_contains_443(token) for token in profile)
        has_hostlist = any(token.startswith("--hostlist=") for token in profile)
        has_ipset_include = any(token.startswith("--ipset=") for token in profile)
        has_google_hostlist = any(token == f"--hostlist={list_google}" for token in profile)

        if not (has_udp_443 and has_hostlist) or has_ipset_include or has_google_hostlist:
            continue

        insert_at = 0
        for index, token in enumerate(profile):
            if token.startswith("--hostlist="):
                insert_at = index + 1
        profile.insert(insert_at, f"--hostlist={list_google}")
        updated = True

    return join_profiles(profiles) if updated else args


def split_ports(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def nft_set_expr(items: Iterable[str]) -> str:
    normalized = list(items)
    if not normalized:
        raise ValueError("nft_set_expr ожидает непустой список")
    if len(normalized) == 1:
        return normalized[0]
    return "{ " + ", ".join(normalized) + " }"


def remove_nft_table() -> None:
    subprocess.run(
        ["nft", "delete", "table", "inet", NFT_TABLE],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def apply_nft_rules(interface: str, wf_tcp: str, wf_udp: str) -> None:
    tcp_ports = split_ports(wf_tcp)
    udp_ports = split_ports(wf_udp)

    remove_nft_table()
    script_lines = [
        f"table inet {NFT_TABLE} {{",
        f"  chain {NFT_CHAIN} {{",
        "    type filter hook postrouting priority mangle; policy accept;",
    ]

    if "80" in tcp_ports:
        script_lines.append(
            f"    oifname {json.dumps(interface)} meta mark and 0x40000000 == 0 tcp dport 80 queue num {NFQUEUE_NUM} bypass"
        )
        tcp_ports = [item for item in tcp_ports if item != "80"]

    if tcp_ports:
        script_lines.append(
            f"    oifname {json.dumps(interface)} meta mark and 0x40000000 == 0 tcp dport {nft_set_expr(tcp_ports)} "
            f"ct original packets 1-6 queue num {NFQUEUE_NUM} bypass"
        )

    if udp_ports:
        script_lines.append(
            f"    oifname {json.dumps(interface)} meta mark and 0x40000000 == 0 udp dport {nft_set_expr(udp_ports)} "
            f"ct original packets 1-6 queue num {NFQUEUE_NUM} bypass"
        )

    script_lines.extend(["  }", "}"])
    nft_script = "\n".join(script_lines) + "\n"

    run_command(["nft", "-f", "-"], input_text=nft_script)


def load_state() -> dict[str, object] | None:
    if not STATE_FILE.exists():
        return None
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def save_state(data: dict[str, object]) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    pid = data.get("pid")
    if pid is not None:
        PID_FALLBACK.write_text(f"{pid}\n", encoding="utf-8")


def clear_state() -> None:
    STATE_FILE.unlink(missing_ok=True)
    PID_FALLBACK.unlink(missing_ok=True)


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def terminate_pid(pid: int, *, timeout: float = 10.0) -> None:
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return

    deadline = time.time() + timeout
    while time.time() < deadline:
        if not pid_alive(pid):
            return
        time.sleep(0.2)

    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        return


def ensure_not_running() -> None:
    state = load_state()
    if not state:
        clear_state()
        return

    pid = int(state.get("pid", 0))  # type: ignore
    if pid and pid_alive(pid):
        raise RuntimeError(f"zapret уже запущен (PID {pid})")

    remove_nft_table()
    clear_state()


def start_background(strategy: str | None) -> None:
    require_root()
    require_commands("nft", "ip")
    ensure_user_lists()

    if check_updates_enabled():
        result = check_updates(quiet=True)
        if result["fork_update_available"]:
            print(f"[INFO] доступна новая версия форка: {result['fork']}")
            print(f"[INFO] релизы форка: {result['fork_latest_url']}")

    ensure_not_running()
    strategy_path = parse_strategy_path(strategy)
    parsed = parse_strategy(strategy_path)
    nfqws_bin = resolve_nfqws_bin()
    interface = detect_wan_interface()

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    apply_nft_rules(interface, str(parsed["wf_tcp"]), str(parsed["wf_udp"]))

    log_handle = LOG_FILE.open("ab")
    process = subprocess.Popen(
        [nfqws_bin, *[str(arg) for arg in parsed["nfqws_args"]]],  # type: ignore
        stdout=log_handle,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    time.sleep(0.7)

    if process.poll() is not None:
        remove_nft_table()
        raise RuntimeError(
            f"nfqws завершился сразу после старта, смотри лог: {LOG_FILE}"
        )

    save_state(
        {
            "pid": process.pid,
            "started_at": int(time.time()),
            "strategy": parsed["strategy"],
            "strategy_path": parsed["strategy_path"],
            "interface": interface,
            "wf_tcp": parsed["wf_tcp"],
            "wf_udp": parsed["wf_udp"],
            "ipset_status": ipset_status(),
            "ipset_entries": ipset_entry_count(),
            "nfqws_bin": nfqws_bin,
            "log_file": str(LOG_FILE),
            "mode": "background",
        }
    )
    print(f"Запущено: {parsed['strategy']} (PID {process.pid}, iface {interface})")
    print(f"Лог: {LOG_FILE}")


def stop_running(*, quiet: bool = False) -> None:
    require_root()
    require_commands("nft")
    state = load_state()
    if not state:
        remove_nft_table()
        clear_state()
        if not quiet:
            print("zapret не запущен")
        return

    pid = int(state.get("pid", 0))  # type: ignore
    if pid:
        terminate_pid(pid)
    remove_nft_table()
    clear_state()
    if not quiet:
        print("zapret остановлен")


def restart_running(strategy: str | None) -> None:
    stop_running(quiet=True)
    start_background(strategy)


def print_status() -> int:
    state = load_state()
    if not state:
        print("Статус: stopped")
        print(f"Game Filter: {load_game_filter()['status']}")
        print(f"IPSet Filter: {ipset_status_label()}")
        print(f"Auto-Update Check: {'enabled' if check_updates_enabled() else 'disabled'}")
        return 1

    pid = int(state.get("pid", 0))  # type: ignore
    alive = pid_alive(pid) if pid else False
    print(f"Статус: {'running' if alive else 'stale'}")
    print(f"Стратегия: {state.get('strategy', 'unknown')}")
    print(f"PID: {pid if pid else 'unknown'}")
    print(f"Iface: {state.get('interface', 'unknown')}")
    print(f"Лог: {state.get('log_file', LOG_FILE)}")
    print(f"Game Filter: {load_game_filter()['status']}")
    print(f"IPSet Filter: {ipset_status_label()}")
    if state.get("ipset_status") is not None:
        started_ipset = format_ipset_status(state.get("ipset_status"), state.get("ipset_entries"))
        current_ipset = ipset_status_label()
        if started_ipset != current_ipset:
            print(f"IPSet at start: {started_ipset}")
    print(f"Auto-Update Check: {'enabled' if check_updates_enabled() else 'disabled'}")
    return 0 if alive else 1


def run_foreground(strategy: str | None) -> int:
    require_root()
    require_commands("nft", "ip")
    ensure_user_lists()
    strategy_path = parse_strategy_path(strategy)
    parsed = parse_strategy(strategy_path)
    nfqws_bin = resolve_nfqws_bin()
    interface = detect_wan_interface()

    ensure_not_running()
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    apply_nft_rules(interface, str(parsed["wf_tcp"]), str(parsed["wf_udp"]))

    log_handle = LOG_FILE.open("ab")
    process = subprocess.Popen(
        [nfqws_bin, *[str(arg) for arg in parsed["nfqws_args"]]],  # type: ignore
        stdout=log_handle,
        stderr=subprocess.STDOUT,
    )

    save_state(
        {
            "pid": process.pid,
            "started_at": int(time.time()),
            "strategy": parsed["strategy"],
            "strategy_path": parsed["strategy_path"],
            "interface": interface,
            "wf_tcp": parsed["wf_tcp"],
            "wf_udp": parsed["wf_udp"],
            "ipset_status": ipset_status(),
            "ipset_entries": ipset_entry_count(),
            "nfqws_bin": nfqws_bin,
            "log_file": str(LOG_FILE),
            "mode": "foreground",
        }
    )

    def handle_signal(signum: int, _frame: object) -> None:
        try:
            process.send_signal(signum)
        except ProcessLookupError:
            pass

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    try:
        return process.wait()
    finally:
        remove_nft_table()
        clear_state()


def update_ipset() -> None:
    try:
        content = download_text(REMOTE_IPSET_URL, timeout=15)
    except (urllib.error.URLError, TimeoutError):
        content = (SERVICE_DIR / "ipset-service.txt").read_text(encoding="utf-8", errors="replace")
        print("[WARN] GitHub недоступен, использую локальную копию .service/ipset-service.txt")

    target = LISTS_DIR / "ipset-all.txt"
    backup = LISTS_DIR / "ipset-all.txt.backup"
    if target.exists():
        backup.write_text(target.read_text(encoding="utf-8", errors="replace"), encoding="utf-8")
    target.write_text(content, encoding="utf-8")
    print(f"Обновлён {target}")


def update_hosts() -> None:
    require_root()
    try:
        content = download_text(REMOTE_HOSTS_URL, timeout=15)
    except (urllib.error.URLError, TimeoutError):
        content = (SERVICE_DIR / "hosts").read_text(encoding="utf-8", errors="replace")
        print("[WARN] GitHub недоступен, использую локальную копию .service/hosts")

    managed_block = (
        "# BEGIN zapret-discord-youtube\n"
        + content.strip()
        + "\n# END zapret-discord-youtube\n"
    )

    hosts_path = Path("/etc/hosts")
    backup_path = Path(f"/etc/hosts.zapret-backup-{int(time.time())}")
    existing = hosts_path.read_text(encoding="utf-8", errors="replace")
    backup_path.write_text(existing, encoding="utf-8")

    pattern = re.compile(
        r"\n?# BEGIN zapret-discord-youtube\n.*?\n# END zapret-discord-youtube\n?",
        re.DOTALL,
    )
    if pattern.search(existing):
        updated = pattern.sub("\n" + managed_block, existing).rstrip() + "\n"
    else:
        updated = existing.rstrip() + "\n\n" + managed_block

    hosts_path.write_text(updated, encoding="utf-8")
    print(f"/etc/hosts обновлён. Backup: {backup_path}")


def diagnostics() -> int:
    rows: list[tuple[str, str]] = []

    rows.append(("root", "ok" if os.geteuid() == 0 else "sudo required"))
    rows.append(("python3", shutil.which("python3") or "missing"))
    rows.append(("nft", shutil.which("nft") or "missing"))
    rows.append(("ip", shutil.which("ip") or "missing"))

    try:
        rows.append(("nfqws", resolve_nfqws_bin()))
    except RuntimeError as exc:
        rows.append(("nfqws", str(exc)))

    try:
        rows.append(("wan", detect_wan_interface()))
    except RuntimeError as exc:
        rows.append(("wan", str(exc)))

    rows.append(("game_filter", load_game_filter()["status"]))
    rows.append(("ipset", ipset_status_label()))
    rows.append(("updates", "enabled" if check_updates_enabled() else "disabled"))

    for key, value in rows:
        print(f"{key:12} {value}")

    missing = [value for key, value in rows if key in {"python3", "nft", "ip"} and value == "missing"]
    return 1 if missing else 0


def systemd_unit_dir() -> Path:
    return Path("/etc/systemd/system")


def managed_systemd_unit_paths() -> list[Path]:
    unit_dir = systemd_unit_dir()
    paths = list(unit_dir.glob("zapret-discord-youtube-*.service"))
    current = unit_dir / SYSTEMD_UNIT_NAME
    if current not in paths:
        paths.append(current)
    return sorted(paths, key=lambda path: path.name)


def remove_systemd_unit_files(*, include_current: bool) -> list[str]:
    removed: list[str] = []
    for unit_path in managed_systemd_unit_paths():
        unit_name = unit_path.name
        if unit_name == SYSTEMD_UNIT_NAME and not include_current:
            continue
        if not unit_path.exists():
            continue
        subprocess.run(["systemctl", "disable", "--now", unit_name], check=False)
        unit_path.unlink(missing_ok=True)
        removed.append(unit_name)
    return removed


def install_systemd(strategy: str | None, *, enable_now: bool) -> None:
    require_root()
    require_commands("systemctl")
    strategy_path = parse_strategy_path(strategy)
    python_bin = shutil.which("python3") or "/usr/bin/python3"
    removed_units = remove_systemd_unit_files(include_current=False)
    unit_path = systemd_unit_dir() / SYSTEMD_UNIT_NAME
    quoted_script = f'"{ROOT / "linux" / "zapret_linux.py"}"'
    quoted_strategy = f'"{strategy_path}"'
    unit_contents = f"""[Unit]
Description=zapret-discord-youtube Linux launcher
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory={ROOT}
ExecStart={python_bin} {quoted_script} foreground --strategy {quoted_strategy}
ExecStop={python_bin} {quoted_script} stop
Restart=on-failure
RestartSec=3

[Install]
WantedBy=multi-user.target
"""
    unit_path.write_text(unit_contents, encoding="utf-8")
    run_command(["systemctl", "daemon-reload"])
    for unit_name in removed_units:
        print(f"Удалён конфликтующий systemd unit: {unit_name}")
    if enable_now:
        state = load_state()
        if state and state.get("mode") == "background":
            pid = int(state.get("pid", 0))  # type: ignore
            if pid and pid_alive(pid):
                stop_running(quiet=True)
        run_command(["systemctl", "enable", SYSTEMD_UNIT_NAME])
        is_active = subprocess.run(
            ["systemctl", "is-active", "--quiet", SYSTEMD_UNIT_NAME],
            check=False,
        ).returncode == 0
        run_command(["systemctl", "restart" if is_active else "start", SYSTEMD_UNIT_NAME])
        print(f"Установлен и {'перезапущен' if is_active else 'запущен'} systemd unit: {SYSTEMD_UNIT_NAME}")
    else:
        print(f"Установлен systemd unit: {SYSTEMD_UNIT_NAME}")
        print(f"Запуск: sudo systemctl enable --now {SYSTEMD_UNIT_NAME}")


def remove_systemd() -> None:
    require_root()
    require_commands("systemctl")
    removed_units = remove_systemd_unit_files(include_current=True)
    run_command(["systemctl", "daemon-reload"])
    if removed_units:
        print("Удалены systemd units: " + ", ".join(removed_units))
    else:
        print("systemd units не найдены")


def print_strategy_list() -> None:
    strategies = list_strategies()
    if not strategies:
        print("Стратегии не найдены")
        return
    for idx, strategy in enumerate(strategies, start=1):
        print(f"{idx:2}. {strategy.name}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Linux launcher для стратегий zapret-discord-youtube",
    )
    subparsers = parser.add_subparsers(dest="command")

    subparsers.add_parser("list", help="показать доступные стратегии")

    start_parser = subparsers.add_parser("start", help="запустить стратегию в фоне")
    start_parser.add_argument("strategy", nargs="?", help="имя .bat-стратегии или путь к ней")

    fg_parser = subparsers.add_parser("foreground", help="запустить стратегию в foreground для systemd")
    fg_parser.add_argument("--strategy", required=False, help="имя .bat-стратегии или путь к ней")

    stop_parser = subparsers.add_parser("stop", help="остановить zapret")
    stop_parser.set_defaults(no_args_ok=True)

    restart_parser = subparsers.add_parser("restart", help="перезапустить стратегию")
    restart_parser.add_argument("strategy", nargs="?", help="новая стратегия; по умолчанию текущая или general.bat")

    subparsers.add_parser("status", help="показать статус")
    check_updates_parser = subparsers.add_parser("check-updates", help="проверить обновления")
    check_updates_parser.add_argument(
        "--open",
        action="store_true",
        dest="open_browser",
        help="открыть страницу релизов форка, если доступно обновление",
    )
    subparsers.add_parser("update-ipset", help="обновить lists/ipset-all.txt")
    subparsers.add_parser("update-hosts", help="обновить /etc/hosts управляемым блоком")
    subparsers.add_parser("diagnostics", help="проверить зависимости и состояние")

    game_parser = subparsers.add_parser("game-filter", help="управление game filter")
    game_parser.add_argument("mode", choices=["status", "disable", "all", "tcp", "udp"])

    ipset_parser = subparsers.add_parser("ipset-filter", help="управление режимом ipset")
    ipset_parser.add_argument("mode", choices=["status", "loaded", "none", "any"])

    update_parser = subparsers.add_parser("update-check", help="управление автопроверкой обновлений")
    update_parser.add_argument("mode", choices=["status", "enable", "disable"])

    install_parser = subparsers.add_parser("install-systemd", help="установить systemd unit")
    install_parser.add_argument("strategy", nargs="?", help="имя .bat-стратегии или путь к ней")
    install_parser.add_argument("--enable-now", action="store_true", help="сразу включить и запустить unit")

    subparsers.add_parser("remove-systemd", help="удалить systemd unit")
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    if args.command is None:
        parser.print_help()
        return 0

    try:
        if args.command == "list":
            print_strategy_list()
            return 0
        if args.command == "start":
            start_background(args.strategy)
            return 0
        if args.command == "foreground":
            return run_foreground(args.strategy)
        if args.command == "stop":
            stop_running()
            return 0
        if args.command == "restart":
            strategy = args.strategy
            if strategy is None:
                state = load_state()
                strategy = str(state["strategy_path"]) if state and state.get("strategy_path") else None
            restart_running(strategy)
            return 0
        if args.command == "status":
            return print_status()
        if args.command == "check-updates":
            check_updates(open_browser=args.open_browser)
            return 0
        if args.command == "update-ipset":
            update_ipset()
            return 0
        if args.command == "update-hosts":
            update_hosts()
            return 0
        if args.command == "diagnostics":
            return diagnostics()
        if args.command == "game-filter":
            if args.mode == "status":
                print(load_game_filter()["status"])
            else:
                set_game_filter(args.mode)
                print(f"Game Filter: {load_game_filter()['status']}")
            return 0
        if args.command == "ipset-filter":
            if args.mode == "status":
                print(ipset_status())
            else:
                set_ipset_mode(args.mode)
                print(f"IPSet Filter: {ipset_status()}")
            return 0
        if args.command == "update-check":
            if args.mode == "status":
                print("enabled" if check_updates_enabled() else "disabled")
            else:
                set_update_check(args.mode == "enable")
                print("enabled" if check_updates_enabled() else "disabled")
            return 0
        if args.command == "install-systemd":
            install_systemd(args.strategy, enable_now=args.enable_now)
            return 0
        if args.command == "remove-systemd":
            remove_systemd()
            return 0
    except RuntimeError as exc:
        return fail(str(exc))
    except subprocess.CalledProcessError as exc:
        return fail(
            f"команда завершилась с ошибкой ({exc.returncode}): {' '.join(exc.cmd if isinstance(exc.cmd, list) else [str(exc.cmd)])}"
        )
    except FileNotFoundError as exc:
        return fail(f"не найдена команда или файл: {exc.filename}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
