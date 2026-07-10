# Capability Contract

`manifest.json` is a generated, normalized snapshot of capabilities discovered from source.
`manifest.schema.json` comes from the discriminated Pydantic model. `policy.json` supplies
verification defaults, honest status annotations, required records, and temporary waivers.
`profiles.json` is the typed deployment-profile source snapshot.

## Update And Check

```powershell
python scripts/compare_capability_manifests.py --write
python scripts/compare_capability_manifests.py
$env:PYTHONPATH='src'
python -m pytest -q -c NUL --rootdir . tests/test_capability_contract.py
```

Pull requests compare the current manifest with the base-branch manifest. Removed records,
status downgrades, relationship removals, source drift, and removed verification nodeids fail.

## Evidence Status

- `declaration_only`: a name or routing declaration exists without an implementation.
- `partial`: implementation exists, but an important path or runtime claim remains unverified.
- `implemented`: the entry point exists without sufficient automated evidence for `verified`.
- `verified`: committed pytest nodeids resolve and cover the capability contract.
- `demo_verified`: verified plus explicit demo evidence; no capability claims this by default.

## Temporary Waivers

Waivers live in `policy.json`, name one capability exactly, identify the suppressed rules, and
expire within `max_waiver_days`. Expired or overlong waivers fail closed.

```json
{
  "id": "waiver:sap-maintenance",
  "capability_id": "connector:sap",
  "rules": ["downgrade", "missing_verification"],
  "reason": "Mapper maintenance temporarily removes verified SAP coverage.",
  "owner": "platform",
  "expires_on": "2026-07-20",
  "issue": "ACTII-142"
}
```
