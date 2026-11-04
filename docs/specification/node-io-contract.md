# Node I/O Contract v1

Every node receives `NodeInput` and returns `NodeOutput`. Pydantic rejects undeclared fields so accidental prompt or handler drift is visible.

`NodeInput` contains:

- run/node identity and attempt number
- immutable artifact references
- Claim references
- explicit parameters
- bounded execution context

`NodeOutput` contains:

- terminal node status
- created or updated artifact references
- Claim changes
- typed Defects and Metrics
- blocking or non-blocking Human Review requests
- diagnostic messages and metadata

Large document contents are not copied through the contract. Nodes exchange references carrying optional Revision and SHA-256 identities.
