## Summary

What this pull request changes, and why.

## Checklist

- [ ] Tests pass locally: `pip install -e . pytest ruff`, then `ruff check .` and `PYTHONDONTWRITEBYTECODE=1 python -m pytest -q -p no:cacheprovider tests`.
- [ ] `docs/protocol.md` is updated if any wire type, route, or field changed (parity gate: `tests/test_protocol_parity.py`).
- [ ] Nothing in `ormas_subnet` imports `tensorbox_spec` (gate: `tests/test_import_graph.py`).
- [ ] No secrets (tokens start `ormr_`) and no client repository content (source, diffs, prompts) in the diff.
- [ ] Vocabulary rule followed: **miner**, **firm ask**, **accepted delivery**, **validator** — never "runner", "supplier", "inference miner", or "marketplace".
