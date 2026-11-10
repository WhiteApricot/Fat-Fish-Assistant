"""Operating-system lock shared by live writers and cold snapshot operations."""
from contextlib import contextmanager
import os


@contextmanager
def file_lock(path, busy):
    with path.open('a+b') as stream:
        # Keep the marker bounded across repeated and competing acquisitions.
        # Windows can lock a byte range beyond EOF, so no data needs to be written.
        stream.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise busy(exc) from exc
        yield


def exclusive(instance, name='.instance-lock'):
    return file_lock(instance / 'private' / name,
                     lambda exc: RuntimeError('Instance is owned by another writer or maintenance operation'))
