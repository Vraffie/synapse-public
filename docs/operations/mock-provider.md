# Mock provider

`MockProvider` runs named, caller-supplied `MockScenario` fixtures without external processes or credentials. Each execution emits a started event, its fixed output chunks, and one success or failure event. Cancellation produces a cancelled terminal event and stops remaining chunks. An execution has one stream consumer and cannot be streamed again after it finishes or the consumer closes it.

Execution state exists only in memory. The future worker supervisor must persist orchestration state and reconcile interrupted executions after daemon restart. Provider capability probing, usage observations, scheduling and API wiring are not implemented by this adapter.

`probe` reports the in-process adapter as healthy for a valid profile identifier. `capabilities` reports streaming, cancellation and structured events as supported, and resume, MCP, usage and quota telemetry as unsupported. The context limit and usage observation are unknown.

The shared provider contract defines typed health, capabilities, execution requests, events and usage observations. The mock adapter accepts an `ExecutionRequest` whose input contains a `scenario` name. The existing string scenario argument remains supported. Request input is an immutable JSON snapshot.

`WorkerSupervisor` bounds concurrent provider executions to at most six, rejects duplicate active task IDs, forwards provider events to an asynchronous sink, and supports cancelling queued and running tasks. It cancels an active provider execution when event delivery fails. Its state is in process and it does not persist attempts, provide process-group supervision, or reconcile a restart. Use it for mock orchestration until durable supervision is implemented.

The dependency planner validates a complete task snapshot and selects ready tasks whose dependencies are completed. It orders by ascending priority and task ID and honors the supplied capacity, up to six. Callers must obtain a consistent snapshot and recheck eligibility when claiming tasks. Resource locks, profile eligibility, budgets and persistent scheduling remain application work.

Attempt reservation is a daemon-owned transaction: it requires an assigned task and profile, rejects unresolved attempts, and records its event and outbox atomically. It does not launch a worker or reconcile external effects.

Interrupted attempt recovery can mark unresolved executions unknown in one transaction. Call it only after fencing or stopping old workers: it does not itself fence them, confirm external effects, or authorize retry. Unknown attempts remain unresolved until reconciliation.

The task claim repository checks run and dependency state, enabled profile, and worker/profile capacity under a caller-owned `BEGIN IMMEDIATE` transaction. Application authorization, health, budgets and resource locks remain the caller's responsibility. The generic transition repository can still assign a task without these checks: do not expose it as an assignment command; route assignment through claim. Worker fencing remains required before unresolved capacity can safely be released.
