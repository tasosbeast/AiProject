from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path
import pytest

pytest.importorskip("PySide6")


def test_subprocess_exits_promptly_during_blocked_network_worker(tmp_path: Path) -> None:
    """Proves that a slow/blocked NetworkWorker (daemon thread) does not block real process exit on Quit."""
    script = """
import os
os.environ["QT_QPA_PLATFORM"] = "offscreen"
import sys
import time
from PySide6.QtWidgets import QApplication
from PySide6.QtCore import QTimer
from desktop_assistant.gui.main_window import MainWindow
from desktop_assistant.gui.worker import NetworkWorker
from desktop_assistant.models import AssistantResponse

class DummyAssistant:
    def handle(self, command: str, cancellation_token=None):
        return AssistantResponse.text_reply("ok")
    def shutdown(self):
        pass

def slow_network_call():
    # Simulate a network request blocked on I/O for 30 seconds
    time.sleep(30.0)

app = QApplication(sys.argv)
window = MainWindow(DummyAssistant())
worker = NetworkWorker(slow_network_call)
window._active_worker = worker
window._thread_pool.start(worker)

def trigger_quit():
    window.perform_shutdown()
    app.quit()

QTimer.singleShot(100, trigger_quit)
sys.exit(app.exec())
"""
    script_file = tmp_path / "run_network_shutdown.py"
    script_file.write_text(script, encoding="utf-8")

    start_time = time.monotonic()
    result = subprocess.run(
        [sys.executable, str(script_file)],
        capture_output=True,
        text=True,
        timeout=5.0,
        env=dict(os.environ, PYTHONPATH=os.pathsep.join([str(Path.cwd() / "src"), os.environ.get("PYTHONPATH", "")])),
    )
    elapsed = time.monotonic() - start_time

    assert result.returncode == 0, f"Process failed with stderr: {result.stderr}"
    assert elapsed < 3.0, f"Process took too long to exit ({elapsed:.2f}s), blocked by network worker"


def test_subprocess_finishes_active_action_worker_before_exit(tmp_path: Path) -> None:
    """Proves that an active must-finish ActionWorker (non-daemon thread) finishes its mutation on Quit."""
    marker_file = tmp_path / "action_output.txt"
    script = f"""
import os
os.environ["QT_QPA_PLATFORM"] = "offscreen"
import sys
import time
from pathlib import Path
from PySide6.QtWidgets import QApplication
from PySide6.QtCore import QTimer
from desktop_assistant.gui.main_window import MainWindow
from desktop_assistant.gui.worker import ActionWorker
from desktop_assistant.models import AssistantResponse

class DummyAssistant:
    def handle(self, command: str, cancellation_token=None):
        return AssistantResponse.text_reply("ok")
    def shutdown(self):
        pass

output_path = Path(r"{marker_file}")

def must_finish_action():
    time.sleep(0.2)
    output_path.write_text("MUTATION_COMPLETED_SUCCESSFULLY", encoding="utf-8")

app = QApplication(sys.argv)
window = MainWindow(DummyAssistant())
worker = ActionWorker(must_finish_action)
window._active_action_worker = worker
window._thread_pool.start(worker)

def trigger_quit():
    window.perform_shutdown(action_timeout=2.0)
    app.quit()

# Trigger quit shortly after action starts
QTimer.singleShot(50, trigger_quit)
sys.exit(app.exec())
"""
    script_file = tmp_path / "run_action_shutdown.py"
    script_file.write_text(script, encoding="utf-8")

    start_time = time.monotonic()
    result = subprocess.run(
        [sys.executable, str(script_file)],
        capture_output=True,
        text=True,
        timeout=5.0,
        env=dict(os.environ, PYTHONPATH=os.pathsep.join([str(Path.cwd() / "src"), os.environ.get("PYTHONPATH", "")])),
    )
    elapsed = time.monotonic() - start_time

    assert result.returncode == 0, f"Process failed with stderr: {result.stderr}"
    assert elapsed < 3.0, f"Process took {elapsed:.2f}s"
    assert marker_file.exists(), "Action worker did not complete before process exit"
    assert marker_file.read_text(encoding="utf-8") == "MUTATION_COMPLETED_SUCCESSFULLY"


def test_subprocess_allows_long_running_action_worker_to_complete_beyond_shutdown_timeout(tmp_path: Path) -> None:
    """Proves that a confirmed must-finish ActionWorker that outlasts UI shutdown timeout is NOT killed,
    and the Python process remains alive until the non-daemon worker finishes the mutation cleanly.
    """
    marker_file = tmp_path / "long_action_output.txt"
    script = f"""
import os
os.environ["QT_QPA_PLATFORM"] = "offscreen"
import sys
import time
from pathlib import Path
from PySide6.QtWidgets import QApplication
from PySide6.QtCore import QTimer
from desktop_assistant.gui.main_window import MainWindow
from desktop_assistant.gui.worker import ActionWorker
from desktop_assistant.models import AssistantResponse

class DummyAssistant:
    def handle(self, command: str, cancellation_token=None):
        return AssistantResponse.text_reply("ok")
    def shutdown(self):
        pass

output_path = Path(r"{marker_file}")

def long_must_finish_action():
    # Mutation takes 0.4s to finish safely on disk
    time.sleep(0.4)
    output_path.write_text("LONG_MUTATION_COMPLETED_SUCCESSFULLY", encoding="utf-8")

app = QApplication(sys.argv)
window = MainWindow(DummyAssistant())
worker = ActionWorker(long_must_finish_action)
window._active_action_worker = worker
window._thread_pool.start(worker)

def trigger_quit():
    # UI shutdown only waits 0.1s for fast window close
    window.perform_shutdown(action_timeout=0.1)
    app.quit()

# Trigger quit shortly after action starts
QTimer.singleShot(50, trigger_quit)
sys.exit(app.exec())
"""
    script_file = tmp_path / "run_long_action_shutdown.py"
    script_file.write_text(script, encoding="utf-8")

    start_time = time.monotonic()
    result = subprocess.run(
        [sys.executable, str(script_file)],
        capture_output=True,
        text=True,
        timeout=5.0,
        env=dict(os.environ, PYTHONPATH=os.pathsep.join([str(Path.cwd() / "src"), os.environ.get("PYTHONPATH", "")])),
    )
    elapsed = time.monotonic() - start_time

    assert result.returncode == 0, f"Process failed with stderr: {result.stderr}"
    # The process took >0.35s because it waited for the non-daemon thread, but completed within <3.0s without force kill
    assert elapsed >= 0.35, f"Process exited too early ({elapsed:.2f}s) before non-daemon worker completed"
    assert elapsed < 3.0, f"Process took {elapsed:.2f}s"
    assert marker_file.exists(), "Long-running action worker did not finish before process exit"
    assert marker_file.read_text(encoding="utf-8") == "LONG_MUTATION_COMPLETED_SUCCESSFULLY"

