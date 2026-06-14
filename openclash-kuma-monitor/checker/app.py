import json
import os
import sys
import time
import urllib.parse
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

import requests
from uptime_kuma_api import MonitorType, UptimeKumaApi, UptimeKumaException


@dataclass
class Node:
    name: str
    kind: str


@dataclass
class CheckResult:
    ok: bool
    latency_ms: Optional[int]
    message: str
    attempts: int


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


KUMA_URL = env("KUMA_URL", "http://uptime-kuma:3001")
KUMA_USERNAME = env("KUMA_USERNAME", "admin")
KUMA_PASSWORD = env("KUMA_PASSWORD", "")

OPENCLASH_API = env("OPENCLASH_API", "http://192.168.1.1:9090").rstrip("/")
OPENCLASH_SECRET = env("OPENCLASH_SECRET")
OPENCLASH_PROXY = env("OPENCLASH_PROXY", "http://192.168.1.1:7893")
TEST_GROUP = env("TEST_GROUP", "🧪 节点监控测试")
TEST_URL = env("TEST_URL", "https://cp.cloudflare.com/generate_204")

CHECK_INTERVAL_SECONDS = int(env("CHECK_INTERVAL_SECONDS", "300"))
RETRY_COUNT = int(env("RETRY_COUNT", "2"))
RETRY_DELAY_SECONDS = int(env("RETRY_DELAY_SECONDS", "8"))
REQUEST_TIMEOUT_SECONDS = int(env("REQUEST_TIMEOUT_SECONDS", "12"))
NODES_FILE = env("NODES_FILE", "/app/config/nodes.json")

MONITOR_PREFIX = env("MONITOR_PREFIX", "OpenClash")


def log(message: str) -> None:
    print(time.strftime("%Y-%m-%d %H:%M:%S"), message, flush=True)


def load_nodes() -> List[Node]:
    with open(NODES_FILE, "r", encoding="utf-8") as f:
        raw = json.load(f)
    nodes = [Node(name=item["name"], kind=item.get("kind", "proxy")) for item in raw]
    if not nodes:
        raise RuntimeError("no nodes configured")
    return nodes


def openclash_headers() -> Dict[str, str]:
    if not OPENCLASH_SECRET:
        return {}
    return {"Authorization": f"Bearer {OPENCLASH_SECRET}"}


def openclash_get(path: str) -> dict:
    url = f"{OPENCLASH_API}{path}"
    r = requests.get(url, headers=openclash_headers(), timeout=REQUEST_TIMEOUT_SECONDS)
    r.raise_for_status()
    return r.json()


def openclash_put(path: str, payload: dict) -> None:
    url = f"{OPENCLASH_API}{path}"
    headers = {"Content-Type": "application/json", **openclash_headers()}
    r = requests.put(url, headers=headers, data=json.dumps(payload).encode("utf-8"), timeout=REQUEST_TIMEOUT_SECONDS)
    r.raise_for_status()


def encoded_name(name: str) -> str:
    return urllib.parse.quote(name, safe="")


def current_group_selection() -> Optional[str]:
    data = openclash_get(f"/proxies/{encoded_name(TEST_GROUP)}")
    return data.get("now")


def select_group_node(name: str) -> None:
    openclash_put(f"/proxies/{encoded_name(TEST_GROUP)}", {"name": name})


def clear_openclash_connections() -> None:
    try:
        r = requests.delete(
            f"{OPENCLASH_API}/connections",
            headers=openclash_headers(),
            timeout=min(REQUEST_TIMEOUT_SECONDS, 5),
        )
        if r.status_code >= 400:
            log(f"clear connections returned HTTP {r.status_code}")
    except requests.RequestException as exc:
        log(f"clear connections failed: {exc}")


def test_via_openclash(node: Node) -> CheckResult:
    old_selection: Optional[str] = None
    last_message = "not checked"

    for attempt in range(1, RETRY_COUNT + 2):
        started = time.monotonic()
        try:
            old_selection = current_group_selection()
            select_group_node(node.name)
            clear_openclash_connections()
            response = requests.get(
                TEST_URL,
                proxies={"http": OPENCLASH_PROXY, "https": OPENCLASH_PROXY},
                timeout=REQUEST_TIMEOUT_SECONDS,
            )
            elapsed_ms = int((time.monotonic() - started) * 1000)
            if 200 <= response.status_code < 400:
                return CheckResult(True, elapsed_ms, f"HTTP {response.status_code}", attempt)
            last_message = f"HTTP {response.status_code}"
        except Exception as exc:
            last_message = f"{type(exc).__name__}: {exc}"
        finally:
            if old_selection:
                try:
                    select_group_node(old_selection)
                except Exception as exc:
                    log(f"restore group failed after {node.name}: {exc}")

        if attempt <= RETRY_COUNT:
            time.sleep(RETRY_DELAY_SECONDS)

    return CheckResult(False, None, last_message[:180], RETRY_COUNT + 1)


def wait_for_kuma() -> None:
    for attempt in range(60):
        try:
            with UptimeKumaApi(KUMA_URL, timeout=10) as api:
                api.need_setup()
                return
        except Exception as exc:
            if attempt == 0 or attempt % 10 == 9:
                log(f"waiting for Uptime Kuma at {KUMA_URL}: {exc}")
            time.sleep(5)
    raise RuntimeError("Uptime Kuma did not become ready")


def kuma_login() -> UptimeKumaApi:
    api = UptimeKumaApi(KUMA_URL, timeout=15)
    if api.need_setup():
        if not KUMA_PASSWORD:
            raise RuntimeError("KUMA_PASSWORD is required for first-run setup")
        log("initializing Uptime Kuma user")
        api.setup(KUMA_USERNAME, KUMA_PASSWORD)
    api.login(KUMA_USERNAME, KUMA_PASSWORD)
    return api


def ensure_push_monitors(nodes: List[Node]) -> Dict[str, str]:
    wait_for_kuma()
    api = kuma_login()
    try:
        existing = {m["name"]: m for m in api.get_monitors()}
        tokens: Dict[str, str] = {}
        for node in nodes:
            monitor_name = f"{MONITOR_PREFIX} / {node.kind} / {node.name}"
            monitor = existing.get(monitor_name)
            if not monitor:
                log(f"creating push monitor: {monitor_name}")
                created = api.add_monitor(
                    type=MonitorType.PUSH,
                    name=monitor_name,
                    interval=CHECK_INTERVAL_SECONDS,
                    maxretries=0,
                    retryInterval=60,
                    description=f"Auto-created monitor for OpenClash node {node.name}",
                )
                monitor_id = created.get("monitorID") or created.get("monitorId")
                monitor = api.get_monitor(monitor_id)
            token = monitor.get("pushToken")
            if not token:
                monitor = api.get_monitor(monitor["id"])
                token = monitor.get("pushToken")
            if not token:
                raise RuntimeError(f"missing push token for {monitor_name}")
            tokens[node.name] = token
        return tokens
    finally:
        api.disconnect()


def push_result(token: str, result: CheckResult) -> None:
    status = "up" if result.ok else "down"
    ping = result.latency_ms if result.latency_ms is not None else 0
    msg = f"{result.message}; attempts={result.attempts}"
    url = f"{KUMA_URL.rstrip('/')}/api/push/{token}"
    r = requests.get(
        url,
        params={"status": status, "msg": msg, "ping": ping},
        timeout=REQUEST_TIMEOUT_SECONDS,
    )
    r.raise_for_status()


def run_once(nodes: List[Node], tokens: Dict[str, str]) -> None:
    for node in nodes:
        result = test_via_openclash(node)
        log(
            f"{node.name}: {'UP' if result.ok else 'DOWN'} "
            f"latency={result.latency_ms}ms attempts={result.attempts} msg={result.message}"
        )
        try:
            push_result(tokens[node.name], result)
        except Exception as exc:
            log(f"push to Uptime Kuma failed for {node.name}: {exc}")


def main() -> int:
    nodes = load_nodes()
    log(f"loaded {len(nodes)} nodes")
    tokens = ensure_push_monitors(nodes)
    log("Uptime Kuma push monitors ready")

    while True:
        started = time.monotonic()
        try:
            run_once(nodes, tokens)
        except UptimeKumaException as exc:
            log(f"Uptime Kuma error, refreshing monitors next round: {exc}")
            tokens = ensure_push_monitors(nodes)
        except Exception as exc:
            log(f"check loop error: {type(exc).__name__}: {exc}")
        elapsed = time.monotonic() - started
        time.sleep(max(1, CHECK_INTERVAL_SECONDS - elapsed))


if __name__ == "__main__":
    sys.exit(main())
