"""Shell completion subcommand (`gaius completion <shell>`).

Pure/offline: the script is generated from the in-memory COMMANDS registry, so
no DB or session scan is needed. Asserts each shell emits a non-empty script
mentioning a real command + a global flag, and that an unknown shell errors.
"""
import pytest

from gaius._core import cmd_completion


@pytest.mark.parametrize("shell", ["bash", "zsh", "fish"])
def test_completion_emits_script(shell, capsys):
    cmd_completion([shell])
    out = capsys.readouterr().out
    assert out.strip(), f"{shell} completion should be non-empty"
    # Known command names (concord + inject are always in the published subset)
    assert "concord" in out
    assert "inject" in out
    # A known global flag (fish emits `-l sessions-dir`, bash/zsh `--sessions-dir`)
    assert "sessions-dir" in out
    # gaius is the completed program
    assert "gaius" in out


def test_completion_tracks_registry(capsys):
    """Command list is derived from COMMANDS.keys(), not hard-coded."""
    from gaius._core import COMMANDS
    cmd_completion(["bash"])
    out = capsys.readouterr().out
    # Every live command name appears in the emitted word list.
    for name in COMMANDS.keys():
        assert name in out, f"command {name!r} missing from completion script"


def test_completion_unknown_shell_errors(capsys):
    with pytest.raises(SystemExit) as exc:
        cmd_completion(["tcsh"])
    assert exc.value.code != 0
    err = capsys.readouterr().err
    assert "tcsh" in err or "invalid choice" in err


def test_completion_missing_shell_errors():
    with pytest.raises(SystemExit) as exc:
        cmd_completion([])
    assert exc.value.code != 0
