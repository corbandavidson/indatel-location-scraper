"""
Desktop launcher — starts Streamlit server and opens in the default browser.
Double-click LocationScraper.exe (or run this script) to launch.
"""

import sys
import os
import subprocess
import time
import socket
import webbrowser
import atexit
import ctypes
from pathlib import Path

TITLE = "Location Scraper"

_server_proc = None


def _get_dirs():
    if getattr(sys, "frozen", False):
        exe_dir = Path(sys.executable).parent
        app_dir = exe_dir / "app"
        python_exe = exe_dir / "python" / "python.exe"
    else:
        exe_dir = Path(__file__).parent
        app_dir = exe_dir
        python_exe = Path(sys.executable)
    return exe_dir, app_dir, python_exe


def _find_free_port(start=8501, end=8600):
    for port in range(start, end):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    return start


def _wait_for_server(port, timeout=60):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=2):
                return True
        except OSError:
            time.sleep(0.5)
    return False


def _start_streamlit(port):
    global _server_proc
    exe_dir, app_dir, python_exe = _get_dirs()

    if not python_exe.exists():
        raise FileNotFoundError(f"Python not found at {python_exe}")
    if not (app_dir / "app.py").exists():
        raise FileNotFoundError(f"app.py not found in {app_dir}")

    env = os.environ.copy()
    env["STREAMLIT_SERVER_PORT"] = str(port)
    env["STREAMLIT_SERVER_HEADLESS"] = "true"
    env["STREAMLIT_BROWSER_GATHER_USAGE_STATS"] = "false"
    env["STREAMLIT_GLOBAL_DEVELOPMENT_MODE"] = "false"
    env["STREAMLIT_SERVER_FILE_WATCHER_TYPE"] = "none"
    env["STREAMLIT_BROWSER_SERVER_ADDRESS"] = "localhost"

    pw_browsers = exe_dir / "playwright-browsers"
    if pw_browsers.exists():
        env["PLAYWRIGHT_BROWSERS_PATH"] = str(pw_browsers)

    app_py = app_dir / "app.py"
    cmd = [
        str(python_exe), "-m", "streamlit", "run", str(app_py),
        "--server.headless", "true",
        "--server.port", str(port),
        "--browser.gatherUsageStats", "false",
        "--global.developmentMode", "false",
        "--server.fileWatcherType", "none",
    ]

    _server_proc = subprocess.Popen(
        cmd,
        cwd=str(app_dir),
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
    )


def _kill_server():
    global _server_proc
    if _server_proc and _server_proc.poll() is None:
        _server_proc.terminate()
        try:
            _server_proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            _server_proc.kill()


atexit.register(_kill_server)


def main():
    port = _find_free_port()

    try:
        _start_streamlit(port)
    except Exception as e:
        ctypes.windll.user32.MessageBoxW(
            0, f"Failed to start:\n{e}", TITLE, 0x10,
        )
        return

    if not _wait_for_server(port, timeout=60):
        _kill_server()
        ctypes.windll.user32.MessageBoxW(
            0, "Server did not start in time.\nPlease try again.", TITLE, 0x10,
        )
        return

    webbrowser.open(f"http://localhost:{port}")

    try:
        _server_proc.wait()
    except KeyboardInterrupt:
        pass
    finally:
        _kill_server()


if __name__ == "__main__":
    main()
