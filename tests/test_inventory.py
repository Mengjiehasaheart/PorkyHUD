"""Read-only inventory contracts, parser failure boundaries, and privacy."""

from dataclasses import asdict, FrozenInstanceError, replace
from math import inf, nan
import json
import unittest
from unittest import mock

from porkyhud_inventory import (
    CONNECTION_FILTERS, SEARCH_QUERY_LIMIT, ConnectionRecord, DeviceRecord,
    InventoryObservation, InventoryResult, InventoryStatus, NetworkInterface,
    NetworkInventory, ProbeResult,
    collect_connections, collect_devices, collect_network,
    filter_inventory, summarize_inventory,
    parse_connections, parse_devices, parse_dns, parse_hardware_ports,
    parse_interfaces, parse_lsof_connections, parse_route, observe_inventory,
)


IFCONFIG = """lo0: flags=8049<UP,LOOPBACK,RUNNING,MULTICAST> mtu 16384
    inet 127.0.0.1 netmask 0xff000000
    inet6 ::1 prefixlen 128
en0: flags=8863<UP,BROADCAST,RUNNING,MULTICAST> mtu 1500
    ether 02:ab:cd:ef:12:34
    inet 192.0.2.10 netmask 0xffffff00 broadcast 192.0.2.255
    inet6 fe80::1%en0 prefixlen 64
    media: autoselect (1000baseT <full-duplex>)
    status: active
en1: flags=8863<UP,BROADCAST,RUNNING,MULTICAST> mtu 1500
    status: inactive
"""

DNS = """DNS configuration
resolver #1
  search domain[0] : PRIVATE_DOMAIN.invalid
  nameserver[0] : 192.0.2.53
  nameserver[1] : 2001:db8::53
DNS configuration (for scoped queries)
resolver #1
  nameserver[0] : 192.0.2.53
"""

ROUTE = """   route to: default
destination: default
    gateway: 192.0.2.1
  interface: en0
      flags: <UP,GATEWAY>
"""

PORTS = """Hardware Port: Wi-Fi
Device: en0
Ethernet Address: 02:ab:cd:ef:12:34

Hardware Port: USB 10/100/1000 LAN
Device: en1
Ethernet Address: 02:ab:cd:ef:12:35

VLAN Configurations
===================
"""

TCP_HEADER = "Active Internet connections (including servers)\nProto Recv-Q Send-Q Local Address Foreign Address (state)\n"
TCP = TCP_HEADER + """tcp4 0 1 192.0.2.10.12345 198.51.100.3.443 ESTABLISHED
tcp6 0 0 ::1.8021 *.* LISTEN
tcp46 0 0 *.5000 *.* LISTEN
tcp6 0 0 fe80::1%en0.1234 2001:db8::2.443 TIME_WAIT
"""

LSOF_TCP = """p12345
f12
tIPv4
n192.0.2.10:12345->198.51.100.3:443
TST=ESTABLISHED
TQR=0
TQS=12
f13
tIPv6
n[::1]:8080
TST=LISTEN
TQR=0
TQS=0
p98765
f14
tIPv4
n*:5000
TST=LISTEN
TQR=0
TQS=0
"""


def device_payload():
    return {
        "SPUSBHostDataType": [{"_name": "USB controller", "Driver": "PrivateController",
            "_items": [{"_name": "USB hub", "USBDeviceKeyVendorName": "Vendor",
                "USBDeviceKeyProductID": "0x1234", "USBDeviceKeyLinkSpeed": "5 Gb/s",
                "USBKeyLocationID": "0x12300000", "USBDeviceKeySerialNumber": "PRIVATE_SERIAL",
                "_items": [{"_name": "Keyboard", "USBDeviceKeyProductID": "0x4321",
                            "USBDeviceKeySerialNumber": "PRIVATE_SERIAL_2"}]}]}],
        "SPThunderboltDataType": [{"_name": "Thunderbolt Bus 1", "switch_uid_key": "PRIVATE_UID",
            "_items": [{"_name": "External SSD", "vendor_name_key": "SSD Vendor",
                "device_id_key": "0x0012", "mode_key": "Thunderbolt 3", "route_string_key": "1",
                "switch_uid_key": "PRIVATE_UID_2"}]}],
        "SPDisplaysDataType": [{"_name": "Apple GPU", "spdisplays_ndrvs": [{
            "_name": "Retina Display", "spdisplays_resolution": "3456 x 2234",
            "_spdisplays_display-serial-number": "PRIVATE_DISPLAY_SERIAL"}]}],
    }


class DeviceParserTests(unittest.TestCase):
    def test_tahoe_usb_tree_thunderbolt_and_displays_preserve_useful_topology(self):
        result = parse_devices(json.dumps(device_payload()))
        self.assertEqual(result.status, InventoryStatus.LIVE)
        self.assertTrue(result.complete)
        self.assertEqual(len(result.items), 4)
        by_name = {item.name: item for item in result.items}
        self.assertEqual(by_name["Keyboard"].parent, "USB hub")
        self.assertEqual(by_name["USB hub"].speed, "5 Gb/s")
        self.assertEqual(by_name["External SSD"].kind, "Thunderbolt")
        self.assertEqual(by_name["Retina Display"].parent, "Apple GPU")
        self.assertNotIn("USB controller", by_name)
        projection = json.dumps(asdict(result))
        self.assertNotIn("PRIVATE_", projection)
        self.assertNotIn("SerialNumber", projection)
        self.assertNotIn("serial", projection)

    def test_old_usb_schema_is_supported_and_empty_recognized_lists_are_live(self):
        raw = {"SPUSBDataType": [{"_name": "USB Bus", "_items": [{"_name": "Mouse",
                "vendor_id": "0x1234", "product_id": "0x5678", "speed": "12 Mb/sec",
                "serial_num": "PRIVATE_SERIAL"}]}], "SPThunderboltDataType": [], "SPDisplaysDataType": []}
        result = parse_devices(json.dumps(raw))
        self.assertEqual(result.items[0].name, "Mouse")
        self.assertTrue(result.complete)
        result = parse_devices(json.dumps({key: [] for key in raw}))
        self.assertEqual(result.items, ())
        self.assertEqual(result.status, InventoryStatus.LIVE)
        self.assertTrue(result.complete)

    def test_missing_categories_are_explicitly_incomplete(self):
        result = parse_devices('{"SPUSBDataType": []}')
        self.assertFalse(result.complete)
        self.assertIn("not exposed", result.detail)

    def test_records_are_capped_and_truncation_is_explicit(self):
        raw = device_payload()
        raw["SPUSBHostDataType"][0]["_items"] = [{"_name": "Device %d" % index,
            "USBDeviceKeyProductID": index, "USBKeyLocationID": index} for index in range(150)]
        result = parse_devices(json.dumps(raw))
        self.assertEqual(len(result.items), 128)
        self.assertTrue(result.truncated)
        self.assertFalse(result.complete)

    def test_wrong_shapes_truncated_json_and_bad_suffix_are_rejected_atomically(self):
        for raw in ("", "{", "{}", '{"SPUSBDataType": {}}',
                    '{"SPUSBDataType": [{"_items": null}]}',
                    '{"SPDisplaysDataType": [{"spdisplays_ndrvs": [3]}]}'):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                parse_devices(raw)

    def test_control_characters_paths_and_unknown_private_fields_never_project(self):
        raw = device_payload()
        leaf = raw["SPUSBHostDataType"][0]["_items"][0]["_items"][0]
        leaf["_name"] = "/Users/private/secret-file"
        leaf["USBDeviceKeyVendorName"] = "Vendor\x1b[31m\nName"
        leaf["private_filename"] = "PRIVATE_FILENAME"
        result = parse_devices(json.dumps(raw))
        projection = json.dumps(asdict(result))
        self.assertNotIn("/Users", projection)
        self.assertNotIn("PRIVATE_FILENAME", projection)
        self.assertNotIn("\\u001b", projection)
        self.assertIn("[private path]", projection)
        with self.assertRaises(FrozenInstanceError):
            result.items[0].name = "modified"


class NetworkParserTests(unittest.TestCase):
    def test_interfaces_distinguish_carrier_state_from_administrative_up(self):
        result = parse_interfaces(IFCONFIG)
        self.assertTrue(result.complete)
        lo, active, inactive = result.items
        self.assertEqual(lo.state, "UP / link unknown")
        self.assertEqual(active.state, "ACTIVE")
        self.assertEqual(inactive.state, "INACTIVE")
        self.assertEqual(active.ipv4, ("192.0.2.10",))
        self.assertEqual(active.ipv6, ("fe80::1%en0",))
        self.assertEqual(active.mac, "02:ab:cd:ef:12:34")
        self.assertEqual(active.mtu, 1500)
        self.assertIn("1000baseT", active.medium)

    def test_dns_projects_only_valid_server_ips_and_deduplicates_scoped_resolvers(self):
        self.assertEqual(parse_dns(DNS), ("192.0.2.53", "2001:db8::53"))
        self.assertEqual(parse_dns("No DNS configuration available"), ())
        self.assertNotIn("PRIVATE_DOMAIN", repr(parse_dns(DNS)))
        for raw in ("", "unexpected text", "DNS configuration\nnameserver[0] : invalid"):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                parse_dns(raw)

    def test_default_route_and_hardware_mapping_are_recognized_without_lookups(self):
        self.assertEqual(parse_route(ROUTE), ("192.0.2.1", "en0"))
        self.assertEqual(parse_hardware_ports(PORTS), {"en0": "Wi-Fi", "en1": "USB 10/100/1000 LAN"})
        for raw in ("", ROUTE + " gateway: 192.0.2.2\n", ROUTE.replace("192.0.2.1", "example.com")):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                parse_route(raw)

    def test_malformed_interface_rows_do_not_become_zero_or_partial_evidence(self):
        for raw in ("", "not interfaces", IFCONFIG + "en0: flags=0<> mtu 1500\n",
                    IFCONFIG.replace("192.0.2.10", "999.0.0.1"),
                    IFCONFIG.replace("02:ab:cd:ef:12:34", "PRIVATE_MAC")):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                parse_interfaces(raw)

    def test_interface_address_cap_is_explicitly_truncated(self):
        raw = "en0: flags=1<UP> mtu 1500\n" + "".join(
            "    inet 192.0.2.%d netmask 0xffffff00\n" % index for index in range(1, 20))
        result = parse_interfaces(raw)
        self.assertEqual(len(result.items[0].ipv4), 16)
        self.assertTrue(result.truncated)
        self.assertFalse(result.complete)


class ConnectionParserTests(unittest.TestCase):
    def test_numeric_ipv4_ipv6_wildcards_states_and_queue_bytes(self):
        result = parse_connections(TCP)
        self.assertEqual(result.status, InventoryStatus.LIVE)
        self.assertTrue(result.complete)
        self.assertEqual(result.items[0].state, "LISTEN")
        established = next(item for item in result.items if item.state == "ESTABLISHED")
        self.assertEqual(established.local, "192.0.2.10:12345")
        self.assertEqual(established.remote, "198.51.100.3:443")
        self.assertEqual(established.send_queue, 1)
        self.assertIn("[::1]:8021", [item.local for item in result.items])
        self.assertIn("[fe80::1%en0]:1234", [item.local for item in result.items])

    def test_empty_recognized_table_is_live_but_headerless_empty_is_unknown(self):
        result = parse_connections(TCP_HEADER)
        self.assertTrue(result.complete)
        self.assertEqual(result.items, ())
        with self.assertRaises(ValueError):
            parse_connections("")

    def test_verbose_process_columns_never_cross_projection(self):
        raw = TCP_HEADER + "tcp4 0 0 127.0.0.1.8080 *.* LISTEN 0 0 131072 131072 PRIVATE_PROCESS:12345 00100 0\n"
        result = parse_connections(raw)
        self.assertNotIn("PRIVATE_PROCESS", json.dumps(asdict(result)))
        self.assertNotIn("12345", json.dumps(asdict(result)))

    def test_bad_numeric_endpoints_states_and_suffix_rows_reject_complete_sample(self):
        for raw in (TCP + "tcp4 0 0 bad.8080 *.* LISTEN\n", TCP.replace("198.51.100.3.443", "example.com.443"),
                    TCP.replace("12345", "65536"), TCP.replace("TIME_WAIT", "UNKNOWN"), TCP + "garbage\n"):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                parse_connections(raw)

    def test_cap_is_explicit_and_listeners_are_prioritized(self):
        raw = TCP_HEADER + "".join("tcp4 0 0 192.0.2.10.%d 198.51.100.3.443 TIME_WAIT\n" % port for port in range(1000, 1150))
        raw += "tcp4 0 0 *.80 *.* LISTEN\n"
        result = parse_connections(raw)
        self.assertEqual(len(result.items), 128)
        self.assertTrue(result.truncated)
        self.assertFalse(result.complete)
        self.assertEqual(result.items[0].state, "LISTEN")

    def test_lsof_fallback_projects_only_endpoint_state_queue_and_visibility_limit(self):
        result = parse_lsof_connections(LSOF_TCP)
        self.assertEqual(result.status, InventoryStatus.LIVE)
        self.assertFalse(result.complete)
        self.assertFalse(result.truncated)
        self.assertEqual(len(result.items), 3)
        self.assertIn("protected processes", result.detail)
        self.assertIn("[::1]:8080", [item.local for item in result.items])
        self.assertEqual(next(item.send_queue for item in result.items if item.state == "ESTABLISHED"), 12)
        wire = json.dumps(asdict(result))
        self.assertNotIn("98765", wire)
        self.assertNotIn('"pid"', wire)
        self.assertNotIn('"fd"', wire)

    def test_lsof_shared_socket_endpoints_are_deduplicated_without_pid_attribution(self):
        result = parse_lsof_connections(LSOF_TCP + "p45678\nf15\ntIPv4\nn*:5000\nTST=LISTEN\nTQR=0\nTQS=0\n")
        self.assertEqual(len(result.items), 3)
        self.assertIn("deduplicated", result.detail)

    def test_lsof_missing_queues_or_corrupt_fields_do_not_manufacture_zero(self):
        for raw in ("", "p12345\n", LSOF_TCP.replace("TQS=12\n", ""),
                    LSOF_TCP.replace("TQS=12", "TQS=bad"), LSOF_TCP + "cPRIVATE_PROCESS\n",
                    LSOF_TCP.replace("[::1]:8080", "[::1]:70000"),
                    LSOF_TCP.replace("TST=LISTEN", "TST=UNKNOWN"),
                    LSOF_TCP.replace("n*:5000", "nexample.com:5000")):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                parse_lsof_connections(raw)


class CollectorTests(unittest.TestCase):
    def test_device_runner_is_injected_bounded_and_uses_both_usb_schemas(self):
        runner = mock.Mock(return_value=ProbeResult(json.dumps(device_payload())))
        result = collect_devices(runner)
        self.assertTrue(result.complete)
        argv = runner.call_args.args[0]
        self.assertEqual(argv[0], "/usr/sbin/system_profiler")
        self.assertIn("SPUSBDataType", argv)
        self.assertIn("SPUSBHostDataType", argv)
        self.assertNotIn("SPAudioDataType", argv)
        self.assertEqual(runner.call_args.kwargs, {"timeout_s": 5.0, "max_output_bytes": 4 * 1024**2})

    def test_source_failures_remain_typed_and_raw_detail_never_projects(self):
        for status in (InventoryStatus.MISSING, InventoryStatus.UNSUPPORTED,
                       InventoryStatus.PERMISSION_DENIED, InventoryStatus.TIMEOUT, InventoryStatus.ERROR):
            with self.subTest(status=status):
                runner = mock.Mock(return_value=ProbeResult("PRIVATE_RAW", status, "PRIVATE_STDERR"))
                for collector in (collect_devices, collect_connections, collect_network):
                    result = collector(runner)
                    self.assertEqual(result.status, status)
                    self.assertFalse(result.complete)
                    self.assertEqual(result.items, ())
                    self.assertNotIn("PRIVATE", json.dumps(asdict(result)))

    def test_valid_empty_connection_table_and_safe_flags(self):
        runner = mock.Mock(return_value=ProbeResult(TCP_HEADER))
        self.assertTrue(collect_connections(runner).complete)
        self.assertEqual(runner.call_args.args[0], ("/usr/sbin/netstat", "-anW", "-p", "tcp"))
        self.assertEqual(runner.call_args.kwargs["timeout_s"], 2.0)

    def test_silent_netstat_falls_back_to_bounded_selected_lsof_fields(self):
        runner = mock.Mock(side_effect=(ProbeResult(""), ProbeResult(LSOF_TCP)))
        result = collect_connections(runner)
        self.assertEqual(result.status, InventoryStatus.LIVE)
        self.assertFalse(result.complete)
        self.assertEqual(len(result.items), 3)
        self.assertEqual(runner.call_count, 2)
        call = runner.call_args
        self.assertEqual(call.args[0], ("/usr/sbin/lsof", "-nP", "-iTCP", "-F", "ftnT"))
        self.assertEqual(call.kwargs, {"timeout_s": 2.0, "max_output_bytes": 2 * 1024**2})
        self.assertNotIn("c", call.args[0][-1])

    def test_missing_netstat_can_still_observe_independent_lsof_evidence(self):
        runner = mock.Mock(side_effect=(ProbeResult(status=InventoryStatus.MISSING), ProbeResult(LSOF_TCP)))
        self.assertEqual(collect_connections(runner).status, InventoryStatus.LIVE)

    def test_valid_netstat_table_does_not_start_fallback(self):
        runner = mock.Mock(return_value=ProbeResult(TCP))
        self.assertTrue(collect_connections(runner).complete)
        self.assertEqual(runner.call_count, 1)

    def test_independent_network_supplement_failures_preserve_interfaces(self):
        def runner(argv, **kwargs):
            self.assertGreater(kwargs["timeout_s"], 0)
            self.assertLessEqual(kwargs["timeout_s"], 2.0)
            if "ifconfig" in argv[0]:
                return ProbeResult(IFCONFIG)
            if "scutil" in argv[0]:
                return ProbeResult(status=InventoryStatus.PERMISSION_DENIED)
            if "route" in argv[0]:
                return ProbeResult(ROUTE)
            return ProbeResult(PORTS)
        result = collect_network(runner)
        self.assertEqual(result.status, InventoryStatus.LIVE)
        self.assertEqual(len(result.items), 3)
        self.assertFalse(result.complete)
        self.assertEqual(result.dns_status, InventoryStatus.PERMISSION_DENIED)
        self.assertEqual(result.dns_servers, ())
        self.assertEqual(result.gateway, "192.0.2.1")
        self.assertEqual(result.items[1].hardware_port, "Wi-Fi")

    def test_bad_runner_and_parse_failures_are_sanitized_errors(self):
        for output in (None, ProbeResult("PRIVATE_BAD_RESPONSE")):
            for collector in (collect_devices, collect_connections, collect_network):
                with self.subTest(output=output, collector=collector):
                    result = collector(lambda *args, **kwargs: output)
                    self.assertEqual(result.status, InventoryStatus.ERROR)
                    self.assertNotIn("PRIVATE", result.detail)
        for collector in (collect_devices, collect_connections, collect_network):
            result = collector(mock.Mock(side_effect=RuntimeError("PRIVATE_EXCEPTION")))
            self.assertEqual(result.status, InventoryStatus.ERROR)
            self.assertNotIn("PRIVATE", result.detail)

    def test_completeness_model_rejects_impossible_claims(self):
        with self.assertRaises(ValueError):
            InventoryResult(InventoryStatus.ERROR, complete=True)
        with self.assertRaises(ValueError):
            InventoryResult(InventoryStatus.LIVE, truncated=True, complete=True)
        with self.assertRaises(ValueError):
            NetworkInventory(items=[])


class InventoryObservationTests(unittest.TestCase):
    def test_default_observation_contains_no_invented_attempt_or_good_sample(self):
        observation = InventoryObservation()
        self.assertIsNone(observation.latest)
        self.assertIsNone(observation.last_valid)
        self.assertIsNone(observation.latest_completed_mono_s)
        self.assertIsNone(observation.last_valid_completed_mono_s)

    def test_first_success_records_its_actual_completion_and_is_immutable(self):
        result = parse_devices(json.dumps(device_payload()))
        previous = InventoryObservation()
        observation = observe_inventory(previous, result, 10.0)
        self.assertIs(observation.latest, result)
        self.assertIs(observation.last_valid, result)
        self.assertEqual(observation.latest_completed_mono_s, 10.0)
        self.assertEqual(observation.last_valid_completed_mono_s, 10.0)
        self.assertIsNone(previous.latest)
        with self.assertRaises(FrozenInstanceError):
            observation.last_valid_completed_mono_s = 20.0

    def test_first_failure_keeps_typed_state_without_inventing_retained_rows(self):
        failed = InventoryResult(InventoryStatus.PERMISSION_DENIED, detail="device access denied")
        observation = observe_inventory(InventoryObservation(), failed, 10.0)
        self.assertIs(observation.latest, failed)
        self.assertIsNone(observation.last_valid)
        self.assertIsNone(observation.last_valid_completed_mono_s)
        self.assertEqual(observation.latest_completed_mono_s, 10.0)

    def test_every_failed_refresh_retains_good_rows_at_their_original_age(self):
        result = parse_devices(json.dumps(device_payload()))
        good = observe_inventory(InventoryObservation(), result, 10.0)
        for status in (InventoryStatus.ERROR, InventoryStatus.TIMEOUT, InventoryStatus.MISSING,
                       InventoryStatus.UNSUPPORTED, InventoryStatus.PERMISSION_DENIED, InventoryStatus.CHECKING):
            with self.subTest(status=status):
                failed = InventoryResult(status, detail="bounded inventory unavailable")
                observation = observe_inventory(good, failed, 20.0)
                self.assertEqual(observation.latest.status, status)
                self.assertIs(observation.last_valid, result)
                self.assertEqual(observation.last_valid_completed_mono_s, 10.0)
                self.assertEqual(observation.latest_completed_mono_s, 20.0)

    def test_recovery_replaces_retained_sample_and_completion_time(self):
        good = parse_devices(json.dumps(device_payload()))
        state = observe_inventory(InventoryObservation(), good, 10.0)
        state = observe_inventory(state, InventoryResult(InventoryStatus.ERROR), 20.0)
        empty_live = parse_devices('{"SPUSBDataType": [], "SPThunderboltDataType": [], "SPDisplaysDataType": []}')
        recovered = observe_inventory(state, empty_live, 30.0)
        self.assertEqual(recovered.last_valid.items, ())
        self.assertTrue(recovered.last_valid.complete)
        self.assertEqual(recovered.last_valid_completed_mono_s, 30.0)
        self.assertEqual(recovered.latest_completed_mono_s, 30.0)

    def test_repeated_failures_never_advance_last_good_sample_age(self):
        good = parse_devices(json.dumps(device_payload()))
        state = observe_inventory(InventoryObservation(), good, 10.0)
        state = observe_inventory(state, InventoryResult(InventoryStatus.TIMEOUT), 20.0)
        state = observe_inventory(state, InventoryResult(InventoryStatus.ERROR), 30.0)
        self.assertIs(state.last_valid, good)
        self.assertEqual(state.last_valid_completed_mono_s, 10.0)
        self.assertEqual(state.latest_completed_mono_s, 30.0)
        self.assertEqual(state.latest.status, InventoryStatus.ERROR)

    def test_partial_live_inventory_is_valid_without_claiming_complete_coverage(self):
        partial = parse_lsof_connections(LSOF_TCP)
        observation = observe_inventory(InventoryObservation(), partial, 10.0)
        self.assertIs(observation.last_valid, partial)
        self.assertFalse(observation.last_valid.complete)
        self.assertEqual(observation.last_valid.status, InventoryStatus.LIVE)

    def test_network_failure_retains_network_metadata_and_original_completion(self):
        good = NetworkInventory(InventoryStatus.LIVE, parse_interfaces(IFCONFIG).items,
                                gateway="192.0.2.1", default_interface="en0")
        state = observe_inventory(InventoryObservation(), good, 10.0)
        failed = NetworkInventory(InventoryStatus.TIMEOUT)
        retained = observe_inventory(state, failed, 20.0)
        self.assertEqual(retained.last_valid.gateway, "192.0.2.1")
        self.assertEqual(retained.last_valid_completed_mono_s, 10.0)
        self.assertEqual(retained.latest.status, InventoryStatus.TIMEOUT)

    def test_bad_nonfinite_negative_duplicate_and_reversed_times_are_rejected(self):
        good = parse_devices(json.dumps(device_payload()))
        state = observe_inventory(InventoryObservation(), good, 10.0)
        for timestamp in (nan, inf, -1.0, True, 10.0, 9.0, 10**1000):
            with self.subTest(timestamp=timestamp), self.assertRaises(ValueError):
                observe_inventory(state, good, timestamp)
        self.assertEqual(state.last_valid_completed_mono_s, 10.0)

    def test_forged_future_retention_and_mismatched_completion_are_rejected(self):
        good = parse_devices(json.dumps(device_payload()))
        failed = InventoryResult(InventoryStatus.ERROR)
        for kwargs in (dict(last_valid=good, last_valid_completed_mono_s=10.0),
                       dict(latest=failed, latest_completed_mono_s=10.0,
                            last_valid=good, last_valid_completed_mono_s=11.0),
                       dict(latest=good, latest_completed_mono_s=10.0),
                       dict(latest=good, latest_completed_mono_s=10.0,
                            last_valid=good, last_valid_completed_mono_s=9.0),
                       dict(latest=failed, latest_completed_mono_s=10.0,
                            last_valid=failed, last_valid_completed_mono_s=9.0)):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                InventoryObservation(**kwargs)

    def test_wrong_result_model_and_wrong_public_input_types_are_rejected(self):
        state = observe_inventory(InventoryObservation(), NetworkInventory(InventoryStatus.ERROR), 10.0)
        with self.assertRaises(ValueError):
            observe_inventory(state, InventoryResult(InventoryStatus.ERROR), 20.0)
        for previous, result in ((None, InventoryResult()), (InventoryObservation(), None),
                                 (InventoryObservation(), {})):
            with self.subTest(previous=previous, result=result), self.assertRaises(ValueError):
                observe_inventory(previous, result, 10.0)


class InventoryToolsTests(unittest.TestCase):
    def test_device_search_matches_casefolded_terms_across_visible_fields(self):
        records = (
            DeviceRecord("USB", "Straße keyboard", "Acme", "0x1234", "5 Gb/s",
                         "Desk hub", "Port 2"),
            DeviceRecord("Display", "Studio screen", "Acme", parent="Graphics"),
        )
        source = InventoryResult(InventoryStatus.LIVE, records, complete=True)
        selected = filter_inventory(source, "STRASSE USB acme port 2")
        self.assertEqual(selected.items, records[:1])
        self.assertEqual(filter_inventory(source, "5 gb/s 0x1234 hub").items, records[:1])
        self.assertEqual(filter_inventory(source, "screen graphics").items, records[1:])
        self.assertEqual(source.items, records)

    def test_search_is_literal_and_never_searches_status_or_detail(self):
        source = InventoryResult(
            InventoryStatus.LIVE, (DeviceRecord("USB", "Dock [A]*"),),
            detail="PRIVATE_DIAGNOSTIC", complete=True,
        )
        self.assertIs(filter_inventory(source, "[a]*"), source)
        for query in ("LIVE", "PRIVATE_DIAGNOSTIC", ".*", "$(touch /tmp/search)"):
            with self.subTest(query=query):
                self.assertEqual(filter_inventory(source, query).items, ())

    def test_empty_or_whitespace_search_reuses_the_immutable_result(self):
        source = parse_connections(TCP)
        self.assertIs(filter_inventory(source), source)
        self.assertIs(filter_inventory(source, " \t\n "), source)
        self.assertIs(filter_inventory(source, "tcp"), source)
        with self.assertRaises(FrozenInstanceError):
            filter_inventory(source, "5000").complete = False

    def test_network_search_covers_addresses_medium_hardware_port_and_mtu(self):
        parsed = parse_interfaces(IFCONFIG)
        source = NetworkInventory(
            InventoryStatus.LIVE, parsed.items, gateway="192.0.2.254",
            dns_servers=("192.0.2.53",), default_interface="en0",
        )
        source = replace(source, items=tuple(
            replace(item, hardware_port="Wi-Fi") if item.name == "en0" else item
            for item in source.items
        ))
        for query in ("EN0 ACTIVE", "192.0.2.10 1500", "fe80::1%en0",
                      "02:AB:CD:EF:12:34", "wi-fi 1000baset"):
            with self.subTest(query=query):
                self.assertEqual(tuple(item.name for item in filter_inventory(source, query).items),
                                 ("en0",))
        # Supplements remain in the result but do not make interface rows match.
        for query in ("192.0.2.53", "192.0.2.254", "CHECKING"):
            with self.subTest(query=query):
                self.assertEqual(filter_inventory(source, query).items, ())

    def test_tcp_groups_partition_all_returned_states_including_pending_and_closing(self):
        states = ("LISTEN", "ESTABLISHED", "SYN_SENT", "SYN_RCVD", "CLOSE_WAIT",
                  "FIN_WAIT_1", "CLOSING", "LAST_ACK", "FIN_WAIT_2", "TIME_WAIT", "CLOSED")
        source = InventoryResult(InventoryStatus.LIVE, tuple(
            ConnectionRecord("tcp4", "192.0.2.1:%d" % (1000 + index), "*:*", state)
            for index, state in enumerate(states)
        ), complete=True)
        self.assertEqual(CONNECTION_FILTERS, ("all", "listeners", "established", "other"))
        partitions = tuple(filter_inventory(source, connection_state=group).items
                           for group in CONNECTION_FILTERS[1:])
        self.assertEqual(tuple(item.state for item in partitions[0]), ("LISTEN",))
        self.assertEqual(tuple(item.state for item in partitions[1]), ("ESTABLISHED",))
        self.assertEqual(tuple(item.state for item in partitions[2]), states[2:])
        self.assertEqual(sum(map(len, partitions)), len(source.items))

    def test_tcp_search_and_group_filter_combine_without_endpoint_resolution(self):
        source = parse_connections(TCP)
        selected = filter_inventory(source, "5000 TCP46", connection_state="LISTENERS")
        self.assertEqual(len(selected.items), 1)
        self.assertEqual(selected.items[0].local, "*:5000")
        self.assertEqual(filter_inventory(source, "198.51.100.3:443",
                                          connection_state="listeners").items, ())
        self.assertEqual(filter_inventory(source, "2001:db8::2", connection_state="other").items[0].state,
                         "TIME_WAIT")

    def test_selection_preserves_source_coverage_and_all_network_supplements(self):
        source = NetworkInventory(
            InventoryStatus.LIVE, parse_interfaces(IFCONFIG).items,
            detail="some network details unavailable", truncated=True,
            gateway="192.0.2.1", default_interface="en0", dns_servers=("192.0.2.53",),
            dns_status=InventoryStatus.LIVE, route_status=InventoryStatus.LIVE,
            hardware_status=InventoryStatus.TIMEOUT,
        )
        selected = filter_inventory(source, "no-match")
        self.assertIsInstance(selected, NetworkInventory)
        self.assertEqual(asdict(selected), dict(asdict(source), items=()))
        self.assertTrue(selected.truncated)
        self.assertFalse(selected.complete)
        complete_source = InventoryResult(InventoryStatus.LIVE, (), complete=True)
        self.assertTrue(filter_inventory(complete_source, "missing").complete)

    def test_selection_never_promotes_retained_failed_rows_to_live(self):
        failed = InventoryResult(InventoryStatus.TIMEOUT, parse_connections(TCP).items,
                                 detail="retained earlier records")
        selected = filter_inventory(failed, "5000", connection_state="listeners")
        self.assertEqual(selected.status, InventoryStatus.TIMEOUT)
        self.assertFalse(selected.complete)
        self.assertEqual(selected.detail, failed.detail)
        summary = summarize_inventory(selected)
        self.assertEqual(summary.status, InventoryStatus.TIMEOUT)
        self.assertEqual(summary.total_count, 1)
        self.assertIn("timeout", summary.detail)

    def test_device_summary_counts_returned_kinds_in_predictable_order(self):
        source = InventoryResult(InventoryStatus.LIVE, (
            DeviceRecord("USB", "Keyboard"), DeviceRecord("Thunderbolt", "Storage"),
            DeviceRecord("Display", "Screen"), DeviceRecord("USB", "Hub"),
        ), truncated=True)
        summary = summarize_inventory(source)
        self.assertEqual(summary.kind_counts, (("Display", 1), ("Thunderbolt", 1), ("USB", 2)))
        self.assertEqual(summary.total_count, 4)
        self.assertTrue(summary.truncated)
        self.assertFalse(summary.complete)
        self.assertIn("returned records only", summary.detail)
        self.assertIn("truncated", summary.detail)
        self.assertEqual(summarize_inventory(filter_inventory(source, "USB")).total_count, 2)
        with self.assertRaises(FrozenInstanceError):
            summary.total_count = 100

    def test_network_summary_never_equates_administrative_up_with_an_active_link(self):
        source = NetworkInventory(InventoryStatus.LIVE, parse_interfaces(IFCONFIG).items + (
            NetworkInterface("en2", "DOWN"),
        ))
        summary = summarize_inventory(source)
        self.assertEqual(summary.total_count, 4)
        self.assertEqual(summary.active_interfaces, 1)
        self.assertEqual(summary.unknown_link_interfaces, 1)
        self.assertFalse(summary.complete)
        self.assertEqual(summary.listeners, 0)
        self.assertEqual(summary.kind_counts, ())

    def test_connection_summary_counts_returned_sockets_and_preserves_limited_visibility(self):
        source = parse_lsof_connections(LSOF_TCP)
        summary = summarize_inventory(source)
        self.assertEqual((summary.total_count, summary.listeners, summary.established,
                          summary.other_connections), (3, 2, 1, 0))
        self.assertFalse(summary.complete)
        self.assertIn("partial", summary.detail)
        summary = summarize_inventory(parse_connections(TCP))
        self.assertEqual((summary.total_count, summary.listeners, summary.established,
                          summary.other_connections), (4, 2, 1, 1))

    def test_empty_live_inventory_and_failed_initial_inventory_keep_different_statuses(self):
        live = summarize_inventory(InventoryResult(InventoryStatus.LIVE, complete=True))
        failed = summarize_inventory(InventoryResult(InventoryStatus.PERMISSION_DENIED))
        checking = summarize_inventory(InventoryResult())
        self.assertEqual(live.total_count, failed.total_count)
        self.assertEqual(live.status, InventoryStatus.LIVE)
        self.assertTrue(live.complete)
        self.assertEqual(failed.status, InventoryStatus.PERMISSION_DENIED)
        self.assertFalse(failed.complete)
        self.assertEqual(checking.status, InventoryStatus.CHECKING)

    def test_invalid_query_filter_and_record_inputs_are_rejected(self):
        connections = parse_connections(TCP)
        for kwargs in (dict(query=None), dict(query="x" * (SEARCH_QUERY_LIMIT + 1)),
                       dict(connection_state="closing"), dict(connection_state=None)):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                filter_inventory(connections, **kwargs)
        filter_inventory(connections, "x" * SEARCH_QUERY_LIMIT)
        for source in (None, {}, InventoryResult(items=(object(),))):
            with self.subTest(source=source), self.assertRaises(ValueError):
                filter_inventory(source)
            with self.subTest(source=source), self.assertRaises(ValueError):
                summarize_inventory(source)
        for source in (NetworkInventory(), parse_devices(json.dumps(device_payload()))):
            with self.subTest(source=source), self.assertRaises(ValueError):
                filter_inventory(source, connection_state="listeners")


if __name__ == "__main__":
    unittest.main()
