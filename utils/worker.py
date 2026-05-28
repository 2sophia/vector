import asyncio
import os
import signal
import subprocess
import time
from typing import Dict

worker_procs: Dict[str, subprocess.Popen] = {}
stop_event = asyncio.Event()


def _killpg_safe(pid: int, sig: int) -> None:
    """Manda un segnale al process group del pid, ignorando errori se già morto.
    Deriva il PGID con getpgid invece di assumere pgid==pid: l'assunzione regge solo
    finché i worker partono con start_new_session, e un domani senza quel flag
    killpg(pid) colpirebbe il gruppo del backend stesso (auto-kill)."""
    try:
        os.killpg(os.getpgid(pid), sig)
    except (ProcessLookupError, OSError):
        pass
    except Exception:
        pass


def terminate_worker_group_sync(
    p: subprocess.Popen | None,
    *,
    term_timeout: float = 0.8,
) -> None:
    """
    Versione SYNC: puoi chiamarla ovunque (anche fuori da asyncio).
    """
    if not p or p.poll() is not None:
        return

    _killpg_safe(p.pid, signal.SIGTERM)
    time.sleep(term_timeout)

    if p.poll() is None:
        _killpg_safe(p.pid, signal.SIGKILL)


async def terminate_worker_group(
    p: subprocess.Popen | None,
    *,
    term_timeout: float = 0.8,
) -> None:
    """
    Versione ASYNC: perfetta per FastAPI lifespan (non blocca l'event loop).
    """
    if not p or p.poll() is not None:
        return

    _killpg_safe(p.pid, signal.SIGTERM)
    await asyncio.sleep(term_timeout)

    if p.poll() is None:
        _killpg_safe(p.pid, signal.SIGKILL)


async def watch_process(name: str, cmd: list[str]):
    while not stop_event.is_set():
        p = subprocess.Popen(
            cmd,
            start_new_session=True,  # nuovo process group
        )
        worker_procs[name] = p

        code = await asyncio.to_thread(p.wait)

        if stop_event.is_set():
            return

        print(f"⚠️ Worker {name} exited with code={code}. Restarting in 2s...")
        await asyncio.sleep(2)
