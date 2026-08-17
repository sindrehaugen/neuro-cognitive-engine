import subprocess
import sys
from unittest.mock import MagicMock

from nce.subprocess_registry import (
    _active_processes,
    register_process,
    terminate_all,
    tracked_process,
    unregister_process,
)


def test_register_unregister():
    mock_proc = MagicMock(spec=subprocess.Popen)
    mock_proc.pid = 12345

    register_process(mock_proc)
    assert mock_proc in _active_processes

    unregister_process(mock_proc)
    assert mock_proc not in _active_processes


def test_tracked_process_context_manager():
    mock_proc = MagicMock(spec=subprocess.Popen)
    mock_proc.pid = 54321

    with tracked_process(mock_proc):
        assert mock_proc in _active_processes

    assert mock_proc not in _active_processes


def test_terminate_all_mocks():
    mock_proc1 = MagicMock(spec=subprocess.Popen)
    mock_proc1.pid = 1111
    mock_proc1.poll.return_value = None  # running

    mock_proc2 = MagicMock(spec=subprocess.Popen)
    mock_proc2.pid = 2222
    mock_proc2.poll.return_value = 0  # finished

    register_process(mock_proc1)
    register_process(mock_proc2)

    terminate_all()

    # mock_proc1 is running so it should be terminated
    mock_proc1.terminate.assert_called_once()
    # mock_proc2 is already finished so terminate should not be called
    mock_proc2.terminate.assert_not_called()

    # The registry should be cleared
    assert not _active_processes


def test_terminate_all_real_process():
    # Start a long-running process (e.g. sleep 10)
    if sys.platform == "win32":
        cmd = ["cmd", "/c", "timeout 10"]
    else:
        cmd = ["sleep", "10"]

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    register_process(proc)
    assert proc.poll() is None  # running
    assert proc in _active_processes

    # Terminate all
    terminate_all()

    # The process should be terminated/killed
    proc.wait(timeout=2.0)
    assert proc.poll() is not None  # exited
    assert proc not in _active_processes
