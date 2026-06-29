import unittest
from unittest import mock

from agent.sni import discovery
from agent.sni import emu_connector


class ConstantsSharedTest(unittest.TestCase):
    """Discovery must reuse the connector's probe constants, not re-type them —
    that drift is exactly what this module exists to prevent."""

    def test_nwa_ports_are_the_connectors(self):
        self.assertIs(discovery.NWA_PORTS, emu_connector.NWA_PORTS)
        self.assertEqual(discovery.NWA_HOST, emu_connector.NWA_HOST)
        self.assertEqual(discovery.RA_HOST, emu_connector.RA_HOST)
        self.assertEqual(discovery.RA_PORT, emu_connector.RA_PORT)

    def test_bridge_port_default(self):
        self.assertEqual(discovery.SNI_BRIDGE_PORT, discovery.SNI_BRIDGE_PORTS[0])


class ScanEmulatorsTest(unittest.TestCase):
    def test_nothing_open_returns_empty(self):
        with mock.patch.object(discovery, "_probe_nwa", return_value=None), \
             mock.patch.object(discovery, "_probe_retroarch", return_value=None), \
             mock.patch.object(discovery, "_probe_bridge", return_value=None):
            self.assertEqual(discovery.scan_emulators(), [])

    def test_lists_each_open_nwa_port_with_its_bind(self):
        open_ports = {discovery.NWA_PORTS[0]: "Snes9x-EmuNWA",
                      discovery.NWA_PORTS[1]: "Snes9x-EmuNWA"}
        with mock.patch.object(discovery, "_probe_nwa",
                               side_effect=lambda p, **kw: open_ports.get(p)), \
             mock.patch.object(discovery, "_probe_retroarch", return_value=None), \
             mock.patch.object(discovery, "_probe_bridge", return_value=None):
            found = discovery.scan_emulators()
        self.assertEqual(len(found), 2)
        first = found[0]
        self.assertEqual(first["bind"],
                         {"transport": "emu", "source": "nwa", "port": discovery.NWA_PORTS[0]})
        self.assertIn(str(discovery.NWA_PORTS[0]), first["label"])

    def test_retroarch_and_bridge_with_device(self):
        with mock.patch.object(discovery, "_probe_nwa", return_value=None), \
             mock.patch.object(discovery, "_probe_retroarch", return_value="1.16.0"), \
             mock.patch.object(discovery, "_probe_bridge", return_value=["SD2SNES"]):
            found = discovery.scan_emulators()
        binds = [f["bind"] for f in found]
        self.assertIn({"transport": "emu", "source": "retroarch", "port": None}, binds)
        self.assertIn({"transport": "hardware"}, binds)
        bridge = next(f for f in found if f["bind"] == {"transport": "hardware"})
        self.assertIn("SD2SNES", bridge["label"])

    def test_bridge_up_but_no_device(self):
        with mock.patch.object(discovery, "_probe_nwa", return_value=None), \
             mock.patch.object(discovery, "_probe_retroarch", return_value=None), \
             mock.patch.object(discovery, "_probe_bridge", return_value=[]):
            found = discovery.scan_emulators()
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["bind"], {"transport": "hardware"})
        self.assertIn("no device", found[0]["label"].lower())

    def test_only_first_responding_bridge_port_listed(self):
        # Both bridge ports answer; only one bridge entry should appear.
        with mock.patch.object(discovery, "_probe_nwa", return_value=None), \
             mock.patch.object(discovery, "_probe_retroarch", return_value=None), \
             mock.patch.object(discovery, "_probe_bridge", return_value=["X"]) as pb:
            found = discovery.scan_emulators()
        self.assertEqual([f["bind"] for f in found].count({"transport": "hardware"}), 1)
        pb.assert_called_once()  # stopped after the first port answered


class DetectEmulatorTest(unittest.TestCase):
    def test_not_detected(self):
        with mock.patch.object(discovery, "_probe_bridge", return_value=None), \
             mock.patch.object(discovery, "_probe_nwa", return_value=None), \
             mock.patch.object(discovery, "_probe_retroarch", return_value=None):
            self.assertEqual(discovery.detect_emulator(), ("emu", None, "not detected"))

    def test_bridge_with_device_wins_over_direct_sources(self):
        # A bridge already owning a device is preferred even if NWA is also up,
        # so we coexist with whatever is sharing that bridge.
        with mock.patch.object(discovery, "_probe_bridge", return_value=["FXPak"]), \
             mock.patch.object(discovery, "_probe_nwa", return_value="snes9x") as pn, \
             mock.patch.object(discovery, "_probe_retroarch", return_value="1.16"):
            transport, source, label = discovery.detect_emulator()
        self.assertEqual((transport, source), ("hardware", None))
        self.assertIn("FXPak", label)
        pn.assert_not_called()  # short-circuits before probing direct sources

    def test_nwa_preferred_over_retroarch(self):
        with mock.patch.object(discovery, "_probe_bridge", return_value=None), \
             mock.patch.object(discovery, "_probe_nwa", return_value="snes9x"), \
             mock.patch.object(discovery, "_probe_retroarch", return_value="1.16") as pr:
            self.assertEqual(discovery.detect_emulator(),
                             ("emu", "nwa", "snes9x (EmuNetworkAccess)"))
            pr.assert_not_called()

    def test_retroarch_when_no_nwa(self):
        with mock.patch.object(discovery, "_probe_bridge", return_value=None), \
             mock.patch.object(discovery, "_probe_nwa", return_value=None), \
             mock.patch.object(discovery, "_probe_retroarch", return_value="1.16"):
            self.assertEqual(discovery.detect_emulator(), ("emu", "retroarch", "RetroArch"))

    def test_idle_bridge_is_a_hint_only(self):
        # Bridge up with no device AND no direct source -> hint, not a hard pick.
        with mock.patch.object(discovery, "_probe_bridge", return_value=[]), \
             mock.patch.object(discovery, "_probe_nwa", return_value=None), \
             mock.patch.object(discovery, "_probe_retroarch", return_value=None):
            transport, source, label = discovery.detect_emulator()
        self.assertEqual((transport, source), ("hardware", None))
        self.assertIn("attach", label.lower())

    def test_direct_source_preferred_over_idle_bridge(self):
        # Bridge up but empty; NWA is also up -> take NWA, not the idle bridge.
        with mock.patch.object(discovery, "_probe_bridge", return_value=[]), \
             mock.patch.object(discovery, "_probe_nwa", return_value="snes9x"), \
             mock.patch.object(discovery, "_probe_retroarch", return_value=None):
            self.assertEqual(discovery.detect_emulator(),
                             ("emu", "nwa", "snes9x (EmuNetworkAccess)"))


class ProbeNwaParsingTest(unittest.TestCase):
    """The NWA probe parses the EMULATOR_INFO `name:` field; exercise it against
    a fake socket so the parsing path itself is covered."""

    def _fake_conn(self, reply_bytes):
        sock = mock.Mock()
        sock.recv.return_value = reply_bytes
        cm = mock.MagicMock()
        return mock.patch.object(discovery.socket, "create_connection", return_value=sock), sock

    def test_parses_name_field(self):
        patch, sock = self._fake_conn(b"name: Snes9x-EmuNWA\nversion: 1.60\n")
        with patch:
            self.assertEqual(discovery._probe_nwa(48879), "Snes9x-EmuNWA")
        sock.close.assert_called_once()

    def test_defaults_name_when_absent(self):
        patch, _ = self._fake_conn(b"version: 1.60\n")
        with patch:
            self.assertEqual(discovery._probe_nwa(48879), "snes9x-nwa")

    def test_returns_none_when_nothing_listening(self):
        with mock.patch.object(discovery.socket, "create_connection", side_effect=OSError):
            self.assertIsNone(discovery._probe_nwa(48879))


if __name__ == "__main__":
    unittest.main()
