"""M1 done-criteria: torn-tail NDJSON reads; chain detects out-of-band edits."""
from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from harness import chain, ndjson
from tests import support


class NdjsonLedger(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.ledger = self.root / "events.ndjson"

    def tearDown(self):
        support.rmtree(self.root)

    def test_append_and_read(self):
        ndjson.append_record(self.ledger, {"kind": "a"})
        ndjson.append_record(self.ledger, {"kind": "b"})
        kinds = [r["kind"] for r in ndjson.read_records(self.ledger)]
        self.assertEqual(kinds, ["a", "b"])
        self.assertTrue(all("at" in r for r in ndjson.read_records(self.ledger)))

    def test_torn_tail_is_tolerated(self):
        ndjson.append_record(self.ledger, {"kind": "a"})
        with self.ledger.open("a") as fh:
            fh.write('{"kind": "torn-mid-cra')  # crash mid-append
        records = ndjson.read_records(self.ledger)
        self.assertEqual([r["kind"] for r in records], ["a"])

    def test_append_after_torn_tail_isolates_the_fragment(self):
        """Adversarial-review finding: appending straight onto a torn tail
        merged two records into one unparseable line — silently dropping
        the NEW record (e.g. the human's captured APPROVED), and once a
        third record followed, every read of the ledger raised forever."""
        ndjson.append_record(self.ledger, {"kind": "a"})
        with self.ledger.open("a") as fh:
            fh.write('{"kind": "torn-mid-cra')  # crash mid-append
        ndjson.append_record(self.ledger, {"kind": "b"})   # must not merge
        ndjson.append_record(self.ledger, {"kind": "c"})
        records = ndjson.read_records(self.ledger)
        self.assertEqual([r["kind"] for r in records], ["a", "b", "c"])

    def test_corruption_not_at_tail_is_skipped_not_fatal(self):
        # skip-not-raise is deliberate: raising bricked every later gate
        # decide/event read on the run over one historical crash; a skipped
        # line can never QUALIFY as evidence, only be absent (fail-closed).
        with self.ledger.open("w") as fh:
            fh.write('not json\n{"kind": "b"}\n')
        self.assertEqual([r["kind"] for r in ndjson.read_records(self.ledger)],
                         ["b"])

    def test_strict_read_raises_on_corruption_for_trust_anchors(self):
        # adversarial-review finding: skip-not-raise is fail-OPEN for a
        # latest-wins consumer — a torn NEWEST record silently drops,
        # promoting an older, more-permissive one. Trust anchors read
        # strict and fail closed instead.
        ndjson.append_record(self.ledger, {"verdict": "APPROVED"})
        with self.ledger.open("a") as fh:
            fh.write('{"verdict": "CHANGES_REQ')  # torn newest
        with self.assertRaises(ndjson.LedgerCorruption):
            ndjson.read_records(self.ledger, strict=True)
        # non-strict still tolerates it (absence-based consumers)
        self.assertEqual(
            [r["verdict"] for r in ndjson.read_records(self.ledger)],
            ["APPROVED"])

    def test_record_with_unicode_line_separator_is_not_split(self):
        # adversarial-review finding: str.splitlines() breaks on U+2028/
        # U+2029/U+0085 (emitted literally by json.dumps ensure_ascii=False),
        # so a valid record containing one — common in pasted text — split
        # into fragments and vanished whole. Split on the writer's '\n'.
        for ch in (" ", " ", ""):
            led = self.root / f"u-{ord(ch)}.ndjson"
            ndjson.append_record(led, {"text": f"line one{ch}line two"})
            recs = ndjson.read_records(led)
            self.assertEqual(len(recs), 1, f"U+{ord(ch):04X} split the record")
            self.assertIn(ch, recs[0]["text"])

    def test_missing_file_reads_empty(self):
        self.assertEqual(ndjson.read_records(self.ledger), [])


class IntegrityChain(unittest.TestCase):
    def setUp(self):
        self.workspace = Path(tempfile.mkdtemp())
        self.key = chain.load_or_create_key(self.workspace)
        self.target = self.workspace / "state.yaml"

    def tearDown(self):
        support.rmtree(self.workspace)

    def test_seal_then_verify_round_trip(self):
        chain.seal(self.target, b"content-1", self.key)
        self.assertEqual(chain.verify(self.target, self.key), b"content-1")
        chain.seal(self.target, b"content-2", self.key)  # chained second write
        self.assertEqual(chain.verify(self.target, self.key), b"content-2")

    def test_out_of_band_edit_detected(self):
        chain.seal(self.target, b"legit", self.key)
        self.target.write_bytes(b"tampered via python -c")  # guard bypass
        with self.assertRaises(chain.IntegrityError):
            chain.verify(self.target, self.key)

    def test_label_binds_identity_not_just_content(self):
        # the red-proof replay seam: same content + same key, sealed under
        # label A, must not verify under label B (file copied to another
        # task's proof path) or label-less
        chain.seal(self.target, b"proof", self.key, label="redproof:T1")
        self.assertEqual(
            chain.verify(self.target, self.key, label="redproof:T1"), b"proof")
        with self.assertRaises(chain.IntegrityError):
            chain.verify(self.target, self.key, label="redproof:T2")
        with self.assertRaises(chain.IntegrityError):
            chain.verify(self.target, self.key)

    def test_label_domain_separation_no_collision_with_empty_label(self):
        # adversarial-review finding: the old `{seq}:{prev}:{label}:` form
        # let an empty-label seal over b"redproof:T1:"+X collide with a
        # label="redproof:T1" seal over X. The length-delimited labelled
        # encoding can't collide with the (unchanged) empty-label form.
        from harness.chain import _digest
        key = self.key
        X = b'{"task": "T1"}'
        self.assertNotEqual(
            _digest(key, 0, "", b"redproof:T1:" + X),          # empty label
            _digest(key, 0, "", X, label="redproof:T1"))       # labelled
        # and empty-label state.yaml seals are byte-identical to before
        # (backward compatible): the empty-label digest is unchanged
        self.assertEqual(_digest(key, 3, "abc", b"content"),
                         _digest(key, 3, "abc", b"content", label=""))

    def test_corrupt_or_empty_key_file_is_a_clean_integrity_error(self):
        # adversarial-review finding: a truncated key raised a raw
        # ValueError from bytes.fromhex with no remediation
        key_file = self.workspace / ".claude" / "context" / ".harness-key"
        key_file.write_text("not-hex!!")
        with self.assertRaises(chain.IntegrityError) as ctx:
            chain.load_or_create_key(self.workspace)
        self.assertIn("reseal", str(ctx.exception))
        key_file.write_text("")
        with self.assertRaises(chain.IntegrityError):
            chain.load_or_create_key(self.workspace)

    def test_key_file_is_owner_only_from_birth(self):
        import os
        import stat as stat_mod
        if os.name == "nt":
            # POSIX mode bits don't exist on Windows (os.open's 0o600 maps
            # to nothing; protection is NTFS ACLs, inherited) — the
            # owner-only guarantee is a POSIX-only assertion by nature
            self.skipTest("POSIX permission bits are not a Windows concept")
        key_file = self.workspace / ".claude" / "context" / ".harness-key"
        mode = stat_mod.S_IMODE(key_file.stat().st_mode)
        self.assertEqual(mode, 0o600)

    def test_missing_seal_detected(self):
        self.target.write_bytes(b"unsealed write")
        with self.assertRaises(chain.IntegrityError):
            chain.verify(self.target, self.key)

    def test_forged_seal_without_key_detected(self):
        chain.seal(self.target, b"legit", self.key)
        other_ws = Path(tempfile.mkdtemp())
        try:
            other_key = chain.load_or_create_key(other_ws)
            chain.seal(self.target, b"forged", other_key)  # wrong key
            with self.assertRaises(chain.IntegrityError):
                chain.verify(self.target, self.key)
        finally:
            support.rmtree(other_ws)

    def test_key_created_once_with_tight_perms(self):
        import os
        key2 = chain.load_or_create_key(self.workspace)
        self.assertEqual(self.key, key2)
        if os.name == "nt":  # created-once still asserted above; mode bits
            return           # are POSIX-only (see the sibling perms test)
        key_file = self.workspace / ".claude" / "context" / ".harness-key"
        self.assertEqual(key_file.stat().st_mode & 0o777, 0o600)

    def test_seal_leaves_no_tmp_files_behind(self):
        # Both the content AND the seal write are now tmp+os.replace
        # (adversarial-review finding: the seal file's own write used to be
        # a plain write_text, not atomic even by itself).
        chain.seal(self.target, b"content", self.key)
        leftovers = list(self.workspace.glob("*.tmp"))
        self.assertEqual(leftovers, [])


class Reseal(unittest.TestCase):
    """`harness reseal` — human-invoked recovery for a seal that's missing
    or unreadable (a crash between chain.seal's two writes, or genuine
    tampering — this module can't tell the two apart by design)."""

    def setUp(self):
        self.workspace = Path(tempfile.mkdtemp())
        self.key = chain.load_or_create_key(self.workspace)
        self.target = self.workspace / "state.yaml"

    def tearDown(self):
        support.rmtree(self.workspace)

    def test_reseal_recovers_from_a_missing_seal(self):
        self.target.write_bytes(b"content survived the crash")
        with self.assertRaises(chain.IntegrityError):
            chain.verify(self.target, self.key)
        chain.reseal(self.target, self.key)
        self.assertEqual(chain.verify(self.target, self.key),
                         b"content survived the crash")

    def test_reseal_continues_the_chain_from_a_readable_prior_seal(self):
        chain.seal(self.target, b"v1", self.key)
        chain.seal(self.target, b"v2", self.key)
        seal_file = self.target.with_name(self.target.name + ".hmac")
        prior = json.loads(seal_file.read_text(encoding="utf-8"))
        result = chain.reseal(self.target, self.key)
        self.assertEqual(result["seq"], prior["seq"] + 1)
        self.assertEqual(result["prev"], prior["hmac"])
        self.assertEqual(chain.verify(self.target, self.key), b"v2")

    def test_reseal_starts_a_fresh_chain_when_the_seal_itself_is_corrupt(self):
        chain.seal(self.target, b"v1", self.key)
        seal_file = self.target.with_name(self.target.name + ".hmac")
        seal_file.write_text("not json")
        chain.reseal(self.target, self.key)
        self.assertEqual(chain.verify(self.target, self.key), b"v1")

    def test_reseal_on_missing_content_file_refuses(self):
        with self.assertRaises(chain.IntegrityError):
            chain.reseal(self.target, self.key)

    def test_reseal_after_tampering_reestablishes_a_verifiable_chain(self):
        # This is the module's OWN documented residual: reseal can't
        # distinguish "crashed mid-write" from "tampered then deleted the
        # seal" — it re-baselines either way. The audit trail (the CLI's
        # `reseal` event) is what makes this visible, not this function.
        chain.seal(self.target, b"legit", self.key)
        self.target.write_bytes(b"tampered content")
        with self.assertRaises(chain.IntegrityError):
            chain.verify(self.target, self.key)
        chain.reseal(self.target, self.key)
        self.assertEqual(chain.verify(self.target, self.key), b"tampered content")


if __name__ == "__main__":
    unittest.main()
