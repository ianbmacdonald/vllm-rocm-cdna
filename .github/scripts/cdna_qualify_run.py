#!/usr/bin/env python3
"""Provision one CDNA box, run a qualification step, tear it down, and MEASURE what it cost.

This is the paid half of the CDNA CI MVP. Its job is to produce a real cost number, because
every figure in the plan so far is arithmetic on an hourly rate rather than an observation.

THE SAFETY MODEL, and why it is shaped like this. A read-only probe established that the
provider exposes NONE of the three rails the design wanted:

    tagging            absent -> the sweeper cannot identify its own boxes by tag
    auto-terminate     absent -> the provider will not kill the box for us
    server-side budget absent -> no ceiling survives total client-side failure

So the rails have to be built where they still work:

  1. AN IN-GUEST DEAD MAN'S SWITCH, set at boot via user-data. `shutdown -h` at +N minutes.
     This is the only defence that survives the orchestrator dying, GitHub cancelling the job,
     or this process being killed. It is first because it is the one that cannot be bypassed.
  2. A DURABLE STATE FILE written the instant a VM id exists, before anything else can fail.
     The 2026-07-24 incident was recoverable only because such a file existed.
  3. TEARDOWN WITH RETRIES, then VERIFICATION AGAINST THE PROVIDER API. "Deprovisioned" is a
     claim; list_vms()==0 and burn==0 is evidence. Never assert it from a terminate call.
  4. A HARD WALL-CLOCK CAP in this process, well under the GitHub job timeout.

COST IS MEASURED FROM THE BALANCE DELTA, not derived from the nominal hourly rate: the
provider's own VMInfo.hourly_rate_cents has been observed reporting 1.99 against an actual
2.99/hr burn, so the rate field is not trustworthy for accounting.
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from hotaisle_api import Client  # noqa: E402

STATE_FILE = os.environ.get("CDNA_STATE_FILE", "/tmp/cdna-run-state.json")
DEADMAN_MIN = int(os.environ.get("CDNA_DEADMAN_MINUTES", "45"))
HARD_CAP_S = int(os.environ.get("CDNA_HARD_CAP_SECONDS", "2400"))   # 40 min
MAX_RATE = float(os.environ.get("CDNA_MAX_RATE_USD", "5.00"))
BOOT_WAIT_S = int(os.environ.get("CDNA_BOOT_WAIT_SECONDS", "900"))
DRY_RUN = os.environ.get("CDNA_DRY_RUN", "") not in ("", "0", "false")

# GPU-SHAPE POLICY. The qualification needs ONE card, but capacity is whatever the provider
# happens to be offering, and price scales with GPU count. Combined with the 60-minute minimum
# reservation, taking a 2x box when a 1x would do doubles the FLOOR cost of every run.
#   CDNA_MAX_GPUS            preferred ceiling on GPU count (default 1)
#   CDNA_WAIT_MINUTES        how long to poll for a conforming offer before giving up (default 0)
#   CDNA_ACCEPT_LARGER       if the wait expires, take a bigger box anyway (default off)
# The three together express "just wait for a 1x", "take whatever is there", or a bounded
# compromise, without anyone editing the script.
MAX_GPUS = int(os.environ.get("CDNA_MAX_GPUS", "1"))
WAIT_MINUTES = float(os.environ.get("CDNA_WAIT_MINUTES", "0"))
ACCEPT_LARGER = os.environ.get("CDNA_ACCEPT_LARGER", "") not in ("", "0", "false")
POLL_SECONDS = int(os.environ.get("CDNA_POLL_SECONDS", "60"))

# The dead man's switch is passed as `user_data_url` — a URL the provider FETCHES — not as
# inline user-data. This distinction is load-bearing: the API SILENTLY DROPS unknown fields,
# so a body carrying `user_data` (or the older `cloud_init_url`) is accepted, has no effect,
# and leaves the box with NO kill timer while the run reports success. That failure is
# invisible by construction, which is why the field name is pinned here with a comment.
DEADMAN_URL = os.environ.get(
    "CDNA_DEADMAN_URL",
    "https://raw.githubusercontent.com/ianbmacdonald/vllm-rocm-cdna/"
    "feat/cdna-ci-preflight/.github/cloud-init/deadman.yaml",
)


def log(msg: str) -> None:
    print(f"[cdna-run] {msg}", flush=True)


def summary(md: str) -> None:
    print(md, flush=True)
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a") as fh:
            fh.write(md + "\n")


def write_state(**kw) -> None:
    """Durable the instant a vm id exists — this is what makes a crashed run recoverable."""
    try:
        state = {}
        if os.path.exists(STATE_FILE):
            state = json.load(open(STATE_FILE))
        state.update(kw)
        with open(STATE_FILE, "w") as fh:
            json.dump(state, fh, indent=1)
    except Exception as exc:                       # noqa: BLE001
        log(f"WARNING: could not write state file: {exc}")


def teardown(client: Client, vm_id: str) -> dict:
    """Destroy, then PROVE it. Retries because a transient API error must not leak a box."""
    last_err = None
    for attempt in range(1, 4):
        try:
            client.deprovision_vm(vm_id)
            log(f"deprovision call accepted (attempt {attempt})")
            break
        except Exception as exc:                   # noqa: BLE001
            last_err = exc
            log(f"deprovision attempt {attempt} failed: {type(exc).__name__}: {exc}")
            time.sleep(5 * attempt)
    else:
        log(f"ALL DEPROVISION ATTEMPTS FAILED: {last_err}")

    verified = {"list_vms": None, "burn": None, "ok": False}
    for attempt in range(1, 7):                    # the list can lag the delete
        time.sleep(10)
        try:
            vms = [v for v in client.list_vms() if v.get("vm_id") == vm_id]
            bal = client.get_balance()
            verified["list_vms"] = len(vms)
            verified["burn"] = bal.get("hourly_burn_usd", None)
            if not vms and (verified["burn"] or 0) == 0:
                verified["ok"] = True
                log(f"teardown VERIFIED against the API (attempt {attempt}): vm gone, burn $0.00/hr")
                break
            log(f"teardown not yet verified (attempt {attempt}): "
                f"matching_vms={len(vms)} burn=${verified['burn']}")
        except Exception as exc:                   # noqa: BLE001
            log(f"verification attempt {attempt} errored: {type(exc).__name__}: {exc}")
    write_state(teardown=verified)
    return verified


def main() -> int:
    if not os.getenv("HOTAISLE_API_KEY"):
        log("no credential — refusing to run blind")
        return 1

    # POSITIVE CONTROL ON THE ONLY RAIL THAT SURVIVES EVERYTHING. The provider FETCHES this
    # URL; if it 404s the box boots with no kill timer and nothing anywhere reports a problem.
    # Observed 2026-08-11: raw.githubusercontent.com served 404 for ~18s after the push before
    # the CDN caught up, which is exactly the window in which a run would arm nothing.
    import urllib.request
    try:
        with urllib.request.urlopen(DEADMAN_URL, timeout=30) as resp:
            body_txt = resp.read().decode("utf-8", "replace")
        if resp.status != 200 or "shutdown -h" not in body_txt:
            log(f"REFUSING TO PROVISION: dead man's switch URL returned {resp.status} or lacks "
                f"a shutdown directive. Arming nothing would leave a box with no kill timer.")
            return 1
        log(f"dead man's switch verified fetchable ({len(body_txt)} bytes, shutdown directive present)")
    except Exception as exc:                       # noqa: BLE001
        log(f"REFUSING TO PROVISION: cannot fetch the dead man's switch URL: "
            f"{type(exc).__name__}: {exc}")
        return 1

    client = Client()
    t_start = time.monotonic()

    bal0 = client.get_balance()
    start_balance = bal0.get("balance_usd", 0.0)
    log(f"pre-run burn ${bal0.get('hourly_burn_usd', 0):.2f}/hr")

    pre_existing = client.list_vms()
    if pre_existing:
        log(f"REFUSING TO START: {len(pre_existing)} VM(s) already running. "
            "A second box would risk the tenant cap and muddy the cost delta.")
        return 1

    def gpu_count(o):
        return sum(g.get("count", 0) for g in o.get("Specs", {}).get("gpus", []))

    def poll_for_offer():
        """Wait, bounded, for an offer at or under MAX_GPUS. Polling costs nothing: capacity is
        free to look at and only provisioning bills. Returns (offer, waited_seconds, settled)."""
        t0 = time.monotonic()
        deadline = t0 + WAIT_MINUTES * 60
        while True:
            offs = client.list_available_vms()
            fitting = [o for o in offs if gpu_count(o) and gpu_count(o) <= MAX_GPUS]
            if fitting:
                return sorted(fitting, key=gpu_count)[0], time.monotonic() - t0, True
            if time.monotonic() >= deadline:
                shapes = sorted({gpu_count(o) for o in offs}) if offs else []
                log(f"waited {(time.monotonic()-t0)/60:.1f} min; no offer at <= {MAX_GPUS} GPU"
                    f"(s). Shapes on offer: {shapes or 'none'}")
                if ACCEPT_LARGER and offs:
                    bigger = sorted(offs, key=gpu_count)[0]
                    log(f"CDNA_ACCEPT_LARGER is set — taking a {gpu_count(bigger)}x box instead")
                    return bigger, time.monotonic() - t0, False
                return None, time.monotonic() - t0, False
            log(f"no offer at <= {MAX_GPUS} GPU(s) yet; re-checking in {POLL_SECONDS}s "
                f"({(deadline - time.monotonic())/60:.1f} min of wait left)")
            time.sleep(POLL_SECONDS)

    offer, waited_s, exact = poll_for_offer()
    if offer is None:
        log("no acceptable capacity — exiting without spending")
        summary(f"### Paid run skipped\n\nNo offer at or under {MAX_GPUS} GPU(s) after "
                f"{waited_s/60:.1f} min of waiting. **$0 spent.** Set `CDNA_ACCEPT_LARGER=1` "
                f"to take a bigger box, or raise `CDNA_WAIT_MINUTES`.")
        return 0
    if not exact:
        log("NOTE: this box is larger than needed, so the floor cost per run is higher than "
            "the 1x case. Recorded so the cost table is not read as the 1x number.")
    offers = [offer]

    # Take the offer EXACTLY as given. Provisioning a shape the provider did not offer is
    # the documented over-allocation class.
    # FIELD NAMES ARE MEASURED, NOT GUESSED. The offer payload is
    #   {Quantity, OnDemandPrice (CENTS), MinimumReservationMinutes, Specs{gpus:[{count,model}]}}
    # An earlier version read `hourly_rate`/`price`, which are absent, so the rate parsed as
    # 0.00 and the spend cap could never fire. A guard that reads zero is not a guard.
    offer = offers[0]
    rate = float(offer.get("OnDemandPrice", 0) or 0) / 100.0
    gpu_n = gpu_count(offer)
    min_minutes = int(offer.get("MinimumReservationMinutes", 0) or 0)
    floor_cost = rate * (min_minutes / 60.0) if min_minutes else 0.0

    log(f"best offer: {gpu_n}x GPU at ${rate:.2f}/hr, minimum reservation {min_minutes} min")
    if min_minutes:
        log(f"MINIMUM RESERVATION IS BILLED REGARDLESS OF RUN LENGTH -> floor cost "
            f"${floor_cost:.2f} per run. Shortening the box below {min_minutes} min saves NOTHING.")
    if rate > MAX_RATE:
        log(f"REFUSING: offered rate ${rate:.2f}/hr exceeds cap ${MAX_RATE:.2f}/hr "
            f"(raise CDNA_MAX_RATE_USD deliberately if this is intended)")
        return 1

    body = dict(offer)
    body["user_data_url"] = DEADMAN_URL            # dead man's switch, set BEFORE first boot
    log(f"provisioning at ~${rate:.2f}/hr; kill timer via user_data_url={DEADMAN_URL}")
    log("NOTE: a box provisioned WITH user-data reboots and is not reachable until cloud-init "
        "finishes, so this costs some wall clock. That is the price of the only rail that "
        "survives total orchestrator failure.")

    if DRY_RUN:
        log("DRY RUN — would POST the offer above with user_data_url attached. Provisioning NOTHING.")
        summary("### Dry run\n\nValidated the offer shape and the dead man's switch URL. "
                "No VM provisioned, $0 spent.")
        return 0

    # WRITE THE INTENT BEFORE THE CALL, NOT AFTER IT. Measured 20260811: the provision POST
    # TIMED OUT client-side and SUCCEEDED server-side. The exception propagated out of
    # provision_raw before write_state() ran, so a real billing VM existed with NO receipt --
    # an orphan recoverable only by querying the provider. A timeout is INDISTINGUISHABLE from
    # a failure, and the box may exist either way, so the record has to predate the risk.
    write_state(intent="provision", intent_at=time.time(), start_balance=start_balance,
                deadman_url=DEADMAN_URL, vm_id=None,
                note="if vm_id is null and this file exists, a provision was ATTEMPTED: "
                     "query the provider API for orphans before assuming nothing was created")
    try:
        vm = client.provision_raw(body)
    except Exception as exc:                       # noqa: BLE001
        # Do NOT re-raise blind. The request may have succeeded; reconcile against the API.
        log(f"provision call FAILED CLIENT-SIDE: {type(exc).__name__}: {exc}")
        log("the request may still have SUCCEEDED server-side — reconciling against the API")
        write_state(provision_error=f"{type(exc).__name__}: {exc}")
        time.sleep(15)
        try:
            candidates = client.list_vms()
        except Exception as exc2:                  # noqa: BLE001
            log(f"RECONCILE FAILED TOO ({type(exc2).__name__}: {exc2}). A VM MAY BE BILLING "
                f"AND UNTRACKED. Check the provider API by hand before doing anything else.")
            return 3
        if not candidates:
            log("reconciled: no VM exists, nothing was created, nothing is billing")
            return 1
        vm = candidates[0]
        log(f"reconciled: the provision DID succeed — adopting orphan {vm.get('vm_id')}")
        write_state(recovered_from_timeout=True)

    vm_id = vm.get("vm_id")
    write_state(vm_id=vm_id, provisioned_at=time.time(), deadman_minutes=DEADMAN_MIN)
    log(f"provisioned vm_id={vm_id} (state file: {STATE_FILE})")

    workload = {"ran": False, "detail": "not attempted"}
    try:
        deadline = t_start + HARD_CAP_S
        log(f"boot wait, up to {BOOT_WAIT_S}s")
        booted = False
        while time.monotonic() < min(deadline, t_start + BOOT_WAIT_S):
            time.sleep(20)
            cur = [v for v in client.list_vms() if v.get("vm_id") == vm_id]
            if cur and str(cur[0].get("state", "")).lower() in ("running", "active", "ready"):
                booted = True
                log(f"vm reports state={cur[0].get('state')} after {time.monotonic()-t_start:.0f}s")
                break
        workload = {"ran": booted,
                    "detail": "reached running state" if booted else "never reached running state"}
    except Exception as exc:                       # noqa: BLE001
        workload = {"ran": False, "detail": f"{type(exc).__name__}: {exc}"}
        log(f"workload phase errored: {workload['detail']}")
    finally:
        log("tearing down")
        verified = teardown(client, vm_id)

    time.sleep(20)                                 # let the balance settle before reading it
    bal1 = client.get_balance()
    end_balance = bal1.get("balance_usd", 0.0)
    delta = round(start_balance - end_balance, 4)
    elapsed = time.monotonic() - t_start
    write_state(end_balance=end_balance, delta_usd=delta, elapsed_s=round(elapsed, 1))

    summary("### CDNA CI — first paid run, measured\n")
    summary(f"- wall clock: **{elapsed/60:.1f} min**")
    summary(f"- offered shape: **{gpu_n}x GPU at ${rate:.2f}/hr**, minimum reservation **{min_minutes} min**")
    summary(f"- shape policy: max {MAX_GPUS} GPU(s), waited **{waited_s/60:.1f} min**, "
            f"got {'the requested shape' if exact else 'a LARGER box (cost is not the 1x figure)'}")
    summary(f"- floor cost imposed by the minimum reservation: **${floor_cost:.2f}**")
    summary(f"- **measured cost (balance delta): ${delta:.2f}**")
    summary(f"- implied effective rate: **${(delta/(elapsed/3600)) if elapsed > 0 else 0:.2f}/hr**")
    summary(f"- boot/workload: {workload['detail']}")
    summary(f"- teardown verified against provider API: **{verified['ok']}** "
            f"(burn now ${verified['burn']}/hr)")
    summary("\n_Cost is the account balance delta, not a figure derived from the nominal "
            "hourly rate — the provider's rate field has been observed disagreeing with actual burn._")

    if not verified["ok"]:
        log("TEARDOWN NOT VERIFIED — failing loudly so this cannot read as success")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
