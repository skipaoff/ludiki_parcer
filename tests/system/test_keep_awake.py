"""Open positions must survive an idle machine on both platforms the terminal runs on."""

import os

from app.system.keep_awake import KeepAwake


class FakeProcess:
    def __init__(self, command):
        self.command = command
        self.terminated = False

    def terminate(self):
        self.terminated = True


def test_macos_holds_a_caffeinate_tied_to_this_process_and_lets_go():
    spawned = []

    def spawn(command, **kwargs):
        process = FakeProcess(command)
        spawned.append(process)
        return process

    awake = KeepAwake(platform="darwin", spawn=spawn)
    awake.hold()
    awake.hold()  # already held: one helper, not two

    assert awake.held and len(spawned) == 1
    assert spawned[0].command == ["caffeinate", "-s", "-w", str(os.getpid())]

    awake.release()

    assert not awake.held and spawned[0].terminated


def test_a_platform_without_a_way_to_stay_awake_does_not_pretend_to_hold():
    awake = KeepAwake(platform="linux", spawn=lambda *args, **kwargs: None)
    awake.hold()

    assert not awake.held
    awake.release()  # nothing to release, and nothing to fail on


def test_a_caffeinate_that_refuses_to_start_is_not_counted_as_held():
    def spawn(command, **kwargs):
        raise FileNotFoundError("caffeinate")

    awake = KeepAwake(platform="darwin", spawn=spawn)
    awake.hold()

    assert not awake.held


def test_a_read_only_keystore_refuses_writes_with_an_actionable_message():
    """On a server the terminal reads secrets and stores none; the refusal must say what to do."""
    from app.keystore.keystore import EnvironmentBackend

    backend = EnvironmentBackend()
    try:
        backend.set_password("ludik", "session:token", "x")
    except PermissionError as refusal:
        assert "LUDIK_SESSION_TOKEN" in str(refusal)
    else:
        raise AssertionError("writing to the environment backend must be refused")
