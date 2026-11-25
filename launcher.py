"""Local desktop entry point, shared by source runs and the Windows executable."""
import argparse
import hashlib
from http.client import HTTPConnection
import json
import logging
from logging.handlers import RotatingFileHandler
import os
import socket
import sys
import threading
import time
import webbrowser

import uvicorn

from backend.app import create_app
from backend.config import Settings


def acquire_lock(path):
    stream = path.open("a+b")
    stream.seek(0, 2)
    if stream.tell() == 0:
        stream.write(b"0")
        stream.flush()
    stream.seek(0)
    try:
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        stream.close()
        return None
    return stream  # OS releases the lock when the stream/process closes.


def main():
    parser = argparse.ArgumentParser(description="SkyTiles local photo library")
    parser.add_argument("--data-dir", help="Persistent data directory; defaults to data beside the executable")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--dev", action="store_true")
    args = parser.parse_args()
    settings = Settings.discover(args.data_dir, args.dev)
    settings.prepare()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        handlers=[RotatingFileHandler(settings.data_dir / "logs" / "skytiles.log",
                                                      maxBytes=2_000_000, backupCount=3, encoding="utf-8")])
    log = logging.getLogger("launcher")
    service_file = settings.data_dir / "service.json"
    lock = acquire_lock(settings.data_dir / "skytiles.lock")
    if lock is None:
        expected = hashlib.sha256(str(settings.data_dir).encode()).hexdigest()
        last_error = None
        for _ in range(25):
            try:
                service = json.loads(service_file.read_text(encoding="utf-8"))
                url = f"http://127.0.0.1:{int(service['port'])}"
                connection = HTTPConnection("127.0.0.1", int(service["port"]), timeout=1)
                try:
                    connection.request("GET", "/api/health")
                    response = connection.getresponse()
                    healthy = json.loads(response.read())
                finally:
                    connection.close()
                if healthy.get("app") == "SkyTiles" and healthy.get("library") == expected:
                    if not args.no_browser:
                        webbrowser.open("http://127.0.0.1:5173" if args.dev else url)
                    return 0
                last_error = ValueError("服务的影像库标识与当前目录不一致")
            except (OSError, ValueError, KeyError) as error:
                last_error = error
                if _ == 24:
                    log.exception("Could not contact the existing local instance")
                time.sleep(0.2)
        raise RuntimeError(f"无法重新打开已有的 SkyTiles 服务：{last_error}")
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        if not args.dev and not (settings.web_dir / "index.html").is_file():
            raise RuntimeError("找不到界面文件，请先执行 npm run build，或使用完整的 SkyTiles.exe。")
        try:
            sock.bind(("127.0.0.1", args.port))
        except OSError:
            if args.dev:
                raise RuntimeError("开发服务端口已占用，请关闭占用 8765 端口的程序。") from None
            sock.bind(("127.0.0.1", 0))
        sock.listen(128)
        port = sock.getsockname()[1]
        service_file.write_text(json.dumps({"port": port, "pid": os.getpid()}), encoding="utf-8")
        app = create_app(settings)
        server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port,
            log_config=None, access_log=False, loop="asyncio", http="h11", ws="none", lifespan="on"))
        app.state.shutdown_callback = lambda: setattr(server, "should_exit", True)
        url = "http://127.0.0.1:5173" if args.dev else f"http://127.0.0.1:{port}"

        def open_when_ready():
            for _ in range(600):
                if server.started:
                    webbrowser.open(url)
                    return
                if server.should_exit:
                    return
                time.sleep(0.1)

        if not args.no_browser:
            threading.Thread(target=open_when_ready, daemon=True).start()
        log.info("SkyTiles listening at %s; library: %s", url, settings.data_dir)
        server.run(sockets=[sock])
        return 0
    finally:
        sock.close()
        service_file.unlink(missing_ok=True)
        lock.close()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as error:
        logging.exception("SkyTiles could not start")
        if getattr(sys, "frozen", False) and os.name == "nt" and "--no-browser" not in sys.argv:
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, str(error), "SkyTiles 启动失败", 0x10)
        elif sys.stderr:
            print(f"SkyTiles: {error}", file=sys.stderr)
        sys.exit(1)
