# Contributing to ormas-subnet

- **Protocol changes start with an issue.** Before any PR that changes the wire
  protocol, a wire data type, or the scope of `docs/DECISIONS.md`, open an issue
  and get a maintainer response first.
- **Tests must pass.** CI (`.github/workflows/ci.yml`) runs `ruff check .` and
  `PYTHONDONTWRITEBYTECODE=1 python -m pytest -q -p no:cacheprovider tests` on
  Python 3.10 and 3.12. Run both locally before opening a PR.
- **Wire changes are documented.** A change to `ormas_subnet/protocol.py` needs a
  matching entry in `docs/protocol.md` in the same PR.
- **The parity and import-graph tests are the gate.**
  `tests/test_protocol_parity.py` (runs in the owner's private monorepo) holds the
  public data types wire-identical to the server's contract, and
  `tests/test_import_graph.py` holds `ormas_subnet` free of `tensorbox_spec`
  imports. Both must stay green.
- **No secrets, no client repository content.** Tokens start `ormr_`; they come
  from env or file, never argv, never committed. Do not paste source, diffs,
  prompts, or credentials from a client repository into an issue, PR, or fixture.
  The only production hostname allowed in fixtures is `api.ormas.ai`.
- **License.** Contributions are under this repository's MIT license.
- **Commercial and acceptance terms** live in
  [`docs/CONTRACT.md`](docs/CONTRACT.md). That document is owner-controlled; PRs
  do not change it.

Vocabulary in issues, PRs, code, and docs: **miner**, **firm ask**, **accepted
delivery**, **validator** — never "runner", "supplier", "inference miner", or
"marketplace".
