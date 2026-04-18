"""Backend registry.

Backends isolate their dependencies: importing this module is cheap even
when Modal/Fly/AWS/etc. aren't installed. The resolver lazy-imports the
right submodule on first request.
"""
from super_q.backends.base import Backend, BackendError, TaskOutcome, TaskSpec
from super_q.backends.local import LocalBackend

__all__ = ["Backend", "BackendError", "TaskSpec", "TaskOutcome", "LocalBackend", "get_backend"]


_BUILTIN = {
    "local":  ("super_q.backends.local",  "LocalBackend"),
    "docker": ("super_q.backends.docker", "DockerBackend"),
    "modal":  ("super_q.backends.modal",  "ModalBackend"),
    "fly":    ("super_q.backends.fly",    "FlyBackend"),
    "ssh":    ("super_q.backends.ssh",    "SshBackend"),
    "gha":    ("super_q.backends.gha",    "GhaBackend"),
    "aws":    ("super_q.backends.aws",    "AwsBackend"),
}


def get_backend(name: str, **kwargs) -> Backend:
    """Resolve a backend by name.

    Extra kwargs are forwarded to the backend constructor; the `pool`
    kwarg (a `PoolSpec`) is especially common when resolving from a
    `config.toml`.
    """
    name = name.lower()
    if name not in _BUILTIN:
        raise BackendError(f"unknown backend: {name!r}")
    module_path, cls_name = _BUILTIN[name]
    import importlib
    mod = importlib.import_module(module_path)
    cls = getattr(mod, cls_name)
    return cls(**kwargs)
