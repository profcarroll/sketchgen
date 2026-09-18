"""``sketchgen billing``: what this node has actually cost, on the console.

Two halves on purpose, because only one machine can do each.

**Reading** the figure needs the OCI Usage API, and that needs
``~/.oci/oci_api_key.pem`` — a key that can create and destroy infrastructure.
It is not on the node and it is not going to be: the node serves a public
gallery and runs code written by a model. So the query runs on the operator's
machine.

**Recording** it needs the node's database and no credentials at all, so
``--record`` takes the reading on stdin and runs anywhere. ``--sync`` is the
two joined over ssh, which is the command worth running on a schedule.

What is stored is a day at a time, one row per service and SKU, in
``billing_usage`` (migration 013) — not a single number. A single number cannot
say whether the bill is moving, and on an Always Free tenancy the dollar figure
does not move until the day it suddenly does. The metered *quantity* moves
first, so it is kept next to the amount and the console card draws both.

**On tenancies.** A reading is stamped with the tenancy it came from, and
``--record`` refuses a reading from a tenancy other than the node's own unless
told otherwise. This is not hypothetical: on 2026-09-18 the operator's
``~/.oci/config`` was found to point at a different tenancy from the one the
node runs in, and the $0.00 the card had been showing was an unrelated
account's $0.00. The node learns its own tenancy from the instance metadata
service, which needs no credentials at all — that is ``--identify``.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sketchgen import db

EXIT_OK, EXIT_FAIL, EXIT_REFUSED = 0, 1, 3

#: The single-figure keys the card has always read. Still written, so a
#: database that has not been migrated to 013 keeps showing something.
KEY_AMOUNT = "billing_amount"
KEY_CURRENCY = "billing_currency"
KEY_THROUGH = "billing_through"      # end of the window the figure covers
KEY_CHECKED = "billing_checked_utc"  # when a person last asked
KEY_TENANCY = "billing_tenancy"      # which account the figure came from

OCI_CONFIG = Path("~/.oci/config").expanduser()
HELPER = Path("~/.oci/ocirest.sh").expanduser()

#: The instance metadata service. Link-local, node-only, no credentials, and
#: it answers in milliseconds or not at all — hence the short timeout.
IMDS = "http://169.254.169.254/opc/v2/instance/"
IMDS_TIMEOUT = 3.0

#: Where a reading lands on the node by default, matching docs/OPERATIONS.md.
DEFAULT_SSH_REMOTE = (
    "~/sketchgen/.venv/bin/python3 ~/sketchgen/app/bin/sketchgen"
)


def _config_value(key: str) -> str | None:
    try:
        for line in OCI_CONFIG.read_text(encoding="utf-8").splitlines():
            if line.startswith(f"{key}="):
                return line.split("=", 1)[1].strip()
    except OSError:
        return None
    return None


# ---------------------------------------------------------------------------
# The node, asking itself who it is
# ---------------------------------------------------------------------------


def node_identity() -> dict[str, str]:
    """Tenancy, instance and shape, from the metadata service.

    Every field here is readable by anything running on the instance and by
    nothing off it, which is exactly the right shape for this: it identifies
    the machine without carrying any authority to change it. Off the node the
    address does not answer and this raises.
    """
    request = urllib.request.Request(IMDS, headers={"Authorization": "Bearer Oracle"})
    try:
        with urllib.request.urlopen(request, timeout=IMDS_TIMEOUT) as response:
            data = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError, TimeoutError) as exc:
        raise RuntimeError(
            f"the instance metadata service did not answer ({exc}) — "
            "--identify runs on the node and nowhere else"
        ) from exc
    shape_config = data.get("shapeConfig") or {}
    return {
        "node_tenancy": str(data.get("tenantId") or ""),
        "node_instance_id": str(data.get("id") or ""),
        "node_shape": str(data.get("shape") or ""),
        "node_ocpus": str(shape_config.get("ocpus") or ""),
        "node_memory_gb": str(shape_config.get("memoryInGBs") or ""),
        "node_identified_utc": db.utc_now(),
    }


# ---------------------------------------------------------------------------
# The Usage API
# ---------------------------------------------------------------------------


def _usage_call(body: dict) -> dict:
    """POST one query to the Usage API through ``~/.oci/ocirest.sh``.

    The helper signs for the IaaS host; usage lives on another, so the host is
    overridden through the environment rather than the script being edited —
    it is the operator's tool, not this project's.
    """
    region = _config_value("region")
    if not region:
        raise RuntimeError(f"no region in {OCI_CONFIG}")
    if not HELPER.is_file():
        raise RuntimeError(f"no OCI helper at {HELPER}")
    path = Path(os.environ.get("TMPDIR", "/tmp")) / "sketchgen-usage.json"
    path.write_text(json.dumps(body), encoding="utf-8")
    env = dict(os.environ)
    env["OCI_HOST"] = f"usageapi.{region}.oci.oraclecloud.com"
    # The helper hardcodes the IaaS host; a copy with it parameterised is what
    # OCI_HOST reads, so this asks the helper for the one thing it cannot do.
    script = (
        HELPER.read_text(encoding="utf-8")
        .replace('HOST="iaas.${REGION}.oraclecloud.com"',
                 'HOST="${OCI_HOST:-iaas.${REGION}.oraclecloud.com}"')
    )
    run = subprocess.run(
        ["bash", "-s", "POST", "/20200107/usage", str(path)],
        input=script, capture_output=True, text=True, env=env, check=False,
    )
    if run.returncode != 0:
        raise RuntimeError(f"the OCI call failed: {run.stderr.strip()[:200]}")
    text, status = _split_status(run.stdout)
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise RuntimeError(f"the OCI reply was not JSON: {text[:160]}") from exc
    # A refusal is JSON too, and it has no "items" — so without this check a
    # 401 reads as a tenancy that used nothing, which is the exact lie this
    # whole card exists to stop telling. On 2026-09-18 it told it once.
    if status is not None and not 200 <= status < 300:
        code = str(data.get("code") or f"HTTP {status}")
        message = str(data.get("message") or "")[:200]
        hint = ""
        if status in (401, 404):
            hint = (" — check user, fingerprint, key_file and tenancy in "
                    f"{OCI_CONFIG}, and that the user may 'read usage-report "
                    "in tenancy'")
        raise RuntimeError(f"the Usage API refused the query: {code}. "
                           f"{message}{hint}")
    return data


def _split_status(stdout: str) -> tuple[str, int | None]:
    """The body and the HTTP status out of the helper's ``__HTTP_nnn__`` tail.

    ``ocirest.sh`` prints the response then that marker. Reading only the body
    and throwing the marker away is how a refusal passes for an answer.
    """
    body, _, tail = stdout.partition("__HTTP_")
    digits = tail.split("__", 1)[0].strip()
    return body.strip(), int(digits) if digits.isdigit() else None


def _clean_currency(value: object) -> str | None:
    """A currency worth storing, or nothing.

    A grouped query answers "NA" on the rows it aggregates, and the USAGE
    query answers null. Only a real three-letter code means anything.
    """
    text = str(value or "")
    return text if len(text) == 3 and text.isalpha() and text != "NA" else None


def fetch(start: str, end: str, granularity: str = "DAILY") -> dict:
    """Every metered row between two timestamps, quantity and amount together.

    Two calls, because one will not do it. ``queryType=USAGE`` returns the
    quantity with its unit and no currency; ``queryType=COST`` returns the
    amount with its currency and a null unit. The same (day, service, SKU)
    identifies a row in both, so they are joined on it here and the console
    never has to know there were two.
    """
    tenancy = _config_value("tenancy")
    if not tenancy:
        raise RuntimeError(f"no tenancy in {OCI_CONFIG}")
    base = {
        "tenantId": tenancy,
        "timeUsageStarted": start,
        "timeUsageEnded": end,
        "granularity": granularity,
        "groupBy": ["service", "skuPartNumber", "skuName"],
    }
    merged: dict[tuple[str, str, str], dict] = {}
    for query_type in ("USAGE", "COST"):
        for item in _usage_call({**base, "queryType": query_type}).get("items") or []:
            day = str(item.get("timeUsageStarted") or "")[:10]
            if not day:
                continue
            key = (day, str(item.get("service") or "?"),
                   str(item.get("skuPartNumber") or "?"))
            row = merged.setdefault(key, {
                "day": day, "service": key[1], "sku": key[2],
                "sku_name": None, "quantity": 0.0, "unit": None,
                "amount": 0.0, "currency": None,
            })
            row["sku_name"] = item.get("skuName") or row["sku_name"]
            if query_type == "USAGE":
                row["quantity"] = float(item.get("computedQuantity") or 0.0)
                row["unit"] = item.get("unit") or row["unit"]
            else:
                row["amount"] = float(item.get("computedAmount") or 0.0)
                row["currency"] = _clean_currency(item.get("currency")) or row["currency"]
    rows = [merged[key] for key in sorted(merged)]
    currency = next((r["currency"] for r in rows if r["currency"]), "USD")
    return {
        "tenancy": tenancy,
        "start": start,
        "end": end,
        "currency": currency,
        "fetched_utc": db.utc_now(),
        "rows": rows,
    }


def totals(payload: dict) -> dict:
    """The reading collapsed to what a person reads first."""
    rows = payload.get("rows") or []
    services: dict[str, float] = {}
    for row in rows:
        services[row["service"]] = services.get(row["service"], 0.0) + float(
            row.get("amount") or 0.0
        )
    return {
        "amount": sum(float(r.get("amount") or 0.0) for r in rows),
        "ocpu_hours": sum(float(r.get("quantity") or 0.0) for r in rows
                          if r.get("unit") == "OCPU Per Hour"),
        "gb_hours": sum(float(r.get("quantity") or 0.0) for r in rows
                        if r.get("unit") == "Gigabyte Per Hour"),
        "services": services,
        "days": sorted({r["day"] for r in rows}),
    }


# ---------------------------------------------------------------------------
# The subcommand
# ---------------------------------------------------------------------------


def _open_db(path_text: str) -> tuple[object, Path] | None:
    database = Path(path_text).expanduser()
    if not database.is_file():
        print(f"refused: no database at {database}", file=sys.stderr)
        return None
    return db.connect(database), database


def _do_identify(args: argparse.Namespace) -> int:
    try:
        identity = node_identity()
    except RuntimeError as exc:
        print(f"failed: {exc}", file=sys.stderr)
        return EXIT_FAIL
    opened = _open_db(args.db)
    if opened is None:
        return EXIT_REFUSED
    conn, _ = opened
    try:
        for key in db.NODE_IDENTITY_KEYS:
            db.set_meta(conn, key, identity.get(key, ""))
        conn.commit()
    finally:
        conn.close()
    print(f"identified: {identity['node_shape']} "
          f"{identity['node_ocpus']} OCPU / {identity['node_memory_gb']} GB")
    print(f"  tenancy  {identity['node_tenancy']}")
    print(f"  instance {identity['node_instance_id']}")
    return EXIT_OK


def _do_record(args: argparse.Namespace) -> int:
    """Write a reading into the database. No credentials, so it runs anywhere."""
    if args.from_json:
        text = (sys.stdin.read() if args.from_json == "-"
                else Path(args.from_json).expanduser().read_text(encoding="utf-8"))
        try:
            payload = json.loads(text)
        except ValueError as exc:
            print(f"refused: the reading is not JSON ({exc})", file=sys.stderr)
            return EXIT_REFUSED
    elif args.amount is not None:
        payload = {
            "tenancy": args.tenancy or "",
            "currency": args.currency,
            "rows": [],
            "fetched_utc": db.utc_now(),
            "amount_only": float(args.amount),
        }
    else:
        print("refused: --record needs --from-json or --amount", file=sys.stderr)
        return EXIT_REFUSED

    opened = _open_db(args.db)
    if opened is None:
        return EXIT_REFUSED
    conn, _ = opened
    try:
        expected = db.get_meta(conn, "node_tenancy") or ""
        found = str(payload.get("tenancy") or "")
        if expected and found and expected != found and not args.force:
            print(
                "refused: this reading is from a different tenancy than the node.\n"
                f"  node reports    {expected}\n"
                f"  reading is from {found}\n"
                "The figure would be some other account's. Point ~/.oci/config at "
                "the node's tenancy, or pass --force if you mean it.",
                file=sys.stderr,
            )
            return EXIT_REFUSED
        if not expected:
            print("note: the node has not identified itself; run "
                  "`sketchgen billing --identify` on it so readings can be "
                  "checked against the tenancy it actually runs in.",
                  file=sys.stderr)

        rows = payload.get("rows") or []
        written = db.record_billing(conn, found or expected or "unknown", rows,
                                   payload.get("fetched_utc"))
        if "amount_only" in payload:
            amount = float(payload["amount_only"])
        else:
            amount = sum(float(r.get("amount") or 0.0) for r in rows)
        through = args.through or (
            max((str(r["day"]) for r in rows), default="") if rows else ""
        )
        db.set_meta(conn, KEY_AMOUNT, f"{amount:.2f}")
        db.set_meta(conn, KEY_CURRENCY, payload.get("currency") or args.currency)
        db.set_meta(conn, KEY_THROUGH, through)
        db.set_meta(conn, KEY_CHECKED, db.utc_now())
        db.set_meta(conn, KEY_TENANCY, found or expected or "")
        conn.commit()
    finally:
        conn.close()
    print(f"recorded: {amount:.2f} {payload.get('currency') or args.currency}"
          f"{f' through {through}' if through else ''}, {written} daily rows")
    return EXIT_OK


def _do_sync(args: argparse.Namespace, payload: dict) -> int:
    """Ship a reading to the node over ssh and record it there."""
    remote = (f"{args.remote} billing --record --from-json - "
              f"--db {args.remote_db}")
    if args.force:
        remote += " --force"
    run = subprocess.run(
        ["ssh", args.sync, remote],
        input=json.dumps(payload), capture_output=True, text=True, check=False,
    )
    sys.stdout.write(run.stdout)
    if run.stderr:
        sys.stderr.write(run.stderr)
    if run.returncode != 0:
        print(f"failed: the node refused the reading (exit {run.returncode})",
              file=sys.stderr)
        return EXIT_FAIL
    return EXIT_OK


def cmd(args: argparse.Namespace) -> int:
    if args.identify:
        return _do_identify(args)
    if args.record:
        return _do_record(args)

    now = datetime.now(timezone.utc)
    if args.since:
        start = args.since
    elif args.days:
        start = (now - timedelta(days=args.days)).strftime("%Y-%m-%dT00:00:00Z")
    else:
        start = now.strftime("%Y-%m-01T00:00:00Z")
    # Oracle's window is half-open and the current day is always partial, so
    # asking through tomorrow is what gets today's hours so far.
    end = args.until or (now + timedelta(days=1)).strftime("%Y-%m-%dT00:00:00Z")

    try:
        payload = fetch(start, end)
    except RuntimeError as exc:
        print(f"failed: {exc}", file=sys.stderr)
        return EXIT_FAIL

    if args.sync:
        return _do_sync(args, payload)
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return EXIT_OK

    summary = totals(payload)
    if args.amount_only:
        print(f"{summary['amount']:.2f}")
        return EXIT_OK

    print(f"{start} -> {end}")
    print(f"tenancy {payload['tenancy']}")
    print()
    for service, cost in sorted(summary["services"].items()):
        print(f"  {service:28} {cost:>10.2f} {payload['currency']}")
    print(f"  {'TOTAL':28} {summary['amount']:>10.2f} {payload['currency']}")
    print()
    span = len(summary["days"]) or 1
    print(f"  metered over {span} day(s): "
          f"{summary['ocpu_hours']:.0f} OCPU-hours, "
          f"{summary['gb_hours']:.0f} GB-hours")
    print(f"  which is {summary['ocpu_hours'] / (span * 24):.2f} OCPU and "
          f"{summary['gb_hours'] / (span * 24):.1f} GB held on average")
    print()
    print("to put this on the console, record it on the node:")
    print(f"  python3 bin/sketchgen billing --sync <host>")
    return EXIT_OK


def register(top: argparse._SubParsersAction) -> None:
    p = top.add_parser(
        "billing",
        help="what this tenancy has cost, and record it for the console card",
        description=(
            "Without a flag: asks the OCI Usage API what the tenancy has cost "
            "this month, day by day, and prints it by service. Needs ~/.oci, so "
            "it runs on the operator's machine and never on the node. --sync "
            "does that and ships the result to the node in one command. "
            "--record writes a reading handed to it, which needs no credentials "
            "and so runs anywhere. --identify runs on the node and records "
            "which tenancy and shape it actually is, so a reading from the "
            "wrong account can be refused instead of shown."
        ),
    )
    p.add_argument("--identify", action="store_true",
                   help="on the node: record its tenancy and shape from the "
                        "instance metadata service (no credentials needed)")
    p.add_argument("--record", action="store_true",
                   help="write a reading into the database instead of querying OCI")
    p.add_argument("--from-json", dest="from_json", default=None, metavar="P",
                   help="the reading to record, as a file or '-' for stdin")
    p.add_argument("--sync", default=None, metavar="HOST",
                   help="query OCI here, then record the result on HOST over ssh")
    p.add_argument("--remote", default=DEFAULT_SSH_REMOTE, metavar="CMD",
                   help="the sketchgen command on the far side of --sync")
    p.add_argument("--remote-db", dest="remote_db",
                   default="~/sketchgen/sketchgen.db", metavar="P",
                   help="the database on the far side of --sync")
    p.add_argument("--force", action="store_true",
                   help="record a reading even though it is from another tenancy")
    p.add_argument("--amount", type=float, default=None, metavar="N",
                   help="record just this total, with no daily rows")
    p.add_argument("--tenancy", default=None, metavar="OCID",
                   help="the tenancy --amount came from")
    p.add_argument("--currency", default="USD", metavar="C")
    p.add_argument("--through", default=None, metavar="DATE",
                   help="the end of the window the figure covers, as 2026-09-17")
    p.add_argument("--json", action="store_true",
                   help="print the reading as JSON instead of a table")
    p.add_argument("--amount-only", dest="amount_only", action="store_true",
                   help="print just the total, for a shell to capture")
    p.add_argument("--days", type=int, default=None, metavar="N",
                   help="query the last N days instead of this month")
    p.add_argument("--since", default=None, metavar="TS")
    p.add_argument("--until", default=None, metavar="TS")
    p.add_argument("--db", default=db.DEFAULT_DB_PATH, metavar="P",
                   help="database file (default: $SKETCHGEN_DB, else "
                        "~/sketchgen/sketchgen.db)")
    p.set_defaults(func=cmd, _parser=p)
