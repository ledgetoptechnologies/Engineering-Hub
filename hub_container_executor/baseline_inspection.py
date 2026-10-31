"""Read-only facade over a separately enrolled baseline Engine connection."""
from .daemon_backend import DockerEngineBackend


class BaselineInspectionBackend:
    """Borrow an independently configured backend; expose no Engine mutations."""
    def __init__(self, backend):
        if type(backend) is not DockerEngineBackend or backend._gate_mount_verifier is None:
            raise ValueError('separately enrolled baseline backend required')
        self.__backend = backend
        self.binding = backend.binding
        self.source_directory = backend._gate_mount_verifier.source_directory

    def verify_authority(self, image, timeout_ms):
        return self.__backend._gate_mount_verifier.verify_current(image, timeout_ms=timeout_ms)

    def inspect_image(self, image, timeout_ms):
        return self.__backend.inspect_image(image, timeout_ms)

    def inspect_container(self, container_id, timeout_ms):
        return self.__backend.inspect_container(container_id, timeout_ms)
