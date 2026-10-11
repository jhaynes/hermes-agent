# U0 freeze manifest — seam contract + probe suite

Frozen: 2026-10-10 (U0, task t_9b97698a, branch `candidate/review-accounting-v1` on `jhaynes/hermes-agent`, built on pinned commit `e2427daf3d7f5ef90b0dc196d89fee4a2debb0d4`).

Plan v3 sha256 verified on read before any work: `1f7cd6d13c65457581d84e8f30810549f177cdea9c2a9c18462b668183ec5e7e`.

| Artifact | sha256 |
|---|---|
| `seam-contract-v2.md` (contract prose + capabilities + probes + traceability) | `99c306c19456f8a8748f41bd3844724d0da274eda5431c477476df5bf69a6886` |
| `seam-contract-v2.schema.json` (machine-readable schema naming every probe ID) | `c34a2fecce84a166f37da24ef04131c6eaaffd992a8a236fd9aaa7380f115603` |
| `seam-contract-v2.json` (instance document, schema-validated: 66 probes, 5 capabilities, 20 traceability rows) | `0a72d7bc5e2f6b2e55b37652144246c91e132ef0d2a7c4576b3896be4d6677a4` |
| `gen_instance.py` (deterministic generator: prose is source of truth, JSON derived from it) | `aede58dab6d8da9978d56ecd9a918a07fbe3d4c2653977d07d1ce96f2005ee2b` |

Contents of the frozen probe suite: 25 re-derived baseline behavioral probes (each citing its Phase A receipt evidence) + 41 new per-capability probes (U0 sanity x2, U1 x5, U2 x10, U3 x8, U4 x7, U5 x9). All U1-U5 probes reference frozen plan-v2 clauses via the traceability matrix. Schema validation passed (`jsonschema`, draft 2020-12).

Amendment rule: changes are decisions — contract version bumps to `v3`, every digest in this manifest is recomputed (regenerate the instance with `python3 docs/contracts/gen_instance.py`), and the prior version stays in git history. U6 judges against one specific contract SHA.

Maker-independence note (plan v3 U0): the probe suite is authored under the maker-independence rule — the builder implementing U1-U5 must not be the sole author of the probes that will judge that work. This freeze is submitted for independent review on the U0 card; reviewer approval of this digest is the maker-independence record.

Scope note: this commit is docs-only. It does not modify candidate runtime code, tests, or any other surface (plan v3 U0: "a docs-only commit, not an implementation change").
