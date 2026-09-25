"""同主机跨进程互斥；未知效果留下持久所有者，不用过期时间冒险接管。"""

import os
from pathlib import Path
import tempfile

from qharness.exception import LoopExecutionError
from qharness.loop.repository import digest


class RunOwnership:
    def __init__(self, repository, root):
        physical = os.path.normcase(os.path.realpath(root))
        self.root_ref = digest(["physical-root", physical])
        database = str(repository.sessions.kw["bind"].url)
        self.owner = digest([database, repository.key])
        self.keys = sorted((self.root_ref, digest(["run", database, repository.key])))
        self.handles = []

    def acquire(self):
        if self.handles:
            raise LoopExecutionError("当前服务已经持有运行锁", code="ownership_busy")
        directory = Path(tempfile.gettempdir()) / "qharness-ownership"
        directory.mkdir(mode=0o700, exist_ok=True)
        try:
            for key in self.keys:
                handle = (directory / (key + ".lock")).open("a+b")
                handle.seek(0, 2)
                if handle.tell() == 0:
                    handle.write(b"0")
                    handle.flush()
                handle.seek(0)
                try:
                    if os.name == "nt":
                        import msvcrt
                        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError:
                    handle.close()
                    raise LoopExecutionError("Run 或同物理工作区已有执行者", code="ownership_busy") from None
                self.handles.append(handle)
                handle.seek(1)
                prior = handle.read().decode("ascii")
                if prior and prior != self.owner:
                    raise LoopExecutionError("工作区上一个运行未确认释放，必须先恢复核对", code="ownership_busy")
                handle.seek(1)
                handle.truncate()
                handle.write(self.owner.encode("ascii"))
                handle.flush()
                os.fsync(handle.fileno())
        except BaseException:
            self.release(uncertain=True)
            raise

    def release(self, *, uncertain=False):
        for handle in reversed(self.handles):
            if not uncertain:
                handle.seek(1)
                handle.truncate()
                handle.flush()
                os.fsync(handle.fileno())
            handle.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            handle.close()
        self.handles.clear()
