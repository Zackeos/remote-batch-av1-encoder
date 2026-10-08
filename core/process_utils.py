"""
Cross-platform process tree suspend/resume/kill helpers built on psutil,
with POSIX signal fallbacks.
"""

import os
import signal
import psutil

def format_time_hms(seconds: float) -> str:
    """Formats seconds as m:ss, or h:mm:ss when an hour or longer."""
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    if h > 0:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"

def suspend_process(pid: int, trim_ram: bool = True) -> bool:
    """Suspends a process tree. On Windows, optionally trims the root process's working set."""
    success = False
    try:
        proc = psutil.Process(pid)
        for child in proc.children(recursive=True):
            try:
                child.suspend()
            except Exception:
                pass
        proc.suspend()
        success = True
    except Exception:
        if os.name != 'nt':
            try:
                os.kill(pid, signal.SIGSTOP)
                success = True
            except Exception:
                pass

    if trim_ram and os.name == 'nt':
        try:
            import ctypes
            psapi = ctypes.windll.psapi
            kernel32 = ctypes.windll.kernel32
            handle_m = kernel32.OpenProcess(0x1F0FFF, False, pid)
            if handle_m:
                psapi.EmptyWorkingSet(handle_m)
                kernel32.SetProcessWorkingSetSize(handle_m, ctypes.c_size_t(-1), ctypes.c_size_t(-1))
                kernel32.CloseHandle(handle_m)
        except Exception:
            pass

    return success

def resume_process(pid: int) -> bool:
    """Resumes a suspended process tree cross-platform."""
    try:
        proc = psutil.Process(pid)
        proc.resume()
        for child in proc.children(recursive=True):
            try:
                child.resume()
            except Exception:
                pass
        return True
    except Exception:
        if os.name != 'nt':
            try:
                os.kill(pid, signal.SIGCONT)
                return True
            except Exception:
                pass
    return False

def kill_process_tree(pid: int):
    """Forcefully terminates a process and all its child processes cross-platform."""
    try:
        proc = psutil.Process(pid)
        for child in proc.children(recursive=True):
            try:
                child.kill()
            except Exception:
                pass
        proc.kill()
    except Exception:
        if os.name != 'nt':
            try:
                os.kill(pid, signal.SIGKILL)
            except Exception:
                pass
