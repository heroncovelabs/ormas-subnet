# Verifier catalog public `.4` to `.6` transition

This release is the **stage-one compatibility build**. It preserves the
current public `2026-10-14.4` catalog and compiler as the default for newly
frozen packets,
while accepting packets frozen with either of these reviewed compiler identities:

- public `.4` (`sha256:398f6c9f…`): `2ba990540ac8653f130efe5ed132a0dfdca6048db18386352719d3a317ae2c00`
- `.6`: `bffacd5331642f9515f21ac8fd0ac634c24ea7f1f4ac1e50c655617c1c63124f`

The `.6` catalog digest is
`sha256:060d5d8b80cc47d050b55f169644d68f33ede3a0cda5cc588a008b805dfc2551`.
This build keeps it in catalog history so `.6` packets validate, but its local
runtime and current catalog remain public `.4`. Installing this build therefore does
not start producing `.6` packets.

## Release order

1. Build the public compatibility wheel twice from the exact reviewed commit;
   require byte-identical wheels and record the commit and wheel SHA-256.
2. Deploy and verify the private gateway accept bridge. It must retain its
   existing emitted catalog while accepting public `.4` and `.6` packets.
3. Install this public compatibility wheel on every eligible controlled miner,
   validator, and checker. Requalify each identity and record its installed
   source and wheel hashes.
   Preserve explicit public `.4` qualifications and add inspected `.6` qualification
   records before activation; a convenience approval derived from this build's
   current catalog creates only public `.4` evidence.
4. Disable or exclude any node that has not reported the compatibility build.
   Do not activate a `.6` producer while an eligible old node can receive work.
5. Only after the compatible fleet and gateway are verified may the separate
   activation release change the current catalog and runtime to `.6`.

Stop the rollout if the emitted catalog changes from public `.4`, the active compiler
set differs from the two identities above, a public `.4` packet stops validating, a
synthetic `.6` packet fails validation, or qualification cannot bind an exact
installed artifact to an eligible identity.

## Rollback

This compatibility build can be the public drain binary after a `.6` activation
only while the gateway uses the separately frozen postactivation drain release
and ordinary new admission is closed. The plain pre-bridge public `.4` build rejects
`.6` and is never a safe target. Preserve and renew exact `.6` qualification
records for the downgraded bridge nodes; their current public `.4` catalog does not
create `.6` qualifications automatically. Allow already-frozen `.6` packets to
finish or enter the normal preserved failure/recovery path. Do not rewrite
frozen packet inputs.

The private stage-one bridge is preactivation only. After `.6` admission begins,
use the postactivation drain candidate: plain
`a4fcba0b95cd440c9f56b7f701c27127b5f26306` rejects `.6`, while stage one would
reopen legacy fire admission.

## Finalization gate

Remove public `.4` from the public active set only in a later reviewed release after:

- no queued, leased, acceptance-pending, retryable, or recovery record refers
  to the public `.4` catalog or compiler;
- no terminal parent can still create a legacy repair and no retained validator
  assignment or recovery journal retains legacy execution authority;
- all packet-producing clients intended to remain eligible emit `.6`;
- every eligible miner, validator, and checker reports a qualified `.6` capable
  artifact; and
- the rollback package retained by operations still accepts both generations.

Finalization also requires a reviewed legacy repair-admission cutoff and an
evidence-only historical parser so revocation does not make retained accounting
records unreadable. No stage-one or activation release may remove `.5` execution
authority.

Use read-only inventory for production evidence. Do not create jobs, invoke
providers, submit chain transactions, or mutate receipts to prove the cutover.
