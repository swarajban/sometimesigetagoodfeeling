"""`goodfeeling serve`: the web server, the worker it supervises, and (with --dev) the Vite dev server."""

from __future__ import annotations

import contextlib
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path

HOST = "127.0.0.1"
WEB_DIR = Path(__file__).resolve().parents[3] / "web"


class Child:
    """A child process in its own process group, so stopping it also stops whatever it started (ffmpeg, node)."""

    def __init__(self, name: str, argv: list[str], *, cwd: Path | None = None, env: dict | None = None):
        self.name, self.argv, self.cwd, self.env = name, argv, cwd, env
        self.proc: subprocess.Popen | None = None

    def start(self) -> None:
        self.proc = subprocess.Popen(self.argv, cwd=self.cwd, env=self.env, start_new_session=True)

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def stop(self, grace: float = 8.0) -> None:
        if self.proc is None or self.proc.poll() is not None:
            return
        with contextlib.suppress(ProcessLookupError):
            os.killpg(self.proc.pid, signal.SIGTERM)
        try:
            self.proc.wait(grace)
        except subprocess.TimeoutExpired:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(self.proc.pid, signal.SIGKILL)
            self.proc.wait(5)


def supervise(worker: Child, stopping: threading.Event) -> None:
    """Restart the worker if it dies while the server is up."""
    backoff = 1.0
    while not stopping.wait(1.0):
        if worker.alive():
            backoff = 1.0
            continue
        code = worker.proc.returncode if worker.proc else None
        print(f"worker exited ({code}); restarting in {backoff:.0f} s", flush=True)
        if stopping.wait(backoff):
            return
        worker.start()
        backoff = min(30.0, backoff * 2)


def serve(port: int, data_root: str | None, open_browser: bool, dev: bool, dev_port: int) -> None:
    import uvicorn

    from goodfeeling import DISPLAY_NAME
    from goodfeeling.server.app import create_app
    from goodfeeling.settings import load_settings

    root = load_settings().resolved_data_root(data_root)
    app = create_app(root)
    env = {**os.environ, "GOODFEELING_DATA_ROOT": str(root), "PYTHONUNBUFFERED": "1"}
    worker = Child(
        "worker",
        [sys.executable, "-m", "goodfeeling.jobs.worker", "--data-root", str(root), "--parent", str(os.getpid())],
        env=env,
    )
    children = [worker]
    url = f"http://{HOST}:{port}"
    if dev:
        npm = shutil.which("npm")
        if npm is None:
            raise SystemExit("--dev needs npm (Node.js) on PATH")
        if not (WEB_DIR / "node_modules").exists():
            subprocess.run([npm, "install"], cwd=WEB_DIR, check=True)
        vite_env = {**env, "GOODFEELING_API": url}
        children.append(
            Child(
                "vite",
                [npm, "run", "dev", "--", "--host", HOST, "--port", str(dev_port), "--strictPort"],
                cwd=WEB_DIR,
                env=vite_env,
            )
        )
        url = f"http://{HOST}:{dev_port}"
    for child in children:
        child.start()
    stopping = threading.Event()
    threading.Thread(target=supervise, args=(worker, stopping), daemon=True).start()
    print(f"{DISPLAY_NAME} on {url} (data in {root}); Ctrl-C stops the server and the worker", flush=True)
    if open_browser:
        threading.Timer(1.5 if not dev else 3.0, lambda: webbrowser.open(url)).start()

    class Server(uvicorn.Server):
        def handle_exit(self, sig, frame):
            app.state.stopping = True
            super().handle_exit(sig, frame)

    config = uvicorn.Config(app, host=HOST, port=port, log_level="warning", timeout_graceful_shutdown=3)
    server = Server(config)
    try:
        server.run()
    finally:
        stopping.set()
        for child in reversed(children):
            child.stop()
        time.sleep(0.1)
