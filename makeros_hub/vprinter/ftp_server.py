from __future__ import annotations

import asyncio
import hashlib
import os
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from .auth import MemberAuthSet
from .capture import UploadRecord


MAX_UPLOAD_BYTES = 4 * 1024 * 1024 * 1024
FTP_CONTROL_TIMEOUT_SEC = 300.0
FTP_DATA_CONNECT_TIMEOUT_SEC = 30.0
FTP_CHUNK_TIMEOUT_SEC = 60.0
FTP_CHUNK_SIZE = 65536


@dataclass
class FtpConfig:
    ip: str
    upload_dir: Path
    auth: MemberAuthSet
    passive_start: int = 50000
    passive_end: int = 50009
    max_upload_bytes: int = MAX_UPLOAD_BYTES
    concurrent_uploads: int = 1
    on_stored: Callable[[UploadRecord], None] | None = None
    upload_semaphore: asyncio.Semaphore = field(init=False)

    def __post_init__(self) -> None:
        self.upload_semaphore = asyncio.Semaphore(max(1, int(self.concurrent_uploads)))


class PassiveDataServer:
    def __init__(
        self,
        server: asyncio.AbstractServer,
        port: int,
        connection: asyncio.Future[tuple[asyncio.StreamReader, asyncio.StreamWriter]],
        done: asyncio.Event,
    ) -> None:
        self.server = server
        self.port = port
        self.connection = connection
        self.done = done

    async def close(self) -> None:
        self.done.set()
        self.server.close()
        await self.server.wait_closed()


class FtpServer:
    def __init__(
        self,
        host: str,
        port: int,
        config: FtpConfig,
        ssl_context,
        log: Callable[[str], None],
    ) -> None:
        self.host = host
        self.port = port
        self.config = config
        self.ssl_context = ssl_context
        self.log = log
        self.server: asyncio.AbstractServer | None = None
        self._sessions: set[FtpSession] = set()
        self._tasks: set[asyncio.Task] = set()

    async def start(self) -> "FtpServer":
        self.config.upload_dir.mkdir(parents=True, exist_ok=True)
        self.server = await asyncio.start_server(
            self._handle,
            host=self.host,
            port=self.port,
            ssl=self.ssl_context,
        )
        return self

    async def close(self) -> None:
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()
        for session in list(self._sessions):
            await session.abort()
        tasks = [task for task in self._tasks if not task.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        if task is not None:
            self._tasks.add(task)
        session = FtpSession(reader, writer, self.config, self.ssl_context, self.log)
        self._sessions.add(session)
        try:
            await session.run()
        finally:
            self._sessions.discard(session)
            if task is not None:
                self._tasks.discard(task)


async def start_ftp_server(
    host: str,
    port: int,
    config: FtpConfig,
    ssl_context,
    log: Callable[[str], None],
) -> FtpServer:
    return await FtpServer(host, port, config, ssl_context, log).start()


class FtpSession:
    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        config: FtpConfig,
        ssl_context,
        log: Callable[[str], None],
    ) -> None:
        self.reader = reader
        self.writer = writer
        self.config = config
        self.ssl_context = ssl_context
        self.log = log
        self.peer = writer.get_extra_info("peername")
        self.peer_ip = _peer_ip(self.peer)
        self.user: str | None = None
        self.member_id: str | None = None
        self.logged_in = False
        self.passive: PassiveDataServer | None = None
        self._aborted = False
        self._partial_path: Path | None = None

    async def run(self) -> None:
        self.log(f"FTPS control TLS connection from {self.peer}")
        await self.reply("220 Virtual Bambu FTPS ready")
        try:
            while True:
                raw = await asyncio.wait_for(
                    self.reader.readline(),
                    timeout=FTP_CONTROL_TIMEOUT_SEC,
                )
                if not raw:
                    return
                line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
                if not line:
                    continue
                verb, _, arg = line.partition(" ")
                verb = verb.upper()
                arg = arg.strip()
                self.log(f"FTPS command from {self.peer}: {_display_command(verb, arg)}")
                if verb == "USER":
                    await self.cmd_user(arg)
                elif verb == "PASS":
                    await self.cmd_pass(arg)
                elif verb == "PBSZ":
                    await self.reply("200 PBSZ=0")
                elif verb == "PROT":
                    await self.reply("200 Protection level set to Private")
                elif verb == "TYPE":
                    await self.reply("200 Type set")
                elif verb == "PASV":
                    await self.cmd_pasv()
                elif verb == "EPSV":
                    await self.cmd_epsv()
                elif verb == "STOR":
                    await self.cmd_stor(arg)
                elif verb == "SIZE":
                    await self.cmd_size(arg)
                elif verb == "CWD":
                    await self.reply("250 Directory changed")
                elif verb == "PWD":
                    await self.reply('257 "/" is current directory')
                elif verb == "SYST":
                    await self.reply("215 UNIX Type: L8")
                elif verb == "FEAT":
                    await self.reply(
                        "211-Features\r\n PBSZ\r\n PROT\r\n PASV\r\n EPSV\r\n SIZE\r\n211 End"
                    )
                elif verb == "NOOP":
                    await self.reply("200 NOOP ok")
                elif verb == "OPTS":
                    await self.reply("200 OPTS ok")
                elif verb == "REST":
                    await self.reply("350 Restart marker accepted")
                elif verb == "LIST":
                    await self.cmd_list()
                elif verb == "QUIT":
                    await self.reply("221 Goodbye")
                    return
                else:
                    await self.reply(f"502 Command not implemented: {verb}")
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            self.log(f"FTPS control connection from {self.peer} timed out")
        except asyncio.IncompleteReadError:
            self.log(f"FTPS control connection from {self.peer} ended")
        except Exception as exc:
            self.log(f"FTPS error for {self.peer}: {exc}")
        finally:
            await self.close_passive()
            if self._partial_path is not None:
                _unlink_quietly(self._partial_path)
                self._partial_path = None
            self.writer.close()
            await _wait_closed(self.writer)
            self.log(f"FTPS control connection closed for {self.peer}")

    async def abort(self) -> None:
        self._aborted = True
        await self.close_passive()
        self.writer.close()
        await _wait_closed(self.writer)

    async def cmd_user(self, arg: str) -> None:
        self.user = arg
        await self.reply("331 Password required")

    async def cmd_pass(self, arg: str) -> None:
        if self.user != "bblp":
            self.config.auth.record_failure(arg, self.peer_ip)
            await self.reply("530 Login incorrect")
            return
        auth = self.config.auth.authenticate(arg, self.peer_ip)
        if auth.ok and auth.member_id is not None:
            self.logged_in = True
            self.member_id = auth.member_id
            await self.reply("230 Login successful")
            return
        if auth.rate_limited:
            self.log(f"FTPS auth rate-limited for peer_ip={self.peer_ip}")
        else:
            self.log(f"FTPS auth failed for peer_ip={self.peer_ip}")
        await self.reply("530 Login incorrect")

    async def cmd_pasv(self) -> None:
        if not self.logged_in:
            await self.reply("530 Not logged in")
            return
        await self.close_passive()
        self.passive = await self.open_passive()
        octets = self.config.ip.split(".")
        if len(octets) != 4:
            await self.reply("522 Network protocol not supported, use IPv4")
            return
        p1, p2 = divmod(self.passive.port, 256)
        await self.reply(f"227 Entering Passive Mode ({','.join(octets)},{p1},{p2})")

    async def cmd_epsv(self) -> None:
        if not self.logged_in:
            await self.reply("530 Not logged in")
            return
        await self.close_passive()
        self.passive = await self.open_passive()
        await self.reply(f"229 Entering Extended Passive Mode (|||{self.passive.port}|)")

    async def cmd_stor(self, arg: str) -> None:
        if not self.logged_in or self.member_id is None:
            await self.reply("530 Not logged in")
            return
        if self.passive is None:
            await self.reply("425 Use PASV first")
            return
        filename = _safe_filename(arg)
        target, partial = _unique_upload_paths(self.config.upload_dir, filename)
        self._partial_path = partial
        acquired = False
        try:
            await self.config.upload_semaphore.acquire()
            acquired = True
            await self.reply("150 Opening TLS data connection")
            data_reader, data_writer = await asyncio.wait_for(
                self.passive.connection,
                timeout=FTP_DATA_CONNECT_TIMEOUT_SEC,
            )
            total, sha256 = await self.receive_file(data_reader, data_writer, partial)
            partial.replace(target)
            self._partial_path = None
            self.log(f"FTPS stored {target} from {self.peer}: {total} bytes")
            await self.reply("226 Transfer complete")
            if self.config.on_stored is not None and filename.endswith(".3mf"):
                try:
                    self.config.on_stored(
                        UploadRecord(
                            member_id=self.member_id,
                            filename=filename,
                            file_path=target,
                            sha256=sha256,
                            size=total,
                        )
                    )
                except Exception as cb_exc:  # noqa: BLE001 - observe-only capture
                    self.log(f"FTPS on_stored callback error: {cb_exc}")
        except Exception as exc:
            self.log(f"FTPS STOR failed for {self.peer}: {exc}")
            _unlink_quietly(partial)
            await self.reply("451 Local error in processing")
        finally:
            if acquired:
                self.config.upload_semaphore.release()
            await self.close_passive()
            if self._partial_path is not None:
                _unlink_quietly(self._partial_path)
                self._partial_path = None

    async def cmd_size(self, _arg: str) -> None:
        if not self.logged_in:
            await self.reply("530 Not logged in")
            return
        await self.reply("550 File not found")

    async def cmd_list(self) -> None:
        if not self.logged_in:
            await self.reply("530 Not logged in")
            return
        if self.passive is None:
            await self.reply("425 Use PASV first")
            return
        await self.reply("150 Opening TLS data connection")
        try:
            _, data_writer = await asyncio.wait_for(
                self.passive.connection,
                timeout=FTP_DATA_CONNECT_TIMEOUT_SEC,
            )
            data_writer.close()
            await _wait_closed(data_writer)
            await self.reply("226 Transfer complete")
        except Exception as exc:
            self.log(f"FTPS LIST failed for {self.peer}: {exc}")
            await self.reply("451 Local error in processing")
        finally:
            await self.close_passive()

    async def open_passive(self) -> PassiveDataServer:
        last_error: Exception | None = None
        for port in range(self.config.passive_start, self.config.passive_end + 1):
            future: asyncio.Future[tuple[asyncio.StreamReader, asyncio.StreamWriter]] = (
                asyncio.get_running_loop().create_future()
            )
            done = asyncio.Event()

            async def handle_data(
                reader: asyncio.StreamReader,
                writer: asyncio.StreamWriter,
                fut: asyncio.Future[tuple[asyncio.StreamReader, asyncio.StreamWriter]] = future,
                done_event: asyncio.Event = done,
                bound_port: int = port,
            ) -> None:
                peer = writer.get_extra_info("peername")
                self.log(f"FTPS data TLS connection from {peer} on passive port {bound_port}")
                if not fut.done():
                    fut.set_result((reader, writer))
                    await done_event.wait()
                else:
                    writer.close()
                    await _wait_closed(writer)

            try:
                server = await asyncio.start_server(
                    handle_data,
                    host="0.0.0.0",
                    port=port,
                    ssl=self.ssl_context,
                )
                self.log(f"FTPS passive listener opened on {port}")
                return PassiveDataServer(server, port, future, done)
            except OSError as exc:
                last_error = exc
        raise RuntimeError(f"no passive FTPS ports available: {last_error}")

    async def receive_file(
        self,
        data_reader: asyncio.StreamReader,
        data_writer: asyncio.StreamWriter,
        target: Path,
    ) -> tuple[int, str]:
        total = 0
        digest = hashlib.sha256()
        try:
            with target.open("wb") as handle:
                while True:
                    if self._aborted:
                        raise RuntimeError("session aborted")
                    chunk = await asyncio.wait_for(
                        data_reader.read(FTP_CHUNK_SIZE),
                        timeout=FTP_CHUNK_TIMEOUT_SEC,
                    )
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > self.config.max_upload_bytes:
                        raise ValueError("upload exceeds 4 GiB limit")
                    digest.update(chunk)
                    handle.write(chunk)
        finally:
            data_writer.close()
            await _wait_closed(data_writer)
        return total, digest.hexdigest()

    async def close_passive(self) -> None:
        if self.passive is not None:
            await self.passive.close()
            self.passive = None

    async def reply(self, line: str) -> None:
        self.log(f"FTPS reply to {self.peer}: {line!r}")
        self.writer.write(line.encode("utf-8") + b"\r\n")
        await asyncio.wait_for(self.writer.drain(), timeout=5.0)


def _safe_filename(value: str) -> str:
    name = Path(value.strip() or "upload.bin").name
    name = name.replace("/", "_").replace("\\", "_")
    return name or "upload.bin"


def _unique_upload_paths(upload_dir: Path, filename: str) -> tuple[Path, Path]:
    upload_id = uuid.uuid4().hex
    stored_name = f"{upload_id}-{filename}"
    return upload_dir / stored_name, upload_dir / f".{stored_name}.{os.getpid()}.part"


def _display_command(verb: str, arg: str) -> str:
    if verb == "PASS":
        return "PASS <redacted>"
    return f"{verb} {arg}".rstrip()


def _peer_ip(peer: object) -> str | None:
    if isinstance(peer, tuple) and peer:
        return str(peer[0])
    return None


def _unlink_quietly(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    except OSError:
        pass


async def _wait_closed(writer: asyncio.StreamWriter) -> None:
    try:
        await writer.wait_closed()
    except Exception:
        pass
