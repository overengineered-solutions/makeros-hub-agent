from __future__ import annotations

import asyncio
import json
import logging
import threading
from concurrent.futures import Future
from pathlib import Path
from typing import Any

from ..config import SPOOL_DIR, VirtualPrinterConfig
from ..diagnostics import get_default, redact
from .auth import MemberAuthSet
from .capture import CapturedJob, CaptureCoordinator, UploadRecord


log = logging.getLogger("makeros-hub.vprinter")

VP_BASE_DIR = SPOOL_DIR / "virtual-printer"
PLAIN_BIND_PORT = 3000
TLS_BIND_PORT = 3002
SSDP_PORT = 2021
MQTT_PORT = 8883
FTPS_PORT = 990
PASSIVE_START = 50000
PASSIVE_END = 50009


class VirtualPrinterManager:
    """Thread-backed owner for the asyncio virtual-printer runtime.

    The public async methods satisfy the VP module contract. The synchronous
    wrappers are used by the existing heartbeat loop, which is intentionally not
    converted to asyncio in this slice.
    """

    def __init__(
        self,
        *,
        base_dir: Path | None = None,
        on_capture=None,
        diagnostics=None,
    ) -> None:
        self._base_dir = Path(base_dir or VP_BASE_DIR)
        self._on_capture = on_capture
        self._diagnostics = diagnostics or get_default()
        self._lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._supervisor: _AsyncVirtualPrinterSupervisor | None = None

    async def start(self) -> None:
        self._ensure_loop()

    async def stop(self) -> None:
        if self._loop is None:
            return
        future = self._submit(self._stop_in_loop())
        await asyncio.wrap_future(future)
        self._stop_loop_thread()

    async def reconcile(self, config: VirtualPrinterConfig | None) -> None:
        if config is None and self._loop is None:
            return
        self._ensure_loop()
        future = self._submit(self._reconcile_in_loop(config))
        await asyncio.wrap_future(future)
        if config is None:
            self._stop_loop_thread()

    def reconcile_sync(self, config: VirtualPrinterConfig | None, *, timeout: float = 20.0) -> None:
        if config is None and self._loop is None:
            return
        self._ensure_loop()
        future = self._submit(self._reconcile_in_loop(config))
        future.result(timeout=timeout)
        if config is None:
            self._stop_loop_thread()

    def stop_sync(self, *, timeout: float = 20.0) -> None:
        if self._loop is None:
            return
        future = self._submit(self._stop_in_loop())
        future.result(timeout=timeout)
        self._stop_loop_thread()

    def _ensure_loop(self) -> None:
        with self._lock:
            if self._loop is not None and self._thread is not None and self._thread.is_alive():
                return
            ready = threading.Event()

            def runner() -> None:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                self._supervisor = _AsyncVirtualPrinterSupervisor(
                    base_dir=self._base_dir,
                    on_capture=self._on_capture,
                    diagnostics=self._diagnostics,
                )
                self._loop = loop
                ready.set()
                try:
                    loop.run_forever()
                finally:
                    pending = [task for task in asyncio.all_tasks(loop) if not task.done()]
                    for task in pending:
                        task.cancel()
                    if pending:
                        loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
                    loop.run_until_complete(loop.shutdown_asyncgens())
                    loop.close()

            self._thread = threading.Thread(target=runner, name="makeros-vprinter", daemon=True)
            self._thread.start()
        ready.wait(timeout=5.0)
        if self._loop is None or self._supervisor is None:
            raise RuntimeError("virtual printer event loop failed to start")

    def _submit(self, coro) -> Future:
        loop = self._loop
        if loop is None:
            raise RuntimeError("virtual printer event loop is not running")
        return asyncio.run_coroutine_threadsafe(coro, loop)

    async def _reconcile_in_loop(self, config: VirtualPrinterConfig | None) -> None:
        if self._supervisor is None:
            raise RuntimeError("virtual printer supervisor is not ready")
        await self._supervisor.reconcile(config)

    async def _stop_in_loop(self) -> None:
        if self._supervisor is not None:
            await self._supervisor.stop()

    def _stop_loop_thread(self) -> None:
        with self._lock:
            loop = self._loop
            thread = self._thread
            self._loop = None
            self._thread = None
            self._supervisor = None
        if loop is not None and loop.is_running():
            loop.call_soon_threadsafe(loop.stop)
        if thread is not None and thread.is_alive():
            thread.join(timeout=5.0)
            if thread.is_alive():
                log.warning("virtual printer event loop thread did not stop within timeout")


class _AsyncVirtualPrinterSupervisor:
    def __init__(self, *, base_dir: Path, on_capture, diagnostics=None) -> None:
        self.base_dir = base_dir
        self.on_capture = on_capture or _default_capture_logger
        self.diagnostics = diagnostics
        self.runtime: _VirtualPrinterRuntime | None = None
        self.fingerprint: tuple[Any, ...] | None = None

    async def reconcile(self, config: VirtualPrinterConfig | None) -> None:
        if config is None or not config.enabled:
            await self.stop()
            return
        fingerprint = _fingerprint(config)
        if self.runtime is not None and self.fingerprint == fingerprint:
            return
        await self.stop()
        runtime = _VirtualPrinterRuntime(config, base_dir=self.base_dir, on_capture=self.on_capture)
        try:
            await runtime.start()
        except ImportError as exc:
            safe = redact(str(exc))
            self._record_failure(f"virtual printer dependency missing: {safe}")
            log.error("virtual printer cannot start; dependency missing: %s", safe)
            await runtime.stop()
            return
        except Exception as exc:  # noqa: BLE001 - config-down must not break heartbeat
            safe = redact(str(exc))
            self._record_failure(f"virtual printer start failed: {safe}")
            log.warning("virtual printer start failed: %s", safe)
            await runtime.stop()
            return
        self.runtime = runtime
        self.fingerprint = fingerprint
        log.info(
            "virtual printer started serial=%s model=%s bind_ip=%s",
            config.serial,
            config.model,
            config.bind_ip,
        )

    async def stop(self) -> None:
        runtime = self.runtime
        self.runtime = None
        self.fingerprint = None
        if runtime is not None:
            await runtime.stop()
            log.info("virtual printer stopped")

    def _record_failure(self, message: str) -> None:
        if self.diagnostics is not None:
            self.diagnostics.record("vprinter", message)


class _VirtualPrinterRuntime:
    def __init__(self, config: VirtualPrinterConfig, *, base_dir: Path, on_capture) -> None:
        self.config = config
        self.base_dir = base_dir / _safe_serial(config.serial)
        self.on_capture = on_capture
        self.auth = MemberAuthSet(config.members)
        self.capture = CaptureCoordinator(on_capture, log.warning)
        self.servers: list[asyncio.AbstractServer] = []
        self.ssdps: list[Any] = []
        self.ftp = None
        self.broker = None

    async def start(self) -> None:
        # Lazy imports keep the agent importable before cryptography is present.
        from .bind_server import BindReplyConfig, start_bind_server
        from .cert import create_server_ssl_context, ensure_certificates
        from .ftp_server import FtpConfig, start_ftp_server
        from .mqtt_broker import MqttBroker
        from .report import build_get_version, build_print_ack, build_push_status
        from .ssdp import SsdpConfig, start_ssdp_responder

        self.base_dir.mkdir(parents=True, exist_ok=True)
        bundle = ensure_certificates(self.base_dir, self.config.serial, self.config.bind_ip)
        bind_config = BindReplyConfig(
            serial=self.config.serial,
            model=self.config.model,
            name=self.config.name,
            fw=self.config.fw,
        )
        self.broker = MqttBroker(
            serial=self.config.serial,
            auth=self.auth,
            report_builder=lambda sequence, gcode_state, gcode_file, prepare_percent: build_push_status(
                units=self.config.units,
                trays=self.config.trays,
                sequence_id=sequence,
                filaments=self.config.pool,
                gcode_state=gcode_state,
                gcode_file=gcode_file,
                prepare_percent=prepare_percent,
            ),
            version_builder=lambda sequence_id: build_get_version(
                model=self.config.model,
                serial=self.config.serial,
                units=self.config.units,
                sequence_id=sequence_id,
                ams_type=self.config.ams_type,
            ),
            ack_builder=lambda sequence_id, gcode_file: build_print_ack(sequence_id, gcode_file),
            on_project_file=self.capture.record_project_file,
            log=log.info,
        )

        try:
            self.servers.append(
                await start_bind_server("0.0.0.0", PLAIN_BIND_PORT, bind_config, log.info)
            )
            self.servers.append(
                await start_bind_server(
                    "0.0.0.0",
                    TLS_BIND_PORT,
                    bind_config,
                    log.info,
                    ssl_context=create_server_ssl_context(bundle),
                )
            )
            self.servers.append(
                await self.broker.start(
                    "0.0.0.0",
                    MQTT_PORT,
                    create_server_ssl_context(bundle),
                )
            )
            self.ssdps.append(
                await start_ssdp_responder(
                    SSDP_PORT,
                    SsdpConfig(
                        ip=self.config.bind_ip,
                        serial=self.config.serial,
                        model=self.config.model,
                        name=self.config.name,
                        fw=self.config.fw,
                    ),
                    log.info,
                )
            )
            self.ftp = await start_ftp_server(
                "0.0.0.0",
                FTPS_PORT,
                FtpConfig(
                    ip=self.config.bind_ip,
                    upload_dir=self.base_dir / "uploads",
                    auth=self.auth,
                    passive_start=PASSIVE_START,
                    passive_end=PASSIVE_END,
                    on_stored=self._on_stored,
                ),
                create_server_ssl_context(bundle, tls12_only=True),
                log.info,
            )
        except Exception:
            await self.stop()
            raise

    async def stop(self) -> None:
        for ssdp in self.ssdps:
            await ssdp.close()
        self.ssdps.clear()
        for server in self.servers:
            server.close()
        if self.servers:
            await asyncio.gather(*(server.wait_closed() for server in self.servers), return_exceptions=True)
        self.servers.clear()
        if self.broker is not None:
            await self.broker.close()
            self.broker = None
        if self.ftp is not None:
            await self.ftp.close()
            self.ftp = None
        self.capture.clear()

    def _on_stored(self, upload: UploadRecord) -> None:
        if self.broker is not None:
            self.broker.set_print_state("FINISH", gcode_file=upload.filename, prepare_percent="100")
        self.capture.record_upload(upload)


def _default_capture_logger(job: CapturedJob) -> None:
    log.info(
        "vprinter.capture_observed member_id=%s filename=%s size=%d sha256=%s "
        "use_ams=%s ams_mapping=%s required_filaments=%s submitted_at=%s",
        job.member_id,
        job.filename,
        job.size,
        job.sha256,
        job.use_ams,
        json.dumps(job.ams_mapping, sort_keys=True, default=str),
        json.dumps(job.required_filaments, sort_keys=True),
        job.submitted_at.isoformat(),
    )


def _fingerprint(config: VirtualPrinterConfig) -> tuple[Any, ...]:
    return (
        config.serial,
        config.model,
        config.name,
        config.fw,
        config.bind_ip,
        config.units,
        config.trays,
        config.ams_type,
        tuple((member.member_id, member.access_code) for member in config.members),
        json.dumps(list(config.pool), sort_keys=True, separators=(",", ":"), default=str),
    )


def _safe_serial(serial: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in serial)
