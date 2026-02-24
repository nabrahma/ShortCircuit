import logging

logger = logging.getLogger(__name__)


class AsyncExecutor:
    """
    Deprecated bridge layer kept only for backward reference.
    Runtime usage is blocked intentionally.
    """

    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def start(self, config: dict):
        raise RuntimeError(
            "BRIDGE REMOVED in Phase 44.5. AsyncExecutor.start() must not be used."
        )

    def run(self, coro):
        raise RuntimeError(
            "BRIDGE REMOVED in Phase 44.5. AsyncExecutor.run() must not be used."
        )

    def run_bg(self, coro):
        raise RuntimeError(
            "BRIDGE REMOVED in Phase 44.5. AsyncExecutor.run_bg() must not be used."
        )

    def get_loop(self):
        raise RuntimeError(
            "BRIDGE REMOVED in Phase 44.5. AsyncExecutor.get_loop() must not be used."
        )


class SyncWrapper:
    """
    Deprecated sync facade for async objects.
    """

    def __init__(self, target, executor: AsyncExecutor):
        self._target = target
        self._executor = executor

    def assert_interface(self, required_methods: list[str]):
        missing = [name for name in required_methods if not hasattr(self._target, name)]
        if missing:
            raise AttributeError(
                f"{type(self._target).__name__} missing required interface members: "
                f"{', '.join(sorted(missing))}"
            )

    def __getattr__(self, name):
        raise RuntimeError(
            "BRIDGE REMOVED in Phase 44.5. This call site was not migrated. "
            "Find the caller and convert to direct await."
        )
