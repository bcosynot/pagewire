"""The shipped tunables file must cover everything the code can be configured with.

Criterion 6 of the deploy ticket is "the tunables are settable without editing code",
evidenced by "the shipped config file with each one present". That claim is only worth
anything if it is checked, so this test derives the set of environment variable names the
code actually reads straight out of `src/pagewire/config.py` and insists the shipped
`deploy/pagewire.env.example` names every one of them.

The point is the derivation, not a hand-kept list: a new setting added to `config.py`
without a line in the shipped file fails this test, which is the only way a config file
stays honest across a code change.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
CONFIG_SOURCE = (REPO / "src" / "pagewire" / "config.py").read_text(encoding="utf-8")
EXAMPLE = (REPO / "deploy" / "pagewire.env.example").read_text(encoding="utf-8")

PREFIX = "PAGEWIRE_"

# The tunables the deploy ticket names in criterion 6, by the setting each one is.
CRITERION_6 = {
    "stitch window": "PAGEWIRE_STITCH_WINDOW_SECONDS",
    "Readwise destination switch": "PAGEWIRE_READWISE_ENABLED",
    "Hardcover destination switch": "PAGEWIRE_HARDCOVER_ENABLED",
    "Readwise token": "PAGEWIRE_READWISE_TOKEN",
    "Hardcover token": "PAGEWIRE_HARDCOVER_TOKEN",
    "device address": "PAGEWIRE_DEVICE_ADDRESS",
    "Hardcover throttle": "PAGEWIRE_HARDCOVER_THROTTLE_SECONDS",
    "sweep trigger": "PAGEWIRE_SWEEP_INTERVAL_SECONDS",
}


def _read_names() -> set[str]:
    """Every environment variable name config.py passes to a reader helper.

    Matches `_str(env, "NAME", …)`, `_int(env, "NAME", …)`, `_flag(env, "NAME")` and
    friends, including calls whose arguments wrap onto following lines.
    """
    found = re.findall(r'_[a-z]+\(\s*env\s*,\s*"([A-Z0-9_]+)"', CONFIG_SOURCE)
    assert found, "no environment reads found — did config.py change shape?"
    return {PREFIX + name for name in found}


def _shipped_keys() -> set[str]:
    keys = set()
    for line in EXAMPLE.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        keys.add(line.split("=", 1)[0].strip())
    return keys


def test_every_environment_read_has_a_line_in_the_shipped_file() -> None:
    missing = sorted(_read_names() - _shipped_keys())
    assert not missing, (
        "config.py reads these but deploy/pagewire.env.example never sets them: "
        + ", ".join(missing)
    )


def test_the_shipped_file_invents_no_settings_the_code_does_not_read() -> None:
    extra = sorted(_shipped_keys() - _read_names())
    assert not extra, (
        "deploy/pagewire.env.example sets these, but nothing in config.py reads them: "
        + ", ".join(extra)
    )


def test_criterion_six_tunables_are_all_present() -> None:
    keys = _shipped_keys()
    missing = {setting: name for setting, name in CRITERION_6.items() if name not in keys}
    assert not missing, f"criterion 6 tunables absent from the shipped file: {missing}"


def test_the_shipped_file_carries_no_real_secret() -> None:
    """It is committed, so the two token lines must be placeholders, not values."""
    for name in ("PAGEWIRE_TOKEN", "PAGEWIRE_READWISE_TOKEN", "PAGEWIRE_HARDCOVER_TOKEN"):
        line = next(
            (ln for ln in EXAMPLE.splitlines() if ln.strip().startswith(name + "=")),
            None,
        )
        assert line is not None, f"{name} missing from the shipped file"
        value = line.split("=", 1)[1].strip()
        assert value.startswith("REPLACE"), (
            f"{name} in the shipped file is not an obvious placeholder: {value!r}"
        )
