# ADR-020: M6 XML dependency index and Catalog Validator adapter interface

Status: Proposed. Date: 2026-11-21.

## Context
ARCHITECTURE.md section 10 lists an "XML indexer", "catalog dependency resolver" and Lens runner for the Domain plugin, and
the inventory list in the architecture doc names "Catalog Validator invocation" and the catalog layout as items still to be supplied.
The XML schema, the reference attribute names and the Lens command line are therefore not specified. Implementing a guessed
schema or command would be unsafe, so this ADR fixes only a neutral interface; everything catalog-specific is configuration.

## Decision
1. `plugins/domain_catalog/integrations/xml_index.py` is a pure library (stdlib only, no I/O beyond reading files the caller
   names, no daemon wiring, no core imports). It is loaded explicitly, never implicitly (ADR-011).
2. Safe parsing: any DTD or entity declaration (`<!DOCTYPE`, `<!ENTITY`) is rejected before parsing, which blocks XXE and
   billion-laughs by construction. Inputs are size-capped (per file, files per repo, nodes, edges, nesting depth) and exceeding
   a cap raises `XmlIndexError`; nothing is silently truncated.
3. Schema-neutral extraction: an element carrying one of `IndexConfig.id_attrs` defines a node; an attribute in
   `IndexConfig.ref_attrs` is a reference from the nearest enclosing defining element to the target id. Defaults
   (`id`/`name`, `ref`/`href`/`refId`) are UNVERIFIED placeholders.
4. Node key is `(repo_id, id)`. A reference resolves to the same repo if defined there, else to the unique definition in
   another repo; zero matches is `unresolved`, several is `ambiguous`. Neither is guessed.
5. Query API: `dependencies`, `dependents`, `transitive_dependencies`, `transitive_dependents` (optional max_depth,
   cycle-safe). Output is sorted; `to_dict()` is canonical and `sha256()` gives a digest usable in manifests.
6. Catalog Validator adapter: `LensAdapter` Protocol plus `AllowlistedLensAdapter`, which only builds a `PlannedLensCommand`
   (argv tuple, never a shell string) from an operator-supplied absolute executable. Arguments with write/deploy verbs are
   refused. It does not execute; execution, if ever added, goes through the supervisor and PolicyPort.

## Alternatives
Guess the catalog schema (rejected: unsafe); add a `defusedxml` dependency (rejected: DOCTYPE rejection is sufficient and
adds no dependency); execute Lens now (rejected: command unknown).

## Risks and consequences
Defaults may not match the real catalog; set config after inventory. Rejecting every DTD may refuse legitimate catalog files
that use one; this is a deliberate fail-closed choice. The Lens deny-list is a heuristic, not a security boundary.
