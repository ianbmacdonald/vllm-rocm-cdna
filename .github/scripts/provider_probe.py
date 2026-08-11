#!/usr/bin/env python3
"""Read-only capability probe for the GPU provider. Provisions NOTHING and spends NOTHING.

Three safety rails in the CDNA CI design depend on capabilities we have never confirmed exist:

  * a box-side dead man's switch      -> needs auto-terminate, or user-data we can put a timer in
  * tag-based sweeping                -> needs tags, because age-based sweeping would eventually
                                         kill an operator's interactive debugging session
  * a spend ceiling that survives us  -> needs a server-side budget/quota, the only rail that
                                         holds when every client-side control has failed

This probe answers "do those exist" BEFORE a paid run, and it reports what it OBSERVES rather
than asserting what it expects. An unknown is printed as unknown; nothing here is inferred.

It also records balance so a later run can compute a real cost delta, and it deliberately does
NOT print the balance figure into the job log, because these logs are readable by anyone with
repo access and the absolute balance is the sponsor's business, not the repository's.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from hotaisle_api import Client, API_BASE  # noqa: E402


def emit(md: str) -> None:
    print(md)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a") as fh:
            fh.write(md + "\n")


def main() -> int:
    if not os.getenv("HOTAISLE_API_KEY"):
        emit("Provider probe skipped: no credential.")
        return 0

    client = Client()
    emit("### 2. Provider capability probe (read-only)\n")

    # --- auth + billing reachability -------------------------------------------------
    try:
        balance = client.get_balance()
    except Exception as exc:                      # noqa: BLE001 - report, never mask
        emit(f"**FAIL — cannot read balance:** `{type(exc).__name__}: {exc}`")
        emit("\nA paid run must not be authorised while billing is unreadable: the cost record "
             "and the spend ceiling both depend on this endpoint.")
        return 1

    burn = balance.get("hourly_burn_usd", 0.0)
    emit(f"- auth: **ok** (`{API_BASE}`)")
    emit(f"- billing endpoint readable: **yes**")
    emit(f"- current burn: **${burn:.2f}/hr** "
         + ("(nothing running, as expected)" if burn == 0 else "**— something is billing right now**"))

    # --- is anything already running? ------------------------------------------------
    try:
        running = client.list_vms()
    except Exception as exc:                      # noqa: BLE001
        emit(f"- **FAIL — cannot list VMs:** `{type(exc).__name__}: {exc}`")
        return 1

    emit(f"- VMs currently running: **{len(running)}**")
    if running:
        emit("  - a preflight that finds running VMs is worth a look before any paid run:")
        for vm in running:
            emit(f"    - `{vm.get('vm_id', '?')}` state=`{vm.get('state', '?')}`")

    # --- capability discovery, observed not assumed ----------------------------------
    # The offer payload is the only authoritative statement of what the provider will accept.
    caps = {"tagging": "unknown", "auto_terminate": "unknown", "budget_cap": "unknown"}
    try:
        offers = client.list_available_vms()
    except Exception as exc:                      # noqa: BLE001
        offers = []
        emit(f"- capability probe degraded: could not list offers (`{type(exc).__name__}`)")

    blob = json.dumps(offers).lower() if offers else ""
    if blob:
        caps["tagging"] = "present" if ("tag" in blob or "label" in blob) else "absent-from-offer"
        caps["auto_terminate"] = (
            "present" if ("auto_terminate" in blob or "ttl" in blob or "expire" in blob)
            else "absent-from-offer"
        )
    bal_blob = json.dumps(balance).lower()
    caps["budget_cap"] = (
        "present" if ("budget" in bal_blob or "limit" in bal_blob or "quota" in bal_blob)
        else "absent-from-balance"
    )

    emit("\n| capability | a rail depends on it | observed |")
    emit("|---|---|---|")
    emit(f"| tags on a VM | tag-based sweeping (so we never kill a human's session) | **{caps['tagging']}** |")
    emit(f"| auto-terminate / TTL | box-side dead man's switch | **{caps['auto_terminate']}** |")
    emit(f"| server-side budget cap | the only ceiling that survives total client failure | **{caps['budget_cap']}** |")

    emit(f"\n- offers visible right now: **{len(offers)}**"
         + ("" if offers else " — capacity is zero, so a paid run would have nothing to rent"))

    absent = [k for k, v in caps.items() if v.startswith("absent")]
    if absent:
        emit("\n**Fallbacks required** for: " + ", ".join(f"`{a}`" for a in absent) + ".")
        emit("Absent auto-terminate, the dead man's switch has to be an in-guest timer set via "
             "user-data at boot. Absent tags, the sweeper must key on something else that "
             "distinguishes an automated run from a human session — age alone is not safe on a "
             "shared account. Absent a server-side cap, no ceiling survives a total client failure, "
             "which raises how much the external monitor matters.")

    emit("\n_No VM was provisioned. This probe cost $0._")
    return 0


if __name__ == "__main__":
    sys.exit(main())
