"""Unit tests for `sketchgen billing` — the meter, the guard, and the card.

Run:  python3 -m unittest tests.test_billing

No OCI call is made anywhere in here. :func:`sketchgen.cli.billing.fetch` is
driven with a stubbed ``_usage_call`` returning the two shapes Oracle actually
answers with — a USAGE query carrying the quantity and its unit, a COST query
carrying the amount and a currency of "NA" — because the whole reason fetch()
makes two calls is that neither answer is complete on its own, and a test that
stubbed one of them would not notice if the join broke.

The tenancy guard has its own case because it is the bug this packet exists
for: on 2026-09-18 the operator's ~/.oci/config was pointing at a different
tenancy from the node's, and the console had been showing an unrelated
account's $0.00 for weeks with nothing to say so.
"""

import io
import json
import re
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sketchgen import db, web  # noqa: E402
from sketchgen.cli import billing  # noqa: E402

NODE_TENANCY = "ocid1.tenancy.oc1..node"
OTHER_TENANCY = "ocid1.tenancy.oc1..someone-else"


def usage_item(day, service, sku, name, quantity, unit):
    return {
        "timeUsageStarted": f"{day}T00:00:00.000Z", "service": service,
        "skuPartNumber": sku, "skuName": name,
        "computedQuantity": quantity, "unit": unit,
        "computedAmount": 0, "currency": None,
    }


def cost_item(day, service, sku, name, amount, currency="USD"):
    # Oracle's COST answer: no unit, and "NA" wherever it aggregated rows.
    return {
        "timeUsageStarted": f"{day}T00:00:00.000Z", "service": service,
        "skuPartNumber": sku, "skuName": name,
        "computedQuantity": 0, "unit": None,
        "computedAmount": amount, "currency": currency,
    }


def rows_for(day, ocpu=96.0, gigabytes=576.0, amount=0.0):
    return [
        {"day": day, "service": "Compute", "sku": "B93297",
         "sku_name": "Standard - A1", "quantity": ocpu,
         "unit": "OCPU Per Hour", "amount": amount, "currency": "USD"},
        {"day": day, "service": "Compute", "sku": "B93298",
         "sku_name": "Standard - A1 - Memory", "quantity": gigabytes,
         "unit": "Gigabyte Per Hour", "amount": 0.0, "currency": "USD"},
    ]


class FetchTests(unittest.TestCase):
    """The two calls, joined on (day, service, SKU)."""

    def test_quantity_and_amount_arrive_from_different_calls(self):
        answers = {
            "USAGE": {"items": [
                usage_item("2026-09-15", "Compute", "B93297",
                           "Standard - A1", 96.0, "OCPU Per Hour"),
                usage_item("2026-09-15", "Compute", "B93298",
                           "Standard - A1 - Memory", 576.0, "Gigabyte Per Hour"),
            ]},
            "COST": {"items": [
                cost_item("2026-09-15", "Compute", "B93297", "Standard - A1", 1.25),
                cost_item("2026-09-15", "Compute", "B93298",
                          "Standard - A1 - Memory", 0.75),
            ]},
        }
        with mock.patch.object(billing, "_config_value",
                               side_effect=lambda k: {"tenancy": NODE_TENANCY,
                                                      "region": "us-ashburn-1"}[k]), \
             mock.patch.object(billing, "_usage_call",
                               side_effect=lambda body: answers[body["queryType"]]):
            payload = billing.fetch("2026-09-15T00:00:00Z", "2026-09-16T00:00:00Z")

        self.assertEqual(NODE_TENANCY, payload["tenancy"])
        self.assertEqual("USD", payload["currency"])
        by_sku = {row["sku"]: row for row in payload["rows"]}
        self.assertEqual(96.0, by_sku["B93297"]["quantity"])
        self.assertEqual("OCPU Per Hour", by_sku["B93297"]["unit"])
        self.assertEqual(1.25, by_sku["B93297"]["amount"])
        self.assertEqual(576.0, by_sku["B93298"]["quantity"])
        self.assertEqual(0.75, by_sku["B93298"]["amount"])

    def test_na_currency_is_not_a_currency(self):
        self.assertIsNone(billing._clean_currency("NA"))
        self.assertIsNone(billing._clean_currency(None))
        self.assertIsNone(billing._clean_currency("US"))
        self.assertEqual("USD", billing._clean_currency("USD"))

    def test_totals_read_the_units_not_the_skus(self):
        payload = {"rows": rows_for("2026-09-15", amount=2.0)}
        summary = billing.totals(payload)
        self.assertEqual(96.0, summary["ocpu_hours"])
        self.assertEqual(576.0, summary["gb_hours"])
        self.assertEqual(2.0, summary["amount"])
        self.assertEqual({"Compute": 2.0}, summary["services"])


class StatusTests(unittest.TestCase):
    """A refusal must not read as a tenancy that used nothing.

    This is the failure that actually happened: a 401 is valid JSON with no
    "items" in it, so a caller that reads the body and drops the status prints
    $0.00 and zero OCPU-hours with total confidence.
    """

    def helper_output(self, body, status):
        return f"{body}\n__HTTP_{status}__\n"

    def test_the_status_comes_off_the_tail(self):
        body, status = billing._split_status(self.helper_output('{"items": []}', 200))
        self.assertEqual('{"items": []}', body)
        self.assertEqual(200, status)

    def test_a_body_with_no_marker_has_no_status(self):
        body, status = billing._split_status('{"items": []}')
        self.assertEqual('{"items": []}', body)
        self.assertIsNone(status)

    def _call(self, body, status):
        run = SimpleNamespace(returncode=0, stdout=self.helper_output(body, status),
                              stderr="")
        with mock.patch.object(billing, "_config_value", return_value="us-ashburn-1"), \
             mock.patch.object(billing.Path, "is_file", return_value=True), \
             mock.patch.object(billing.Path, "read_text", return_value="HOST=x"), \
             mock.patch.object(billing.Path, "write_text", return_value=None), \
             mock.patch.object(billing.subprocess, "run", return_value=run):
            return billing._usage_call({"queryType": "COST"})

    def test_a_401_raises_instead_of_answering_nothing(self):
        refusal = json.dumps({
            "code": "NotAuthenticated",
            "message": "The required information to complete authentication "
                       "was not provided or was incorrect.",
        })
        with self.assertRaises(RuntimeError) as caught:
            self._call(refusal, 401)
        said = str(caught.exception)
        self.assertIn("NotAuthenticated", said)
        self.assertIn("read usage-report", said)

    def test_a_404_says_to_check_the_config_too(self):
        with self.assertRaises(RuntimeError) as caught:
            self._call(json.dumps({"code": "NotFound", "message": "nope"}), 404)
        self.assertIn("config", str(caught.exception))

    def test_a_200_is_returned_as_it_came(self):
        self.assertEqual({"items": []}, self._call('{"items": []}', 200))


class RecordTests(unittest.TestCase):
    """What lands in the database, and what is refused."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="sketchgen-billing-")
        self.db_path = Path(self._tmp.name) / "sketchgen.db"
        db.init(self.db_path)
        conn = db.connect(self.db_path)
        try:
            db.set_meta(conn, "node_tenancy", NODE_TENANCY)
            db.set_meta(conn, "node_ocpus", "16.0")
            db.set_meta(conn, "node_shape", "VM.Standard.A1.Flex")
            conn.commit()
        finally:
            conn.close()
        self.addCleanup(self._tmp.cleanup)

    def args(self, **over):
        base = dict(identify=False, record=True, from_json=None, sync=None,
                    remote="", remote_db="", force=False, amount=None,
                    tenancy=None, currency="USD", through=None, json=False,
                    amount_only=False, days=None, since=None, until=None,
                    db=str(self.db_path))
        base.update(over)
        return SimpleNamespace(**base)

    def record(self, payload, **over):
        path = Path(self._tmp.name) / "reading.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = billing.cmd(self.args(from_json=str(path), **over))
        return code, out.getvalue(), err.getvalue()

    def test_a_reading_from_the_node_s_tenancy_is_written(self):
        code, out, _ = self.record({
            "tenancy": NODE_TENANCY, "currency": "USD",
            "fetched_utc": "2026-09-18T12:00:00Z",
            "rows": rows_for("2026-09-17", amount=1.5),
        })
        self.assertEqual(billing.EXIT_OK, code)
        self.assertIn("2 daily rows", out)
        conn = db.connect(self.db_path)
        try:
            daily = db.billing_daily(conn, NODE_TENANCY)
            self.assertEqual(1, len(daily))
            self.assertEqual(96.0, daily[0]["ocpu_hours"])
            self.assertEqual(576.0, daily[0]["gb_hours"])
            self.assertAlmostEqual(1.5, daily[0]["amount"])
            self.assertEqual("1.50", db.get_meta(conn, billing.KEY_AMOUNT))
            self.assertEqual("2026-09-17", db.get_meta(conn, billing.KEY_THROUGH))
            self.assertEqual(NODE_TENANCY, db.get_meta(conn, billing.KEY_TENANCY))
        finally:
            conn.close()

    def test_a_reading_from_another_tenancy_is_refused(self):
        code, _, err = self.record({
            "tenancy": OTHER_TENANCY, "currency": "USD",
            "rows": rows_for("2026-09-17"),
        })
        self.assertEqual(billing.EXIT_REFUSED, code)
        self.assertIn("different tenancy", err)
        conn = db.connect(self.db_path)
        try:
            self.assertEqual([], db.billing_tenancies(conn))
            self.assertIsNone(db.get_meta(conn, billing.KEY_AMOUNT))
        finally:
            conn.close()

    def test_force_records_the_foreign_reading_under_its_own_tenancy(self):
        code, _, _ = self.record({
            "tenancy": OTHER_TENANCY, "currency": "USD",
            "rows": rows_for("2026-09-17"),
        }, force=True)
        self.assertEqual(billing.EXIT_OK, code)
        conn = db.connect(self.db_path)
        try:
            # Kept apart, not merged: the two accounts stay two accounts.
            self.assertEqual([OTHER_TENANCY], db.billing_tenancies(conn))
            self.assertEqual([], db.billing_daily(conn, NODE_TENANCY))
        finally:
            conn.close()

    def test_re_reading_a_day_replaces_it_and_leaves_the_others_alone(self):
        self.record({"tenancy": NODE_TENANCY,
                     "rows": rows_for("2026-09-16") + rows_for("2026-09-17")})
        self.record({"tenancy": NODE_TENANCY,
                     "rows": rows_for("2026-09-17", ocpu=120.0, amount=3.0)})
        conn = db.connect(self.db_path)
        try:
            daily = {row["day"]: row for row in db.billing_daily(conn, NODE_TENANCY)}
            self.assertEqual({"2026-09-16", "2026-09-17"}, set(daily))
            self.assertEqual(96.0, daily["2026-09-16"]["ocpu_hours"])
            self.assertEqual(120.0, daily["2026-09-17"]["ocpu_hours"])
            self.assertAlmostEqual(3.0, daily["2026-09-17"]["amount"])
        finally:
            conn.close()

    def test_record_without_a_reading_is_refused(self):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = billing.cmd(self.args())
        self.assertEqual(billing.EXIT_REFUSED, code)
        self.assertIn("--from-json", err.getvalue())


class IdentityTests(unittest.TestCase):
    def test_the_shape_is_read_out_of_the_metadata_service(self):
        payload = json.dumps({
            "tenantId": NODE_TENANCY, "id": "ocid1.instance.oc1..node",
            "shape": "VM.Standard.A1.Flex",
            "shapeConfig": {"ocpus": 16.0, "memoryInGBs": 96.0},
        }).encode("utf-8")

        class Response(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        with mock.patch.object(billing.urllib.request, "urlopen",
                               return_value=Response(payload)):
            identity = billing.node_identity()
        self.assertEqual(NODE_TENANCY, identity["node_tenancy"])
        self.assertEqual("16.0", identity["node_ocpus"])
        self.assertEqual("96.0", identity["node_memory_gb"])

    def test_off_the_node_it_says_so_rather_than_guessing(self):
        with mock.patch.object(billing.urllib.request, "urlopen",
                               side_effect=OSError("no route to host")):
            with self.assertRaises(RuntimeError) as caught:
                billing.node_identity()
        self.assertIn("runs on the node", str(caught.exception))


class CardTests(unittest.TestCase):
    """What the console says about all of it."""

    def base(self, **over):
        values = {
            "amount": "0.00", "currency": "USD", "through": "2026-09-17",
            "checked": db.utc_now(), "tenancy": NODE_TENANCY,
            "node_tenancy": NODE_TENANCY, "node_shape": "VM.Standard.A1.Flex",
            "node_ocpus": "16.0", "node_memory_gb": "96.0",
            "daily": [
                {"day": "2026-09-15", "amount": 0.0, "ocpu_hours": 96.0,
                 "gb_hours": 576.0, "currency": "USD"},
                {"day": "2026-09-16", "amount": 0.0, "ocpu_hours": 96.0,
                 "gb_hours": 576.0, "currency": "USD"},
                {"day": "2026-09-17", "amount": 0.0, "ocpu_hours": 47.0,
                 "gb_hours": 282.0, "currency": "USD"},
            ],
            "services": [{"service": "Compute", "amount": 0.0, "currency": "USD"}],
            "tenancies": [NODE_TENANCY],
        }
        values.update(over)
        return values

    def test_a_meter_smaller_than_the_machine_is_called_out(self):
        card = web.billing_card(self.base())
        self.assertIn("the meter says", card)
        self.assertIn("16 OCPU", card)
        self.assertIn("a floor, not the answer", card)

    def test_a_meter_that_matches_the_machine_says_nothing(self):
        values = self.base(node_ocpus="4.0")
        self.assertEqual("", web.billing_shape_warning(values))

    def test_a_foreign_figure_is_named_as_one(self):
        card = web.billing_card(self.base(tenancy=OTHER_TENANCY))
        self.assertIn("different tenancy", card)
        self.assertIn("other account", card)

    def test_an_unidentified_node_asks_to_be_identified(self):
        card = web.billing_card(self.base(node_tenancy=""))
        self.assertIn("--identify", card)

    def test_the_bars_measure_hours_not_money(self):
        html = web.billing_bars(self.base()["daily"])
        self.assertIn("OCPU held a day", html)
        self.assertIn("2026-09-15", html)
        self.assertIn("partial", html)          # today is never a full day
        self.assertNotIn('class="charged', html)   # no day carries the class

    def test_a_charged_day_is_marked_without_taking_over_the_axis(self):
        # The day the node was resized is the tall one and it cost nothing;
        # the day money appeared is short. Drawing dollars would hide the
        # resize entirely, which is the fact that explains the bill.
        daily = [
            {"day": "2026-09-12", "amount": 0.0, "ocpu_hours": 96.0,
             "gb_hours": 576.0},
            {"day": "2026-09-13", "amount": 0.0, "ocpu_hours": 384.0,
             "gb_hours": 2304.0},
            {"day": "2026-09-14", "amount": 1.78, "ocpu_hours": 172.0,
             "gb_hours": 1032.0},
        ]
        html = web.billing_bars(daily)
        self.assertIn("OCPU held a day", html)
        bars = re.findall(r'<i class="([^"]*)" style="height:([0-9.]+)%"', html)
        self.assertEqual(["", "", "charged partial"], [c for c, _ in bars])
        heights = [float(h) for _, h in bars]
        # The resize is the peak and the charged day is not — drawn in
        # dollars the first two days would both be flat at nothing.
        self.assertEqual(100.0, heights[1])
        self.assertLess(heights[2], heights[1])
        self.assertGreater(heights[0], 10.0)

    def test_the_allowance_line_reads_the_last_full_day(self):
        line = web.billing_held(self.base()["daily"])
        self.assertIn("2026-09-16", line)            # not today
        self.assertIn("4.00 OCPU", line)
        self.assertIn("inside the free allowance", line)

    def test_a_resize_inside_the_window_does_not_average_into_fiction(self):
        # Twelve days at 4 OCPU then four at 16 averages to 7, which the node
        # has never been. The last full day is 16 and says so.
        daily = ([{"day": f"2026-09-{d:02d}", "amount": 0.0, "ocpu_hours": 96.0,
                   "gb_hours": 576.0} for d in range(1, 13)]
                 + [{"day": f"2026-09-{d:02d}", "amount": 0.0,
                     "ocpu_hours": 384.0, "gb_hours": 2304.0}
                    for d in range(13, 18)])
        line = web.billing_held(daily)
        self.assertIn("16.00 OCPU", line)
        self.assertIn("over the free allowance", line)
        self.assertNotIn("7.", line)
        # and the node is 16 OCPU, so there is no discrepancy to report
        self.assertEqual("", web.billing_shape_warning(
            {"daily": daily, "node_ocpus": "16.0", "node_shape": "x"}))

    def test_over_the_allowance_says_so(self):
        daily = [dict(row, ocpu_hours=384.0, gb_hours=2304.0)
                 for row in self.base()["daily"]]
        self.assertIn("over the free allowance", web.billing_held(daily))

    def test_nothing_recorded_still_renders(self):
        card = web.billing_card({})
        self.assertIn("bin/sketchgen billing", card)
        self.assertNotIn("None", card)


if __name__ == "__main__":
    unittest.main()
