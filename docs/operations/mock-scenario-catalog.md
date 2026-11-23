# Mock scenario catalog (scenario-v1)

`MockRuntime.start(run_id, key, scenarios=None)` accepts an optional map `{node_id: name | [name, ...]}` over a closed, server-owned catalog in `synapse.persistence.mock_scenarios`. No client text, command or bytes reach the provider. Without `scenarios` the run is the unchanged `fixed-v1` fixture.

| Name | Provider output | Provider outcome | Verifier result |
| --- | --- | --- | --- |
| `succeed` | exact candidate | succeeded | approved |
| `fail` | none | failed | n/a (attempt fails) |
| `reject` | different bytes | succeeded | changes_required |
| `slow` | 200 empty chunks, then candidate | succeeded | approved (cancellable mid-run) |

A list gives one scenario per attempt (at most `MOCK_MAX_ATTEMPTS`), e.g. `["fail", "succeed"]` is a deterministic retry. Unlisted nodes default to `succeed`. Unknown nodes or names raise `ScenarioError` (a `ValueError`) before any state change.

## Persistence and kind

The canonical map is written once as a `mock.scenarios_configured` run event, with its SHA-256, in the transaction that starts the run. Scenario runs use profile `synapse.builtin.mock.scenario.v1`; the legacy CHECK-constrained `mock_run_config.fixture` column stays `fixed-v1`. `mock_kind()` derives `fixed-v1`, `scenario-v1` or `None` from the profile and event. A missing, duplicated or tampered event yields `None`, and the start, cancel, recovery and resume guards refuse the run. Start is idempotent on its key; the request payload includes the scenarios.

Resume of a recovered scenario run requires the assertion `scenario-v1-no-external-effects`.

## Coverage (`tests/test_mock_scenario_runtime.py`)

Success, exhausted failure (the run goes to `recovering` through conservative recovery), fail-then-succeed retry, reject-then-correct, cancellation of a `slow` run, replay, invalid inputs, duplicated-event rejection, and fixed runs unchanged.

## Approval-gated paths

The mock engine never parks a task: gating is driven by a trusted service (`task-approval-gates.md`), and `tests/test_task_approval_gate.py` covers gate semantics with the mock engine (parked tasks are ignored and the run reports `blocked`). No catalog name requests approval, so approval-gated scenarios are not exercised end to end here.

## Not covered

Scenarios are not yet exposed through the HTTP API, CLI or desktop; only `MockRuntime.start` accepts them.
