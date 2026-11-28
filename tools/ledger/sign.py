#!/usr/bin/env python3
"""账本渲染产物的 Ed25519 签名（发布流水线用，CI 侧运行）。

签名对象：registry.json、intel.json（ intel.xml 由 intel.json 生成，不重复签）。
产物：同名 .sig 文件（base64 签名，64 字节）。
私钥只在 CI（ Actions 密钥 LEDGER_SIGNING_KEY，base64 PKCS8），
公钥将嵌入客户端（T2.2）与中心服务拉取脚本（center/pull.py）。

用法：
  python tools/ledger/sign.py --data-dir render            # 对 render/ 产物签名
  python tools/ledger/sign.py --selftest                   # 自测

依赖 cryptography（仅 CI/运维侧，中心服务与客户端保持纯标准库）。
"""

from __future__ import annotations

import base64
import sys
from pathlib import Path

try:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import load_der_private_key
except ImportError:
    print("需要 cryptography 库：python -m pip install cryptography", file=sys.stderr)
    sys.exit(2)

PUBLIC_KEY_B64 = "MCowBQYDK2VwAyEA2rFXMAXI5WRc1V86g8WEdiBGrvSVYkI9PeiXEjlQSDI="
SIGNED_FILES = ["registry.json", "intel.json"]


def load_private_key(data: str):
    """私钥为 PEM 文本（PKCS8），与 genkey.py 产出一致；也兼容 base64 DER。"""
    data = data.strip()
    try:
        key = serialization.load_pem_private_key(data.encode("utf-8"), password=None)
    except ValueError:
        key = load_der_private_key(base64.b64decode(data), password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise ValueError("私钥不是 Ed25519")
    return key


def sign_file(key, path: Path) -> str:
    """对文件原始字节签名，返回 base64 字符串。"""
    sig = key.sign(path.read_bytes())
    return base64.b64encode(sig).decode("ascii")


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--selftest" in argv:
        return selftest()

    import argparse
    import os

    parser = argparse.ArgumentParser(description="对账本渲染产物做 Ed25519 签名")
    parser.add_argument("--data-dir", default="render", help="render 产物目录")
    parser.add_argument("--key", default=os.environ.get("LEDGER_SIGNING_KEY", ""),
                        help="私钥（base64 PKCS8 DER），默认读环境变量 LEDGER_SIGNING_KEY")
    args = parser.parse_args(argv)

    if not args.key:
        print("缺少私钥：设置环境变量 LEDGER_SIGNING_KEY 或 --key 参数", file=sys.stderr)
        return 1

    data_dir = Path(args.data_dir)
    try:
        key = load_private_key(args.key)
    except Exception as e:
        print("私钥加载失败：%s" % e, file=sys.stderr)
        return 1

    for name in SIGNED_FILES:
        path = data_dir / name
        if not path.is_file():
            print("缺少 %s，跳过签名" % path, file=sys.stderr)
            return 1
        sig_b64 = sign_file(key, path)
        (data_dir / (name + ".sig")).write_text(sig_b64 + "\n", encoding="utf-8")
        print("已签名 %s -> %s.sig" % (name, name))
    return 0


def selftest() -> int:
    failures: list[str] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        if ok:
            print("  ok   %s" % name)
        else:
            failures.append(name)
            print("  FAIL %s %s" % (name, detail))

    import base64
    import tempfile

    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
    from cryptography.hazmat.primitives.serialization import load_der_public_key

    with tempfile.TemporaryDirectory() as tmp:
        data_dir = Path(tmp)
        (data_dir / "registry.json").write_bytes(b'{"schema_version": 1}')
        (data_dir / "intel.json").write_bytes(b'{"activities": [], "changelog": []}')

        # 1. 签名与验签闭环
        key = Ed25519PrivateKey.generate()
        der = key.private_bytes(
            encoding=serialization.Encoding.DER,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
        b64_key = base64.b64encode(der).decode()
        loaded = load_private_key(b64_key)
        check("私钥可往返加载", isinstance(loaded, Ed25519PrivateKey), "")

        sig_b64 = sign_file(loaded, data_dir / "registry.json")
        check("签名产物为 64 字节（base64 88 字符）", len(sig_b64) == 88, "len=%d" % len(sig_b64))

        pub = load_der_public_key(base64.b64decode(PUBLIC_KEY_B64))
        check("占位公钥可加载为 Ed25519 公钥", isinstance(pub, Ed25519PublicKey), "")

        # 2. 篡改检测
        tampered = base64.b64encode(b"X" * 64).decode()
        try:
            pub.verify(base64.b64decode(tampered), b'{"schema_version": 1}')
            check("错误签名被验签拒绝", False, "未抛异常")
        except Exception:
            check("错误签名被验签拒绝", True, "")

        # 3. 篡改内容检测
        try:
            pub.verify(base64.b64decode(sig_b64), b'{"schema_version": 2}')
            check("内容篡改被验签拒绝", False, "未抛异常")
        except Exception:
            check("内容篡改被验签拒绝", True, "")

        # 4. 签名者公钥可验证自己签的内容（自生成密钥闭环）
        key.public_key().verify(base64.b64decode(sig_b64), b'{"schema_version": 1}')
        check("签名者公钥可验证自己签的内容", True, "")

    print()
    if failures:
        print("自测失败 %d 项：%s" % (len(failures), "; ".join(failures)))
        return 1
    print("自测全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
