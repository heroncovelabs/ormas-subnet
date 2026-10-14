# Changelog

## 0.0.2 — 2026-10-14

- Accept frozen packets on the deployed `2026-10-14.4` catalog
  (`sha256:ff7c1e51b36c54f8aa8b63d2b9b6ea91c4d1587ee39bc31575b7763ffbf40367`)
  only with its draining verifier compiler
  (`214eadfb183270c187f0dba07ad16123ea402a90dcd2e13d883b906d2060db1c`).
  This older embedded verifier predates trusted-tool hardening. The allowance
  matches the gateway's draining policy but is scoped to the exact pair.
- Keep the public `.4` catalog (`sha256:398f6c9f…`, compiler `2ba99054…`) as the
  default. Revision labels alone do not identify a catalog. Revoked compilers,
  including `6208cd15…`, and revoked acceptance drivers remain refused.
- Before restarting on this version, finish running leases and confirm delivery,
  complete or let outstanding awards lapse, and resolve recovery-pending work on
  the old build. Keep polling while draining; queued jobs never offered on need
  no action. Back up state; do not delete unresolved recovery records. After
  upgrading, run `ormas-miner doctor` and re-register for qualification on the
  deployed catalog digest. This release does not add other future catalogs.
