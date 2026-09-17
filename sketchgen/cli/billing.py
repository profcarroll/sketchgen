"""``sketchgen billing``: what this node has actually cost, on the console.

Two halves on purpose, because only one machine can do each.

**Reading** the figure needs the OCI Usage API, and that needs
``~/.oci/oci_api_key.pem`` — a key that can create and destroy infrastructure.
It is not on the node and it is not going to be: the node serves a public
gallery and runs code written by a model. So ``--query`` runs on the operator's
machine.

**Recording** it needs the node's database and no credentials at all, so
``--record`` takes the numbers as arguments and runs anywhere. One line joins
them, and it is in docs/OPERATIONS.md.

The console card renders what was last recorded and says when — never a live
figure, never a guess. Oracle's usage data lags by a day or more anyway, so a
card claiming to be current would be lying twice over.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from sketchgen import db

EXIT_OK, EXIT_FAIL, EXIT_REFUSED = 0, 1, 3

#: Where the card reads from. `meta` is key/value, so these are the four keys.
KEY_AMOUNT = "billing_amount"
KEY_CURRENCY = "billing_currency"
KEY_THROUGH = "billing_through"      # end of the window the figure covers
KEY_CHECKED = "billing_checked_utc"  # when a person last asked

OCI_CONFIG = Path("~/.oci/config").expanduser()
HELPER = Path("~/.oci/ocirest.sh").expanduser()


def _config_value(key: str) -> str | None:
    try:
        for line in OCI_CONFIG.read_text(encoding="utf-8").splitlines():
            if line.startswith(f"{key}="):
                return line.split("=", 1)[1].strip()
    except OSError:
        return None
    return None


def query_oci(start: str, end: str) -> dict:
    """Month-to-date cost from the Usage API, through ``~/.oci/ocirest.sh``.

    The helper signs for the IaaS host; usage lives on another, so the host is
    overridden through the environment rather than the script being edited —
    it is the operator's tool, not this project's.
    """
    tenancy = _config_value("tenancy")
    region = _config_value("region")
    if not tenancy or not region:
        raise RuntimeError(f"no tenancy/region in {OCI_CONFIG}")
    if not HELPER.is_file():
        raise RuntimeError(f"no OCI helper at {HELPER}")
    body = {
        "tenantId": tenancy,
        "timeUsageStarted": start,
        "timeUsageEnded": end,
        "granularity": "MONTHLY",
        "queryType": "COST",
        "groupBy": ["service"],
    }
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
    text = run.stdout.split("__HTTP_")[0].strip()
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise RuntimeError(f"the OCI reply was not JSON: {text[:160]}") from exc
    items = data.get("items") or []
    total = sum(float(i.get("computedAmount") or 0) for i in items)
    # A grouped query returns "NA" on the rows it aggregates; only a real
    # three-letter code is worth showing on a card.
    currency = next(
        (str(i.get("currency")) for i in items
         if i.get("currency") and str(i.get("currency")).isalpha()
         and len(str(i.get("currency"))) == 3),
        "USD",
    )
    per_service: dict[str, float] = {}
    for item in items:
        per_service[str(item.get("service") or "?")] = per_service.get(
            str(item.get("service") or "?"), 0.0
        ) + float(item.get("computedAmount") or 0)
    return {"amount": total, "currency": currency, "services": per_service}


def cmd(args: argparse.Namespace) -> int:
    if args.record:
        if args.amount is None:
            print("refused: --record needs --amount", file=sys.stderr)
            return EXIT_REFUSED
        database = Path(args.db).expanduser()
        if not database.is_file():
            print(f"refused: no database at {database}", file=sys.stderr)
            return EXIT_REFUSED
        conn = db.connect(database)
        try:
            db.set_meta(conn, KEY_AMOUNT, f"{float(args.amount):.2f}")
            db.set_meta(conn, KEY_CURRENCY, args.currency)
            db.set_meta(conn, KEY_THROUGH, args.through or "")
            db.set_meta(conn, KEY_CHECKED, db.utc_now())
            conn.commit()
        finally:
            conn.close()
        print(f"recorded: {float(args.amount):.2f} {args.currency} "
              f"through {args.through or '—'}")
        return EXIT_OK

    now = datetime.now(timezone.utc)
    start = args.since or now.strftime("%Y-%m-01T00:00:00Z")
    end = args.until or now.strftime("%Y-%m-%dT00:00:00Z")
    try:
        result = query_oci(start, end)
    except RuntimeError as exc:
        print(f"failed: {exc}", file=sys.stderr)
        return EXIT_FAIL
    if args.amount_only:
        print(f"{result['amount']:.2f}")
        return EXIT_OK
    print(f"{start} -> {end}")
    for service, cost in sorted(result["services"].items()):
        print(f"  {service:28} {cost:>10.2f} {result['currency']}")
    print(f"  {'TOTAL':28} {result['amount']:>10.2f} {result['currency']}")
    print()
    print("to show it on the console, record it on the node — see "
          "docs/OPERATIONS.md, 'The billing card'")
    return EXIT_OK


def register(top: argparse._SubParsersAction) -> None:
    p = top.add_parser(
        "billing",
        help="what this tenancy has cost, and record it for the console card",
        description=(
            "Without --record: asks the OCI Usage API what the tenancy has cost "
            "this month and prints it by service. Needs ~/.oci, so it runs on "
            "the operator's machine and never on the node. With --record: "
            "writes a figure you pass it into the database for the console card "
            "to show, which needs no credentials and so runs anywhere."
        ),
    )
    p.add_argument("--record", action="store_true",
                   help="write --amount into the database instead of querying OCI")
    p.add_argument("--amount", type=float, default=None, metavar="N",
                   help="the figure to record")
    p.add_argument("--currency", default="USD", metavar="C")
    p.add_argument("--through", default=None, metavar="DATE",
                   help="the end of the window the figure covers, as 2026-09-17")
    p.add_argument("--amount-only", dest="amount_only", action="store_true",
                   help="print just the total, for a shell to capture")
    p.add_argument("--since", default=None, metavar="TS")
    p.add_argument("--until", default=None, metavar="TS")
    p.add_argument("--db", default=db.DEFAULT_DB_PATH, metavar="P",
                   help="database file (default: $SKETCHGEN_DB, else "
                        "~/sketchgen/sketchgen.db)")
    p.set_defaults(func=cmd, _parser=p)
