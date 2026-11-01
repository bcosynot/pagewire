"""The smallest test that proves the package is installable and importable.

This is not a behaviour test. It exists so the CI workflow has something real to run
from the first commit; the relay's behaviour tests arrive with the relay's code.
"""

import pagewire


def test_version_is_exposed():
    assert pagewire.__version__
