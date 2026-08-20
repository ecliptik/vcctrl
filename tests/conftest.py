"""Make pytest report a failed check() as a failed test, not an error.

`check()` in test_core.py records failures in a module-level FAILURES list
instead of raising, so that one broken invariant does not hide the twenty
checks after it. The cost is that a test function which found failures still
RETURNS NORMALLY, and pytest counts it as passed. That is not hypothetical:
"17 passed" from this suite was once cited as the evidence for a merge, on a
run where nothing was asserting anything.

The first fix put the check in an autouse fixture. It worked -- exit status 1,
test named -- but it fired during TEARDOWN, so pytest labelled it `1 error`
and still counted the test in its passed tally. A failing run that says
"20 passed" is exactly the kind of half-true readout this suite exists to
stamp out.

Forcing the exception into the CALL phase instead makes the verdict what it
should be: `1 failed`, and not counted as passed.

Deliberately generic -- it keys off any test module exposing a FAILURES list,
and imports nothing from test_core. Importing it here would drag in vcctrld
and evdev at collection time, which is a lot of machinery to load before
pytest has decided it is even running these tests.
"""

import pytest


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_call(item):
    failures = getattr(getattr(item, "module", None), "FAILURES", None)
    if failures is None:
        yield
        return

    first = len(failures)
    outcome = yield

    # The test blew up on its own. That exception is the real story; replacing
    # it with a summary of the checks would bury the traceback that explains
    # why the remaining checks never ran.
    if outcome.excinfo is not None:
        return

    new = failures[first:]
    if new:
        outcome.force_exception(
            AssertionError("check() failed: %s" % ", ".join(new)))
