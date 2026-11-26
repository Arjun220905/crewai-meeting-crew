"""Start, stop and exercise the local server."""

import argparse
import asyncio
import fcntl
import os
import socket
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic, sleep

import uvicorn

from meeting_crew.server import Settings, create_app


def request(path: Path, command: bytes) -> str | None:
    with socket.socket(socket.AF_UNIX) as client:
        client.settimeout(1)
        try:
            client.connect(str(path))
            client.sendall(command)
            return client.recv(128).decode()
        except (FileNotFoundError, ConnectionRefusedError):
            return None


async def serve(run_dir: Path, lock_fd: int) -> None:
    path = run_dir / "control.sock"
    try:
        config = Settings.from_env()
        server = uvicorn.Server(uvicorn.Config(create_app(config), host=config.host, port=config.port))

        async def control(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            try:
                command = await asyncio.wait_for(reader.readexactly(1), timeout=1)
                if command == b"q":
                    server.should_exit = True
                    writer.write(b"stopping")
                elif server.started:
                    port = server.servers[0].sockets[0].getsockname()[1]
                    writer.write(str(port).encode())
                else:
                    writer.write(b"starting")
                await writer.drain()
            except (TimeoutError, ConnectionError, asyncio.IncompleteReadError):
                pass
            finally:
                writer.close()

        path.unlink(missing_ok=True)
        async with await asyncio.start_unix_server(control, path=path):
            await server.serve()
    finally:
        path.unlink(missing_ok=True)
        os.close(lock_fd)


def wait_ready(path: Path) -> int:
    deadline = monotonic() + 15
    while monotonic() < deadline:
        status = request(path, b"s")
        if status and status.isdecimal():
            return int(status)
        sleep(0.1)
    raise RuntimeError("Timed out waiting for the webhook server.")


def start(run_dir: Path, *, env: dict[str, str] | None = None) -> int:
    run_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    control = run_dir / "control.sock"
    log_path = run_dir / "server.log"
    with (run_dir / "lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            port = wait_ready(control)
            print(f"Server already running on port {port}.", flush=True)
            return port
        control.unlink(missing_ok=True)
        with log_path.open("ab") as log:
            process = subprocess.Popen(
                [sys.executable, "-m", "scripts.manage", "_serve", str(run_dir), str(lock.fileno())],
                env=env,
                pass_fds=(lock.fileno(),),
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=log,
                start_new_session=True,
            )
    try:
        deadline = monotonic() + 15
        while monotonic() < deadline and process.poll() is None:
            status = request(control, b"s")
            if status and status.isdecimal():
                port = int(status)
                print(f"Server started on port {port}. Log {log_path}", flush=True)
                return port
            sleep(0.1)
        raise RuntimeError(f"Server failed to start. See {log_path}.")
    except (Exception, KeyboardInterrupt):
        if process.poll() is None:
            process.terminate()
        process.wait(timeout=5)
        raise


def stop(run_dir: Path, *, timeout: float = 70) -> None:
    control = run_dir / "control.sock"
    if request(control, b"q") is None:
        print("Server is stopped.", flush=True)
        return
    deadline = monotonic() + timeout
    while monotonic() < deadline:
        if request(control, b"s") is None:
            print("Server stopped.", flush=True)
            return
        sleep(0.1)
    raise RuntimeError(f"Server is still shutting down. See {run_dir / 'server.log'}.")


def test_replay() -> None:
    with TemporaryDirectory(prefix="meeting-crew-", dir="/tmp") as name:
        root = Path(name)
        env = os.environ | {
            "PORT": "0",
            "WEBHOOK_HOST": "127.0.0.1",
            "OUTPUT_DIR": str(root / "out"),
            "TRANSCRIPT_SOURCE": "fixture",
            "LLM_MODE": "recorded",
            "GEMINI_API_KEY": "",
            "GOOGLE_API_KEY": "",
            "MEETSTREAM_API_KEY": "",
            "MEETSTREAM_WEBHOOK_SECRET": "",
            "WEBHOOK_SECRET": "",
        }
        run_dir = root / "service"
        try:
            port = start(run_dir, env=env)
            subprocess.run(
                [sys.executable, "-m", "scripts.replay"], env=env | {"PORT": str(port)}, check=True
            )
        finally:
            stop(run_dir)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["start", "stop", "test", "_serve"])
    parser.add_argument("args", nargs="*")
    args = parser.parse_args()
    try:
        if args.command == "_serve":
            asyncio.run(serve(Path(args.args[0]), int(args.args[1])))
        elif args.command == "start":
            start(Path(".run"))
        elif args.command == "stop":
            stop(Path(".run"))
        else:
            test_replay()
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
        raise SystemExit(str(exc)) from exc


if __name__ == "__main__":
    main()
