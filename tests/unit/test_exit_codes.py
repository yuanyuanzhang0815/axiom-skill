from axiom.cli import _exit_for_verdict


def test_exit_codes_granular():
    # #9: exit codes distinguish verdicts so automation (`axiom run && deliver`,
    # CI `case $?`) can triage without parsing the checkpoint JSON. The old form
    # collapsed all non-VERIFIED to 2.
    assert _exit_for_verdict("VERIFIED") == 0
    assert _exit_for_verdict("BLOCKED") == 3     # resolve a gate then re-run
    assert _exit_for_verdict("PARTIAL") == 4     # replan
    assert _exit_for_verdict("UNVERIFIED") == 5  # gather evidence
    assert _exit_for_verdict("garbage") == 2     # unknown/invalid -> generic
