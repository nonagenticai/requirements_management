"""Pytest plugins loaded via ``-p`` from ``addopts``.

A plugin module loaded with ``-p`` is imported into the xdist CONTROLLER as well
as into every worker. A hook defined in a subdirectory ``conftest.py`` is not —
this repo has already been burned by exactly that (see
``tests/integration/conftest.py``'s note on ``pytest_sessionfinish`` never
registering on the controller). That is the whole reason this package exists
rather than another conftest.
"""
