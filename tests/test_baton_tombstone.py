"""gaius baton is a tombstone; the HITL verb is gaius spin."""
from gaius.baton import cmd_baton
from gaius._core import COMMANDS
from gaius.spin import cmd_spin


def test_baton_exits_2_and_names_spin(capsys):
    rc = cmd_baton(["pass", "--skill", "should-not-write"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "retired" in err.lower()
    assert "gaius spin" in err
    assert "not an alias" in err
    assert "hot-stamp" in err
    assert "session-handoff remains" in err


def test_baton_dispatch_is_the_tombstone():
    assert COMMANDS["baton"] is cmd_baton
    assert COMMANDS["spin"] is cmd_spin
