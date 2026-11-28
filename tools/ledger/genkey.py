#!/usr/bin/env python3
"""Generate the Ed25519 signing keypair for the ledger artifacts.

The private key signs rendered ledger artifacts; the public key is embedded
in the client and verifies them. Run this once at launch:

    python tools/ledger/genkey.py            # prints both keys to the console
    python tools/ledger/genkey.py --out keys # writes public.pem (+ private.pem
                                            # with --save-private) into keys/

The private key must end up in the GitHub Actions secret LEDGER_SIGNING_KEY
and nowhere else. It is never written to disk unless --save-private is given.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

SELF_TEST_PAYLOAD = b"facet-ledger key check"


def load_public(pem: str) -> Ed25519PublicKey:
    return serialization.load_pem_public_key(pem.encode())  # type: ignore[return-value]


def self_test(private_key: Ed25519PrivateKey, public_pem: str) -> bool:
    """Sign a known payload, verify it, then prove tampering is caught."""
    public_key = load_public(public_pem)
    signature = private_key.sign(SELF_TEST_PAYLOAD)
    try:
        public_key.verify(signature, SELF_TEST_PAYLOAD)
    except InvalidSignature:
        print("self-test FAILED: valid signature rejected")
        return False
    try:
        public_key.verify(signature, SELF_TEST_PAYLOAD + b"x")
    except InvalidSignature:
        pass  # expected: tampered payload must fail
    except Exception as exc:  # noqa: BLE001 - any other error is a real bug
        print(f"self-test FAILED: unexpected error {exc!r}")
        return False
    else:
        print("self-test FAILED: tampered payload accepted")
        return False
    print("self-test PASSED: valid signature verifies, tampering is rejected")
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", metavar="DIR",
                        help="write public.pem into DIR (private.pem too "
                             "when --save-private is also given)")
    parser.add_argument("--save-private", action="store_true",
                        help="write private.pem into --out DIR; the default "
                             "is console-only so the key never touches disk")
    args = parser.parse_args()

    private_key = Ed25519PrivateKey.generate()
    private_pem = private_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    public_pem = private_key.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode()

    if not self_test(private_key, public_pem):
        return 1

    print("\nPRIVATE KEY — paste into GitHub Actions secret LEDGER_SIGNING_KEY:")
    print(private_pem)
    print("PUBLIC KEY — to be embedded in the client (T2.2):")
    print(public_pem)

    if args.out:
        out_dir = Path(args.out)
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "public.pem").write_text(public_pem, encoding="utf-8")
        print(f"\nwrote {out_dir / 'public.pem'}")
        if args.save_private:
            (out_dir / "private.pem").write_text(private_pem, encoding="utf-8")
            print(f"wrote {out_dir / 'private.pem'} — delete it after it is "
                  "in the GitHub secret")
        else:
            print("private key left on the console only; rerun with "
                  "--save-private if you want it on disk")

    print("\nDo not commit either key. The private key must not appear in "
          "logs, issues or chat history.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
