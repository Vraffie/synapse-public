# Domain Catalog Plugin

This plugin will provide Domain-specific workflows, XML dependency analysis, Catalog Validator validation, and approval-gated STAGING deployment. The core platform must remain provider- and domain-neutral.

## XML dependency index (ADR-020)

`integrations/xml_index.py` is a pure, read-only library: safe XML parsing (DTD/entity rejected, size limits), a cross-repo
dependency index with dependencies/dependents/transitive queries and a deterministic digest, and a plan-only Catalog Validator
adapter. Not wired into the daemon; attribute names and the Lens command are unverified placeholders.
