# License and SBOM Notes

The core is Apache License 2.0. `LICENSE` and `NOTICE` are distributed at the
repository root. The project allowlist for directly reused code is MIT or
Apache-2.0; repositories without a root license are research references only.
GPL and AGPL code is not included.

Before a release, generate a dependency inventory from the lock files and record
package name, version, license, source URL and whether it is runtime or dev-only.
The inventory should include Python and npm dependencies and be attached to the
release as `SBOM.spdx.json` or CycloneDX equivalent. Every vendored file must
retain its SPDX header and be listed in `NOTICE`.

Recommended CI checks:

```text
pip-audit / osv-scanner for Python dependencies
npm audit --omit=dev for the web runtime
reuse lint for SPDX and copyright headers
syft dir:. -o spdx-json > SBOM.spdx.json
```

Provider credentials, generated media and user prompts are runtime data and must
not be committed. Use `.env.example` for configuration names only.
