"""种子文件（.torrent）解析：bencode 解码 + infohash 计算。

用途：用户发 .torrent 直链或文件到 userbot 时，提取 infohash 构造磁力
链接，复用 CD2 离线下载（AddOfflineFiles）把内容离线到 115——CD2 的
离线接口只吃磁力/sha1 链接，不吃种子文件本身。

bencode 解码为**最小实现**（~30 行，无依赖）：只为取 info 段与展示字段
（name/files/length/private），不追求通用完整性。infohash 按规范对
info 段的**原始字节**切片算 SHA1——从原始数据定位切片，不经重编码。
"""
import hashlib
import os
import urllib.parse as _urlparse


class BencodeError(ValueError):
    pass


def _decode(data, i):
    """从 data[i] 解码一个值，返回 (值, 下一位置)。"""
    c = data[i:i + 1]
    if c == b"i":                                   # 整数 i<num>e
        j = data.index(b"e", i)
        return int(data[i + 1:j]), j + 1
    if c == b"l":                                   # 列表 l<...>e
        i += 1
        out = []
        while data[i:i + 1] != b"e":
            v, i = _decode(data, i)
            out.append(v)
        return out, i + 1
    if c == b"d":                                   # 字典 d<...>e
        i += 1
        out = {}
        while data[i:i + 1] != b"e":
            k, i = _decode(data, i)
            v, i = _decode(data, i)
            out[k] = v
        return out, i + 1
    j = data.index(b":", i)                         # 字符串 <len>:<bytes>
    n = int(data[i:j])
    return data[j + 1:j + 1 + n], j + 1 + n


def bdecode(data):
    """bencode 字节串 → Python 对象；损坏抛 BencodeError。"""
    try:
        value, i = _decode(data, 0)
    except (ValueError, IndexError) as e:
        raise BencodeError(f"bencode 损坏：{e}") from e
    if i != len(data):
        raise BencodeError("bencode 尾部有多余数据")
    return value


def _skip(data, i):
    """跳过 data[i] 起的一个完整值，返回结束位置（不构建对象，快）。"""
    c = data[i:i + 1]
    if c == b"i":
        return data.index(b"e", i) + 1
    if c in b"ld":
        i += 1
        while data[i:i + 1] != b"e":
            i = _skip(data, i)
        return i + 1
    j = data.index(b":", i)
    return j + 1 + int(data[i:j])


def parse_torrent(data):
    """种子字节 → 摘要 dict：name/infohash/private/size/files。

    infohash 按规范对 info 段原始字节算 SHA1；private 标记存在即视为
    私有种子（115 离线拿不到资源）。data 非法抛 BencodeError。"""
    torrent = bdecode(data)
    if not isinstance(torrent, dict) or b"info" not in torrent:
        raise BencodeError("缺少 info 段")
    info = torrent[b"info"]
    start = data.index(b"4:info") + len(b"4:info")
    raw_info = data[start:_skip(data, start)]
    info_hash = hashlib.sha1(raw_info).hexdigest()
    name = info.get(b"name", b"?").decode("utf-8", "replace")
    files = info.get(b"files")
    if files:
        size = sum(f[b"length"] for f in files if b"length" in f)
    else:
        size = info.get(b"length", 0)
    return {
        "name": name,
        "infohash": info_hash,
        "private": b"private" in raw_info,
        "size": int(size),
        "file_count": len(files) if files else 1,
    }


def infohash_from_url(url):
    """从 .torrent 直链 URL 尾段提取 infohash（如 ehtracker 的
    /get/<id>/<infohash>.torrent）；提取不到返回 None。"""
    from urllib.parse import urlsplit
    tail = os.path.basename(urlsplit(str(url or "")).path)
    stem, dot, ext = tail.rpartition(".")
    token = stem or tail
    if (dot and ext.lower() == "torrent"
            and len(token) == 40
            and all(ch in "0123456789abcdefABCDEF" for ch in token)):
        return token.lower()
    return None


def torrent_to_magnet(data):
    """种子字节 → (磁力链接, 摘要 dict)；私有种子抛 ValueError。

    摘要含 name/infohash/private/size/file_count——通知与回执直接用。"""
    info = parse_torrent(data)
    if info["private"]:
        raise ValueError(
            "私有种子（private 标记）——115 离线服务器拿不到资源")
    return magnet_from(info["name"], info["infohash"]), info


def magnet_from(name, infohash):
    """infohash + 显示名 → 磁力链接。"""
    from urllib.parse import quote
    return f"magnet:?xt=urn:btih:{infohash}&dn={quote(name or '')}"
