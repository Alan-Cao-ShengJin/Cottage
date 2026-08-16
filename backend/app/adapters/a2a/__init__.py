"""A2A adapter package. Implementation is planned for M2.2 (see ``docs/ROADMAP.md``).

Reserved deliberately rather than left absent, so the adapter boundary in
docs/ARCHITECTURE.md §5 has a home and nobody is tempted to reach into `core`
from an A2A-shaped handler bolted onto the HTTP router.

This package intentionally exports no implementation yet. The audited translation and
security contract is in ``docs/INTEROP.md`` §6; the executable planned/implemented boundary
is ``tests/test_transport_conformance.py``.
"""
