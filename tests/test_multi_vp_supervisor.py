"""Tests for v0.40.0 _AsyncMultiVirtualPrinterSupervisor diff/reconcile logic.

The supervisor owns N runtimes + ONE shared SSDP listener. Tests stub the
runtime so we exercise the diff logic without spinning up real TCP servers.
"""

from __future__ import annotations

import asyncio
import unittest
from pathlib import Path

from makeros_hub.config import VirtualPrinterConfig, VirtualPrinterMember
from makeros_hub.vprinter.manager import (
    _AsyncMultiVirtualPrinterSupervisor,
)


def _make_config(
    serial: str,
    model: str,
    bind_ip: str = "10.0.0.10",
    pool=(),
    members=(),
) -> VirtualPrinterConfig:
    if not members:
        members = (
            VirtualPrinterMember(
                access_code_sha256="abc" * 22,  # 66 chars
                member_id="m1",
            ),
        )
    return VirtualPrinterConfig(
        enabled=True,
        serial=serial,
        model=model,
        name=f"VP {model}",
        fw="01.08.00.00",
        bind_ip=bind_ip,
        units=4,
        trays=4,
        ams_type="n3f",
        members=members,
        pool=pool,
    )


class _StubRuntime:
    """A drop-in replacement for _VirtualPrinterRuntime that records the
    lifecycle calls without binding any sockets. The supervisor reaches into
    self.runtimes externally, so we MUST quack like a real runtime."""

    def __init__(self, config, *, base_dir, on_capture, shared_ssdp_protocol=None):
        self.config = config
        self.base_dir = base_dir
        self.on_capture = on_capture
        self.shared_ssdp_protocol = shared_ssdp_protocol
        self.started = False
        self.stopped = False
        self.hot_applied: list = []
        # Register with shared SSDP just like the real runtime would.
        if shared_ssdp_protocol is not None:
            from makeros_hub.vprinter.ssdp import SsdpConfig

            shared_ssdp_protocol.register(
                SsdpConfig(
                    ip=config.bind_ip,
                    serial=config.serial,
                    model=config.model,
                    name=config.name,
                    fw=config.fw,
                )
            )

    async def start(self):
        self.started = True

    async def stop(self):
        self.stopped = True
        if self.shared_ssdp_protocol is not None:
            self.shared_ssdp_protocol.unregister(self.config.serial)

    async def apply_hot(self, config):
        self.config = config
        self.hot_applied.append(config)


class TestMultiVpSupervisor(unittest.IsolatedAsyncioTestCase):
    """Use a real-ish supervisor but a stub runtime so we never touch TCP."""

    async def asyncSetUp(self):
        self.base = Path("/tmp/multi-vp-test")
        self.sup = _AsyncMultiVirtualPrinterSupervisor(
            base_dir=self.base,
            on_capture=lambda _job: None,
            diagnostics=None,
        )
        # Stub runtime construction inside the supervisor.
        import makeros_hub.vprinter.manager as mod

        self._orig = mod._VirtualPrinterRuntime
        mod._VirtualPrinterRuntime = _StubRuntime

    async def asyncTearDown(self):
        # Always tear down to free the SSDP socket.
        await self.sup.stop()
        import makeros_hub.vprinter.manager as mod

        mod._VirtualPrinterRuntime = self._orig

    async def test_reconcile_with_empty_list_is_noop(self):
        await self.sup.reconcile([])
        self.assertEqual(len(self.sup.runtimes), 0)

    async def test_reconcile_starts_one_runtime_per_config(self):
        await self.sup.reconcile(
            [
                _make_config("AAA", "A1 mini"),
                _make_config("BBB", "X1C", bind_ip="10.0.0.11"),
            ]
        )
        self.assertEqual(set(self.sup.runtimes.keys()), {"AAA", "BBB"})
        # Both registered with the shared SSDP.
        serials = {c.serial for c in self.sup.shared_ssdp_protocol.configs()}
        self.assertEqual(serials, {"AAA", "BBB"})

    async def test_disappearing_vp_gets_stopped(self):
        await self.sup.reconcile(
            [
                _make_config("AAA", "A1 mini"),
                _make_config("BBB", "X1C", bind_ip="10.0.0.11"),
            ]
        )
        # Second reconcile drops BBB.
        await self.sup.reconcile([_make_config("AAA", "A1 mini")])
        self.assertEqual(set(self.sup.runtimes.keys()), {"AAA"})
        serials = {c.serial for c in self.sup.shared_ssdp_protocol.configs()}
        self.assertEqual(serials, {"AAA"})

    async def test_identity_change_restarts_runtime(self):
        await self.sup.reconcile(
            [_make_config("AAA", "A1 mini", bind_ip="10.0.0.10")]
        )
        first_runtime = self.sup.runtimes["AAA"]
        # Same serial but DIFFERENT bind_ip = identity change → restart.
        await self.sup.reconcile(
            [_make_config("AAA", "A1 mini", bind_ip="10.0.0.99")]
        )
        second_runtime = self.sup.runtimes["AAA"]
        self.assertIsNot(first_runtime, second_runtime)
        self.assertTrue(first_runtime.stopped)
        self.assertEqual(second_runtime.config.bind_ip, "10.0.0.99")

    async def test_hot_state_change_triggers_apply_hot(self):
        cfg = _make_config("AAA", "A1 mini")
        await self.sup.reconcile([cfg])
        runtime = self.sup.runtimes["AAA"]
        # Member set change = hot_state change, identity unchanged → hot apply.
        new_members = (
            VirtualPrinterMember(
                access_code_sha256="xyz" * 22,
                member_id="m2",
            ),
        )
        new_cfg = _make_config("AAA", "A1 mini", members=new_members)
        await self.sup.reconcile([new_cfg])
        # SAME runtime instance, hot_applied recorded.
        self.assertIs(self.sup.runtimes["AAA"], runtime)
        self.assertEqual(len(runtime.hot_applied), 1)

    async def test_reconcile_to_empty_tears_down_shared_ssdp(self):
        await self.sup.reconcile(
            [_make_config("AAA", "A1 mini")]
        )
        self.assertIsNotNone(self.sup.shared_ssdp_runtime)
        await self.sup.reconcile([])
        # Shared SSDP also torn down — no VPs left to advertise.
        self.assertIsNone(self.sup.shared_ssdp_runtime)
        self.assertIsNone(self.sup.shared_ssdp_protocol)

    async def test_disabled_configs_are_ignored(self):
        cfg = _make_config("AAA", "A1 mini")
        # Make a frozen disabled clone (dataclass replace).
        from dataclasses import replace

        disabled = replace(cfg, enabled=False)
        await self.sup.reconcile([disabled])
        self.assertEqual(self.sup.runtimes, {})

    async def test_current_model_returns_first_sorted_serial(self):
        await self.sup.reconcile(
            [
                _make_config("BBB", "X1C", bind_ip="10.0.0.11"),
                _make_config("AAA", "A1 mini"),
            ]
        )
        # AAA sorts before BBB.
        self.assertEqual(self.sup.current_model(), "A1 mini")
        self.assertEqual(set(self.sup.current_models()), {"A1 mini", "X1C"})


if __name__ == "__main__":
    unittest.main()
