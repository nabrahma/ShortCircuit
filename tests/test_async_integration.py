import pytest

from async_utils import AsyncExecutor, SyncWrapper


def test_async_executor_is_fail_fast():
    executor = AsyncExecutor()
    with pytest.raises(RuntimeError, match="BRIDGE REMOVED"):
        executor.start({})


def test_syncwrapper_getattr_is_fail_fast():
    executor = AsyncExecutor()
    wrapper = SyncWrapper(object(), executor)

    with pytest.raises(RuntimeError, match="BRIDGE REMOVED"):
        _ = wrapper.any_method


def test_syncwrapper_assert_interface_still_validates():
    executor = AsyncExecutor()

    class Target:
        async def safe_exit(self):  # pragma: no cover
            return True

    wrapper = SyncWrapper(Target(), executor)
    wrapper.assert_interface(["safe_exit"])

    with pytest.raises(AttributeError, match="missing required interface members"):
        wrapper.assert_interface(["safe_exit", "monitor_hard_stop_status"])
