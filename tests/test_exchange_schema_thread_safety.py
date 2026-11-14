"""The exchange XSD must not mix validation errors between concurrent requests.

lxml's ``XMLSchema`` keeps ``error_log`` on the instance and releases the GIL while
validating. A process-wide cached schema used from the threadpool therefore let one
request read another's errors -- a cross-tenant leak into stored ``XSD-INVALID``
findings. Each thread now has its own schema and copies the log immediately.
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

from tools.architecture.exchange import NS, exchange_schema, import_exchange, schema_errors

XSI = "http://www.w3.org/2001/XMLSchema-instance"
_ELEMENTS = (
    '<elements><element identifier="a" xsi:type="BusinessActor"><name>A</name></element></elements>'
)


def _doc(extra: str = "") -> bytes:
    return (
        f'<model xmlns="{NS}" xmlns:xsi="{XSI}" identifier="m"><name>M</name>'
        f"{_ELEMENTS}{extra}</model>"
    ).encode()


VALID = _doc()
ALPHA = _doc("<bogusAlpha/>")
BETA = _doc("<bogusBeta/>")


def _check(i: int) -> list[str]:
    """Run one validation and return a list of contamination problems (empty = ok)."""
    kind = i % 3
    problems: list[str] = []
    if kind == 0:
        if (errs := schema_errors(VALID)) != []:
            problems.append(f"valid doc got errors {errs}")
    elif kind == 1:
        errs = schema_errors(ALPHA)
        if (
            not errs
            or any("bogusBeta" in e for e in errs)
            or not all("bogusAlpha" in e for e in errs)
        ):
            problems.append(f"alpha errors wrong: {errs}")
        xsd = [x.message for x in import_exchange(ALPHA).losses if x.code == "XSD-INVALID"]
        if not xsd or not all("bogusAlpha" in m for m in xsd):
            problems.append(f"alpha import findings wrong: {xsd}")
    else:
        errs = schema_errors(BETA)
        if (
            not errs
            or any("bogusAlpha" in e for e in errs)
            or not all("bogusBeta" in e for e in errs)
        ):
            problems.append(f"beta errors wrong: {errs}")
        xsd = [x.message for x in import_exchange(VALID).losses if x.code == "XSD-INVALID"]
        if xsd:
            problems.append(f"valid import got findings: {xsd}")
    return problems


def test_concurrent_validations_never_cross_contaminate() -> None:
    assert schema_errors(VALID) == []
    assert schema_errors(ALPHA) and schema_errors(BETA)
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(_check, range(3000)))
    problems = [p for r in results for p in r]
    assert problems == [], f"{len(problems)} contaminated results, e.g. {problems[:3]}"


def test_each_thread_compiles_its_own_schema() -> None:
    # Keep the schemas alive (an id() of a collected object may be reused) and start the
    # threads together so all three are live at once.
    kept: list[object] = []
    lock = threading.Lock()
    barrier = threading.Barrier(3)

    def grab() -> None:
        barrier.wait()
        schema = exchange_schema()
        with lock:
            kept.append(schema)
        barrier.wait()  # no thread ends before all three have their schema

    threads = [threading.Thread(target=grab) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(kept) == 3
    assert all(a is not b for i, a in enumerate(kept) for b in kept[i + 1 :])
    assert exchange_schema() is exchange_schema()
