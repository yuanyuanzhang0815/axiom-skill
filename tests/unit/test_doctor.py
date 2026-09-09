import subprocess
import sys


def test_doctor_subcommand_registered():
    # `axiom doctor` collapses the env/nvm/credentials/auth discovery the
    # flight-price relay did by failing into one preflight sweep. --help must
    # exit 0 without running the checks (so CI / hosts without a backend don't need a
    # live backend to register the command).
    r = subprocess.run(
        [sys.executable, "-m", "axiom", "doctor", "--help"],
        capture_output=True, text=True,
    )
    assert r.returncode == 0, r.stderr
    assert "doctor" in r.stdout
