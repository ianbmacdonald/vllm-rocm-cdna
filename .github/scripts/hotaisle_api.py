#!/usr/bin/env python3
"""hotaisle_api.py — minimal stdlib client for the Hot Aisle metered-GPU API (vendored).

Vendored INTO this skill so the canonical capability has NO cross-repo runtime dependency:
the lemonade original `sys.path.insert`-ed nsp's httpx client from a sibling clone — a
hardcoded-clone-path loader (the promote-lint class-1 defect) that breaks on any machine
without that exact checkout. Endpoints and response shapes mirror
nsp `src/nsp_ai/services/hotaisle_client.py` (the field source of truth; compatibility
contract tracked as ai_support#B247).

VMs are plain DICTS here, deliberately: the original's VMInfo dataclass had `vm_id` while
call sites wrote `vm.id` — one such slip crashed AFTER provisioning with the state file
unwritten (a billing box with no durable record), and a second lived in the over-allocation
guard's deprovision call, where a crash leaves an over-allocated box billing. Dict access
with explicit keys makes that attribute-name class unrepresentable.

Auth: Bearer $HOTAISLE_API_KEY (source it per-invocation, e.g.
`HOTAISLE_API_KEY="$(op read op://<vault>/<item>/credential)"` — never hardcode a ref
here). Team: $HOTAISLE_TEAM. Base: $HOTAISLE_API_BASE.
API docs: https://admin.hotaisle.app/api/docs/
"""
import json
import os
import urllib.error
import urllib.request

API_BASE = "https://admin.hotaisle.app/api"
DEFAULT_TEAM = "nz-team1"   # org-stable default (ai_support governance#R10; documented in SKILL.md)


class HotAisleError(RuntimeError):
    pass


def parse_vm(d: dict) -> dict:
    gpus = d.get("gpus", [])
    return {
        "vm_id": d.get("name", d.get("id", d.get("vm_id", ""))),
        "ip_address": d.get("ip_address", d.get("ip", "")),
        "ssh_user": d.get("ssh_user", "hotaisle"),
        "status": d.get("status", "unknown"),
        "cpu_cores": d.get("cpu_cores", 0),
        "gpu_count": len(gpus) if gpus else d.get("gpu_count", 0),
        "gpu_model": (gpus[0].get("model") if gpus else d.get("gpu_model")) or "unknown",
    }


class Client:
    def __init__(self, api_key=None, team=None, base_url=None):
        # arg > env, fail LOUD when absent — a metered-spend tool must never run blind.
        self.api_key = api_key or os.environ.get("HOTAISLE_API_KEY")
        if not self.api_key:
            raise HotAisleError("HOTAISLE_API_KEY not set — refusing to run blind")
        self.team = team or os.environ.get("HOTAISLE_TEAM") or DEFAULT_TEAM
        self.base = (base_url or os.environ.get("HOTAISLE_API_BASE") or API_BASE).rstrip("/")

    def _request(self, method: str, path: str, body=None):
        url = self.base + path
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            url, data=data, method=method,
            headers={"Authorization": f"Bearer {self.api_key}",
                     "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                raw = r.read()
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")[:300]
            raise HotAisleError(f"HTTP {e.code} {method} {path}: {detail}") from None
        except OSError as e:
            raise HotAisleError(f"{method} {path} failed: {e}") from None
        if not raw:
            return {}
        try:
            return json.loads(raw)
        except ValueError:
            raise HotAisleError(f"non-JSON response from {method} {path}: {raw[:200]!r}") from None

    def get_balance(self) -> dict:
        d = self._request("GET", f"/teams/{self.team}/balance/")
        return {
            "balance_usd": d.get("available_balance", d.get("balance", 0)) / 100.0,
            "hourly_burn_usd": d.get("hourly_rate", 0) / 100.0,
        }

    def list_available_vms(self) -> list:
        d = self._request("GET", f"/teams/{self.team}/virtual_machines/available/")
        return d if isinstance(d, list) else d.get("items", [])

    def list_vms(self) -> list:
        d = self._request("GET", f"/teams/{self.team}/virtual_machines/")
        items = d if isinstance(d, list) else d.get("items", [])
        return [parse_vm(v) for v in items]

    def provision_raw(self, body: dict) -> dict:
        """POST the EXACT offer specs, never a hardcoded shape — provisioning a shape the
        provider did not offer is the Dec-2025 over-allocation class (nsp c006852)."""
        return parse_vm(self._request("POST", f"/teams/{self.team}/virtual_machines/", body=body))

    def deprovision_vm(self, vm_id: str) -> None:
        self._request("DELETE", f"/teams/{self.team}/virtual_machines/{vm_id}/")
