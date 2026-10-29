"""Una instancia por carpeta; los reinicios liberan el bloqueo del proceso."""
import os


def acquire_instance(path):
    stream = open(path, 'a+b')
    if os.fstat(stream.fileno()).st_size == 0:
        stream.write(b'0')
        stream.flush()
    stream.seek(0)
    os.set_inheritable(stream.fileno(), False)
    try:
        if os.name == 'nt':
            import msvcrt
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        stream.close()
        raise RuntimeError('Sentinel ya está ejecutándose en esta carpeta. Abre http://127.0.0.1:5000.') from None
    return stream
