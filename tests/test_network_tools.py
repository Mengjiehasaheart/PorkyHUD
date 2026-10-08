"""Network tool boundaries without remote hosts or real traffic tests."""

import io
import json
import socket
import ssl
import threading
import unittest
from contextlib import redirect_stdout
from unittest import mock

from porkyhud_inventory import InventoryStatus, ProbeResult
from porkyhud_tools import network


def live(value):
    return ProbeResult(json.dumps(value) if isinstance(value, dict) else value)


def dns(address="192.0.2.10", port=443, truncated=False):
    ipv6 = ":" in address
    endpoint = [address, port, 0, 0] if ipv6 else [address, port]
    return live({"status": "LIVE", "elapsed_ms": 1.5, "truncated": truncated,
                 "addresses": [{"family": int(socket.AF_INET6 if ipv6 else socket.AF_INET),
                                "address": endpoint}]})


def connected(address="192.0.2.10", tls_status="LIVE"):
    return live({"tcp": {"status": "LIVE", "elapsed_ms": 2.0, "address": address},
                 "tls": {"status": tls_status, "elapsed_ms": 3.0, "version": "TLSv1.3",
                         "not_before": "2026-01-01T00:00:00+00:00",
                         "not_after": "2027-01-01T00:00:00+00:00"}})


ROUTE = "gateway: 192.0.2.1\ninterface: en0\n"
COUNTERS = """Name Mtu Network Address Ipkts Ierrs Ibytes Opkts Oerrs Obytes Coll
lo0 16384 <Link#1> 10 0 1000 10 0 1000 0
lo0 16384 127 127.0.0.1 10 - 1000 10 - 1000 -
en0 1500 <Link#2> 02:00:00:00:00:01 20 2 5000 30 3 6000 0
en1* 1500 <Link#3> 0 0 0 0 0 0 0
"""


class PortOwnerTests(unittest.TestCase):
    def test_scoped_numeric_machine_query_excludes_remote_only_match(self):
        text = ("p100\0cPython\0\nf7\0PTCP\0n[::1]:8080\0TST=LISTEN\0\n"
                "f8\0PTCP\0n192.0.2.1:51000->198.51.100.1:8080\0TST=ESTABLISHED\0\n"
                "p101\0cworker\0\nf9\0PUDP\0n*:8080\0\n")
        runner = mock.Mock(return_value=live(text))
        result = network.inspect_port(8080, runner)
        argv, timeout = runner.call_args.args
        self.assertEqual(argv, ("/usr/sbin/lsof", "-nP", "-F0pcfPnT", "-iTCP:8080", "-iUDP:8080"))
        self.assertEqual(timeout, 3.0)
        self.assertEqual(len(result.rows), 2)
        self.assertEqual(result.rows[0], ("100", "Python", "TCP", "[::1]:8080", "—", "LISTEN"))
        self.assertEqual(result.rows[1][-1], "NOT APPLICABLE")
        self.assertFalse(result.complete)
        self.assertIn("Excluded 1", " ".join(result.details))

    def test_ipv6_zone_udp_peer_and_missing_state_are_preserved(self):
        text = "p100\ncworker\nf7\nPUDP\nn[fe80::1%en0]:1234->[fe80::2%en0]:5555\n"
        result = network.parse_port_owners(text, 1234, "udp")
        self.assertEqual(result.status, "LIVE")
        self.assertIn("fe80::1%en0", result.rows[0][3])
        tcp = network.parse_port_owners("p1\ncapp\nf2\nPTCP\nn*:1234\n", 1234)
        self.assertEqual(tcp.rows[0][-1], "NOT EXPOSED")

    def test_empty_and_failed_queries_never_claim_a_free_port(self):
        for status in (InventoryStatus.LIVE, InventoryStatus.PERMISSION_DENIED,
                       InventoryStatus.TIMEOUT, InventoryStatus.ERROR, InventoryStatus.MISSING):
            with self.subTest(status=status):
                report = network.inspect_port(443, lambda *_: ProbeResult(status=status))
                self.assertEqual(report.status, status.value)
                self.assertFalse(report.complete)
                self.assertIn("does not mean the port is free", " ".join(report.details))

    def test_malformed_and_duplicate_records_do_not_inflate_rows(self):
        text = ("p1\ncapp\nf2\nPTCP\nn*:443\nTST=LISTEN\n"
                "f3\nPTCP\nn*:443\nTST=LISTEN\n"
                "p2\ncbad\nf4\nPTCP\nnlocalhost:443\n")
        report = network.parse_port_owners(text, 443)
        self.assertEqual(len(report.rows), 1)
        self.assertEqual(report.status, "PARTIAL")
        self.assertEqual(network.parse_port_owners("malformed", 443).status, "ERROR")

    def test_truncation_and_terminal_control_removal(self):
        text = "".join("p%d\ncapp\x1b[31m\nf2\nPTCP\nn*:443\n" % pid for pid in range(1, 131))
        report = network.parse_port_owners(text, 443)
        self.assertEqual(len(report.rows), 128)
        self.assertTrue(report.truncated)
        self.assertFalse(any("\x1b" in cell for row in report.rows for cell in row))

    def test_port_and_protocol_validation_before_any_probe(self):
        runner = mock.Mock()
        for port in (0, 65536, -1, True, "443"):
            with self.assertRaises(ValueError):
                network.inspect_port(port, runner)
        with self.assertRaises(ValueError):
            network.inspect_port(443, runner, "TCP;whoami")
        runner.assert_not_called()


class TargetCheckTests(unittest.TestCase):
    def test_target_validation_allows_names_ip_idna_and_scopes(self):
        for target in ("example.invalid", "localhost", "192.0.2.1", "::1", "[2001:db8::1]", "fe80::1%en0"):
            self.assertTrue(network.validate_target(target))
        self.assertEqual(network.validate_target("bücher.invalid"), "xn--bcher-kva.invalid")
        for target in ("", "-x", "https://example.invalid", "example.invalid:443", "a b", "a\nb", "$()", "a/b", "x" * 254, "fe80::1%--bad/value"):
            with self.subTest(target=target), self.assertRaises(ValueError):
                network.validate_target(target)

    def test_success_includes_verified_tls_dates_and_route_to_connected_peer(self):
        runner = mock.Mock(side_effect=[dns(), connected(), live(ROUTE)])
        result = network.diagnose_network("example.invalid", runner=runner)
        self.assertEqual(result.status, "LIVE")
        self.assertTrue(result.complete)
        self.assertEqual([row[0] for row in result.rows], ["DNS", "ROUTE", "TCP", "TLS"])
        self.assertIn("Certificate expires: 2027", " ".join(result.details))
        self.assertEqual(runner.call_args.args[0], ("/sbin/route", "-n", "get", "-inet", "192.0.2.10"))
        self.assertTrue(all(call.args[1] > 0 for call in runner.call_args_list))
        json.dumps(result.payload(), allow_nan=False)

    def test_ipv6_and_scoped_route(self):
        runner = mock.Mock(side_effect=[dns("fe80::1"), connected("fe80::1"), live("interface: en0\n")])
        result = network.diagnose_network("fe80::1%en0", runner=runner)
        self.assertEqual(result.status, "LIVE")
        self.assertEqual(runner.call_args.args[0][-2:], ("-inet6", "fe80::1%en0"))

    def test_dns_deadline_and_permission_failures_stop_followup_stages(self):
        for status in (InventoryStatus.TIMEOUT, InventoryStatus.PERMISSION_DENIED, InventoryStatus.MISSING):
            runner = mock.Mock(return_value=ProbeResult(status=status))
            result = network.diagnose_network("example.invalid", runner=runner)
            self.assertEqual(result.data["stages"]["dns"]["status"], status.value)
            self.assertFalse(result.complete)
            self.assertEqual(runner.call_count, 1)

    def test_malformed_dns_and_inconsistent_family_do_not_reach_tcp(self):
        cases = ("not json", "[]", '{"status":"LIVE","addresses":[]}',
                 json.dumps({"status": "LIVE", "addresses": [{"family": int(socket.AF_INET), "address": ["::1", 443, 0, 0]}]}))
        for text in cases:
            runner = mock.Mock(return_value=live(text))
            result = network.diagnose_network("example.invalid", runner=runner)
            self.assertEqual(result.status, "ERROR")
            self.assertEqual(runner.call_count, 1)

    def test_failed_certificate_is_not_a_success_or_an_unverified_retry(self):
        runner = mock.Mock(side_effect=[dns(), connected(tls_status="CERTIFICATE FAILED"), live(ROUTE)])
        result = network.diagnose_network("example.invalid", runner=runner)
        self.assertEqual(result.status, "PARTIAL")
        self.assertFalse(result.complete)
        self.assertEqual(result.data["stages"]["tls"]["status"], "CERTIFICATE FAILED")
        self.assertNotIn("Certificate expires", " ".join(result.details))
        self.assertEqual(runner.call_count, 3)

    def test_cancellation_before_and_between_operations_stops_probes(self):
        event = threading.Event()
        event.set()
        runner = mock.Mock()
        result = network.diagnose_network("example.invalid", runner=runner, cancel=event)
        self.assertEqual(result.status, "CANCELLED")
        runner.assert_not_called()
        event.clear()

        def lookup(*_):
            event.set()
            return dns()

        runner = mock.Mock(side_effect=lookup)
        result = network.diagnose_network("example.invalid", runner=runner, cancel=event)
        self.assertEqual(result.status, "CANCELLED")
        self.assertEqual(runner.call_count, 1)

    def test_total_deadline_covers_all_children_and_does_not_restart_budget(self):
        clock = [100.0]
        calls = []
        responses = [dns(), connected(), live(ROUTE)]

        def run(argv, timeout):
            calls.append((argv, timeout, clock[0]))
            self.assertLessEqual(timeout, 105.0 - clock[0] + 1e-8)
            clock[0] += timeout * 0.8
            return responses.pop(0)

        with mock.patch.object(network.time, "monotonic", side_effect=lambda: clock[0]):
            result = network.diagnose_network("example.invalid", runner=run, timeout_s=5)
        self.assertEqual(result.status, "LIVE")
        self.assertLessEqual(clock[0], 105.0)
        self.assertEqual(len(calls), 3)

    def test_nonfinite_samples_and_truncated_lookup_survive_json(self):
        lookup = dns(truncated=True)
        value = json.loads(lookup.stdout)
        value["elapsed_ms"] = float("nan")
        runner = mock.Mock(side_effect=[live(value), connected(), live(ROUTE)])
        report = network.diagnose_network("example.invalid", runner=runner)
        self.assertTrue(report.truncated)
        self.assertFalse(report.complete)
        json.dumps(report.payload(), allow_nan=False)

    def test_invalid_timeouts_and_missing_runner(self):
        for timeout in (0, -1, 61, True, float("inf"), float("nan")):
            with self.assertRaises(ValueError):
                network.diagnose_network("example.invalid", timeout_s=timeout, runner=mock.Mock())
        with self.assertRaises(ValueError):
            network.diagnose_network("example.invalid")


class ChildScriptTests(unittest.TestCase):
    def test_dns_child_uses_system_resolver_and_caps_addresses(self):
        values = [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("192.0.2.%d" % i, 443)) for i in range(1, 12)]
        output = io.StringIO()
        with mock.patch.object(socket, "getaddrinfo", return_value=values), mock.patch("sys.argv", ["child", "example.invalid", "443"]), redirect_stdout(output):
            exec(compile(network._DNS_SCRIPT, "dns-child", "exec"), {})
        result = json.loads(output.getvalue())
        self.assertEqual(len(result["addresses"]), 8)
        self.assertTrue(result["truncated"])

    def test_tls_child_uses_default_verification_and_never_disables_it(self):
        raw = mock.MagicMock()
        secure = mock.MagicMock()
        secure.__enter__.return_value = secure
        secure.version.return_value = "TLSv1.3"
        secure.getpeercert.return_value = {"notBefore": "Jan  1 00:00:00 2026 GMT", "notAfter": "Jan  1 00:00:00 2027 GMT"}
        context = mock.Mock()
        context.wrap_socket.return_value = secure
        addresses = [{"family": int(socket.AF_INET), "address": ["192.0.2.1", 443]}]
        output = io.StringIO()
        with mock.patch.object(socket, "socket", return_value=raw), mock.patch.object(ssl, "create_default_context", return_value=context) as default_context, mock.patch("sys.argv", ["child", "example.invalid", json.dumps(addresses), "3"]), redirect_stdout(output):
            exec(compile(network._CONNECT_SCRIPT, "tls-child", "exec"), {})
        result = json.loads(output.getvalue())
        self.assertEqual(result["tls"]["status"], "LIVE")
        self.assertEqual(result["tls"]["not_after"], "2027-01-01T00:00:00+00:00")
        default_context.assert_called_once_with()
        self.assertEqual(context.wrap_socket.call_args.kwargs["server_hostname"], "example.invalid")
        secure.do_handshake.assert_called_once_with()
        raw.close.assert_called()

    def test_tls_validation_failure_closes_socket_without_retry(self):
        raw = mock.Mock()
        context = mock.Mock()
        context.wrap_socket.side_effect = ssl.SSLCertVerificationError("private failure text")
        addresses = [{"family": int(socket.AF_INET), "address": ["192.0.2.1", 443]}]
        output = io.StringIO()
        with mock.patch.object(socket, "socket", return_value=raw), mock.patch.object(ssl, "create_default_context", return_value=context), mock.patch("sys.argv", ["child", "example.invalid", json.dumps(addresses), "3"]), redirect_stdout(output):
            exec(compile(network._CONNECT_SCRIPT, "tls-child", "exec"), {})
        self.assertEqual(json.loads(output.getvalue())["tls"]["status"], "CERTIFICATE FAILED")
        self.assertNotIn("private failure text", output.getvalue())
        context.wrap_socket.assert_called_once()
        raw.close.assert_called()


class QualityAndInterfaceTests(unittest.TestCase):
    def test_explicit_quality_command_is_bounded_and_uses_documented_units(self):
        runner = mock.Mock(return_value=live({"dl_throughput": 100_000_000, "ul_throughput": 12_000_000,
                                            "responsiveness": 800, "base_rtt": 10.2, "interface_name": "en0", "private_unknown": "not retained"}))
        result = network.network_quality(runner)
        runner.assert_called_once_with(("/usr/bin/networkQuality", "-c", "-M", "20"), 25.0)
        self.assertEqual(result.rows[0], ("Download", "100.00", "Mbit/s"))
        self.assertEqual(result.status, "LIVE")
        self.assertTrue(result.complete)
        self.assertNotIn("private_unknown", result.data)

    def test_quality_timeout_and_malformed_or_partial_json(self):
        result = network.network_quality(lambda *_: ProbeResult(status=InventoryStatus.TIMEOUT))
        self.assertEqual(result.status, "TIMEOUT")
        self.assertEqual(network.parse_network_quality("bad").status, "ERROR")
        self.assertEqual(network.parse_network_quality("[]").status, "ERROR")
        report = network.parse_network_quality(json.dumps({"dl_throughput": 1_000_000, "ul_throughput": float("nan"), "error_code": -1, "error_domain": "private domain"}))
        self.assertEqual(report.status, "PARTIAL")
        self.assertFalse(report.complete)
        self.assertNotIn("private domain", str(report))
        json.dumps(report.payload(), allow_nan=False)

    def test_interface_link_rows_only_and_absent_drops(self):
        values = network.parse_interface_counters(COUNTERS)
        self.assertEqual(len(values), 3)
        self.assertEqual(values[1], network.InterfaceCounters("en0", 5000, 6000, 2, 3, None))
        report = network.interface_diagnostics(values)
        self.assertEqual(report.rows[1][5], "NOT EXPOSED")
        self.assertIsNone(report.data["interfaces"][1]["receive_bytes_per_s"])

    def test_passive_errors_drops_and_counter_rates_reset_or_gap(self):
        text = COUNTERS.replace(" Coll\n", " Coll Drop\n")
        text = "\n".join(line + " 7" if index else line for index, line in enumerate(text.splitlines()))
        values = network.parse_interface_counters(text)
        self.assertEqual(values[1].drops, 7)
        previous = (network.InterfaceCounters("en0", 4000, 5000),)
        report = network.interface_diagnostics(values, previous, 2)
        self.assertEqual(report.data["interfaces"][1]["receive_bytes_per_s"], 500)
        unchanged = network.interface_diagnostics(previous, previous, 1)
        self.assertEqual(unchanged.rows[0][-3:], ("0.0", "0.0", "LIVE"))
        reset = network.interface_diagnostics(previous, values, 1)
        self.assertEqual(reset.rows[0][-1], "RESET")
        gap = network.interface_diagnostics(values, previous, 61)
        self.assertEqual(gap.rows[1][-1], "BASELINE")
        self.assertIsNone(gap.data["elapsed_s"])

    def test_interface_failed_probe_and_malformed_table(self):
        result = network.collect_interface_diagnostics(lambda *_: ProbeResult(status=InventoryStatus.PERMISSION_DENIED))
        self.assertEqual(result.status, "PERMISSION DENIED")
        for text in ("", "Name broken", COUNTERS.replace("5000", "-1"), COUNTERS + "en0 1500 <Link#2> 1 0 1 1 0 1 0\n"):
            with self.subTest(text=text), self.assertRaises(ValueError):
                network.parse_interface_counters(text)
        report = network.collect_interface_diagnostics(lambda *_: live("bad"))
        self.assertEqual(report.status, "ERROR")


if __name__ == "__main__":
    unittest.main()
