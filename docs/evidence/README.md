# Evidence

Every measurable claim made in the README or in `docs/` traces to a file here.
Each file is real output from a real run — nothing in this directory is written
by hand, and no figure appears in the documentation that does not originate in
one of these files.

| File | Claim it proves | Command that produced it |
|---|---|---|
| `gitleaks-full-history.txt` | Full git history has been scanned for credentials, with findings disclosed rather than hidden | `gitleaks detect --source . --redact --log-opts="--all"` |
| `module-purity-audit.txt` | Which modules are side-effect free, and therefore which tests need no mocking | `make audit-purity` |
| `test-suite-full.txt` | N tests passing, with timing | `pytest tests/unit tests/property -v` |
| `coverage-by-package.txt` | Per-package coverage floors are met | `pytest --cov --cov-report=term` |
| `reconciliation-divergence-table.txt` | All seven divergence categories are classified correctly, and adoption is idempotent | `pytest tests/unit/test_reconciliation.py -v` |
| `property-tests-hypothesis.txt` | Invariants P1–P7 hold over generated OHLCV series | `pytest tests/property -v` |
| `brain-isolation-test.txt` | `strategy/` imports nothing from the runtime layer | `pytest tests/unit/test_brain_isolation.py -v` |
| `pip-audit.txt` | No known-vulnerable dependencies | `pip-audit -r requirements.txt` |
| `docker-compose-test-run.txt` | The system builds and its tests pass in a container, with no credentials | `docker compose -f deploy/docker-compose.test.yml up --exit-code-from tests` |

## Rules for this directory

1. **Never edit a file here by hand.** If output is wrong, fix the cause and
   re-run the command.
2. **Never write a number into the documentation that is not in one of these
   files.** A single fabricated figure, once noticed, discounts everything else
   in the repository — correctly.
3. **Redact before committing.** Captured output may contain symbols, order
   identifiers, or balances. Strip anything that reveals position history, and
   strip every profit-and-loss field: a screenshot showing P&L is a performance
   claim regardless of the words around it. See
   [`../DISCLOSURE.md`](../DISCLOSURE.md).
4. **State the command.** An artefact nobody can reproduce is an assertion with
   extra steps.
