"""Ed25519 验签（纯标准库，设计文档 5.2 / dev-plan T2.2）。

客户端运行环境约束 A3：纯标准库、不装任何依赖。签名由 CI 流水线
（tools/ledger/sign.py，运维侧可用 cryptography）生成并随产物发布；
这里只负责验：公钥 + base64 签名 + 原始字节，任何一个字节被篡改即拒绝。

实现为 RFC 8032 的纯 Python 参考（Ed25519 域运算）：
  sign(seed, message)              -> 64 字节签名（自测与工具侧用）
  verify(public, signature, message) -> bool
  load_public_key(text)            -> 32 字节裸公钥（SPKI DER base64 或裸 base64）
  verify_b64(public_b64, sig_b64, message) -> bool

自测覆盖：RFC 8032 向量（空消息）、与 cryptography 的随机密钥交叉验证、
篡改失败、公钥格式解析（自建中心配置自己的公钥）。
"""

from __future__ import annotations

import base64
import hashlib
import sys

__all__ = ["sign", "verify", "load_public_key", "verify_b64", "selftest"]

# Ed25519 参数：p = 2^255-19，l 为群阶
_q = 2**255 - 19
_l = 2**252 + 27742317777372353535851937790883648493


def _inv(x: int) -> int:
    return pow(x, _q - 2, _q)


_d = -121665 * _inv(121666) % _q
_I = pow(2, (_q - 1) // 4, _q)

_by = 4 * _inv(5) % _q


def _xrecover(y: int) -> int:
    xx = (y * y - 1) * _inv(_d * y * y + 1) % _q
    x = pow(xx, (_q + 3) // 8, _q)
    if (x * x - xx) % _q != 0:
        x = (x * _I) % _q
    if x % 2 != 0:
        x = _q - x
    return x


_bx = _xrecover(_by)
_B = (_bx % _q, _by % _q, 1, _bx * _by % _q)
_IDENTITY = (0, 1, 1, 0)


def _add(P, Q):
    x1, y1, z1, t1 = P
    x2, y2, z2, t2 = Q
    a = (y1 - x1) * (y2 - x2) % _q
    b = (y1 + x1) * (y2 + x2) % _q
    c = 2 * t1 * t2 * _d % _q
    dd = 2 * z1 * z2 % _q
    e = b - a
    f = dd - c
    g = dd + c
    h = b + a
    return (e * f % _q, g * h % _q, f * g % _q, e * h % _q)


def _scalarmult(P, e: int):
    R = _IDENTITY
    while e > 0:
        if e & 1:
            R = _add(R, P)
        P = _add(P, P)
        e >>= 1
    return R


def _encode_point(P) -> bytes:
    x, y, z, _ = P
    zi = _inv(z)
    x = x * zi % _q
    y = y * zi % _q
    return int.to_bytes(y | ((x & 1) << 255), 32, "little")


def _decode_point(s: bytes):
    if len(s) != 32:
        raise ValueError("点长度不是 32 字节")
    y = int.from_bytes(s, "little")
    sign = y >> 255  # 编码最高位是 x 的符号位，先剥掉再查 y 的规范性
    y &= (1 << 255) - 1
    if y >= _q:
        raise ValueError("非规范编码（y >= p）")
    xx = (y * y - 1) * _inv(_d * y * y + 1) % _q
    x = pow(xx, (_q + 3) // 8, _q)
    if (x * x - xx) % _q != 0:
        x = (x * _I) % _q
    if x % 2 != sign:
        x = _q - x
    return (x, y, 1, x * y % _q)


def _clamp(s: bytes) -> int:
    """RFC 8032 的私钥标量整理：清低位、置高位。"""
    a = int.from_bytes(s, "little") & ((1 << 254) - 8)
    a |= 1 << 254
    return a


def public_key(seed: bytes) -> bytes:
    """种子（32 字节）→ 公钥（32 字节裸编码）。"""
    h = hashlib.sha512(seed).digest()
    return _encode_point(_scalarmult(_B, _clamp(h[:32])))


def sign(seed: bytes, message: bytes) -> bytes:
    """私钥种子（32 字节）→ 64 字节签名。仅自测与工具侧使用。"""
    h = hashlib.sha512(seed).digest()
    a = _clamp(h[:32])
    pk = _encode_point(_scalarmult(_B, a))
    r = int.from_bytes(hashlib.sha512(h[32:] + message).digest(), "little") % _l
    R = _encode_point(_scalarmult(_B, r))
    k = int.from_bytes(hashlib.sha512(R + pk + message).digest(), "little") % _l
    return R + int.to_bytes((r + k * a) % _l, 32, "little")


def _eq(P, Q) -> bool:
    x1, y1, z1, _ = P
    x2, y2, z2, _ = Q
    return (x1 * z2 - x2 * z1) % _q == 0 and (y1 * z2 - y2 * z1) % _q == 0


def verify(public: bytes, signature: bytes, message: bytes) -> bool:
    """验签。任意字节被篡改 → False；格式非法也 False（不抛异常）。"""
    if len(public) != 32 or len(signature) != 64:
        return False
    try:
        A = _decode_point(public)
        R = _decode_point(signature[:32])
    except (ValueError, ZeroDivisionError):
        return False
    s = int.from_bytes(signature[32:], "little")
    if s >= _l:
        return False
    k = int.from_bytes(hashlib.sha512(signature[:32] + public + message).digest(),
                       "little") % _l
    return _eq(_scalarmult(_B, s), _add(R, _scalarmult(A, k)))


def load_public_key(text: str) -> bytes:
    """公钥字符串 → 32 字节裸公钥。

    接受两种形态：
      * Ed25519 SPKI DER（44 字节）的 base64 —— CI/运维侧导出的标准形态；
      * 32 字节裸公钥的 base64 或 hex: 前缀 —— 自建中心可直接填裸钥。
    """
    value = str(text).strip()
    if value.startswith("hex:"):
        raw = bytes.fromhex(value[4:])
    else:
        try:
            raw = base64.b64decode(value, validate=True)
        except Exception as exc:
            raise ValueError("公钥不是合法 base64：%s" % exc) from exc
    spki = bytes.fromhex("302a300506032b6570032100")
    if len(raw) == 44 and raw[:12] == spki:
        return raw[12:]
    if len(raw) == 32:
        return raw
    raise ValueError("无法识别的 Ed25519 公钥（需要 32 字节裸钥或 SPKI DER 的 base64）")


def verify_b64(public_b64: str, sig_b64: str, message: bytes) -> bool:
    """配置里存公钥字符串、签名是 base64 的便捷验签。"""
    try:
        public = load_public_key(public_b64)
        signature = base64.b64decode(str(sig_b64).strip(), validate=True)
    except Exception:
        return False
    return verify(public, signature, message)


def selftest() -> bool:
    ok = True

    def check(name: str, cond: bool) -> None:
        nonlocal ok
        print(("ok   " if cond else "FAIL ") + name)
        ok &= cond

    # RFC 8032 §7.1 向量 1（空消息）：公钥/签名为文档原文
    seed = bytes.fromhex(
        "9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60")
    pub = bytes.fromhex(
        "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a")
    sig = bytes.fromhex(
        "e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e06522490155"
        "5fb8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b")
    check("RFC 8032 向量 1（空消息）验签通过",
          verify(pub, sig, b"") and sign(seed, b"") == sig)

    # 有 cryptography 时用随机密钥对交叉验证 sign/verify 与参考实现一致
    try:
        import os
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        for i in range(10):
            key = Ed25519PrivateKey.generate()
            sk = key.private_bytes(
                encoding=serialization.Encoding.Raw,
                format=serialization.PrivateFormat.Raw,
                encryption_algorithm=serialization.NoEncryption())
            pk = key.public_key().public_bytes(
                encoding=serialization.Encoding.Raw,
                format=serialization.PublicFormat.Raw)
            message = os.urandom(64)
            ref = key.sign(message)
            check(f"随机密钥对 {i + 1}：与参考实现签名一致且验签通过",
                  public_key(sk) == pk and sign(sk, message) == ref
                  and verify(pk, ref, message))
    except ImportError:
        print("note 未安装 cryptography，跳过交叉验证")

    # 篡改：消息 / 签名 / 公钥任何一个字节变化都必须失败
    check("篡改消息后验签失败", not verify(pub, sig, b"x"))
    check("篡改签名后验签失败", not verify(pub, sig[:-1] + bytes([sig[-1] ^ 1]), b""))
    check("错误公钥后验签失败", not verify(bytes([pub[0] ^ 1]) + pub[1:], sig, b""))
    check("长度非法的签名/公钥返回 False（不抛异常）",
          not verify(pub, sig[:63], b"") and not verify(pub[:31], sig, b""))

    # 公钥解析：SPKI DER base64（CI 形态）、裸 base64、hex 前缀（自建中心形态）
    spki_b64 = base64.b64encode(
        bytes.fromhex("302a300506032b6570032100") + pub).decode()
    check("SPKI DER base64 公钥解析", load_public_key(spki_b64) == pub)
    check("裸 base64 公钥解析", load_public_key(base64.b64encode(pub).decode()) == pub)
    check("hex: 前缀公钥解析", load_public_key("hex:" + pub.hex()) == pub)

    def _raises(text: str) -> bool:
        try:
            load_public_key(text)
        except ValueError:
            return True
        return False

    check("非法公钥报 ValueError", _raises("not-a-key") and _raises("AAAA"))

    # CI 产物形态：真实 render 产物 + 签名端到端（存在时）
    import os
    render = os.environ.get("FACET_RENDER", "")
    if render and os.path.isdir(render):
        for name in ("registry.json", "intel.json"):
            path = os.path.join(render, name)
            sig_path = path + ".sig"
            if not (os.path.isfile(path) and os.path.isfile(sig_path)):
                print(f"note {render} 缺少 {name}(+.sig)，跳过该产物")
                continue
            data = open(path, "rb").read()
            sig_text = open(sig_path, encoding="utf-8").read().strip()
            check(f"{name} CI 产物验签通过", verify_b64(spki_b64, sig_text, data))

    print("自测通过" if ok else "自测失败")
    return ok


if __name__ == "__main__":
    sys.exit(0 if selftest() else 1)
