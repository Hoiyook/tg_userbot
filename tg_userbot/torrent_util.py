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
    """从 data[i] 解码一个值，返回 (值, 下一位置)。

    所有读取都带越界校验：截断的种子（网络下载半途而废）必须快速失败并
    给出明确原因，而不是静默产出垃圾数据（2026-09-29 实测：站点返回
    截断的种子被"成功"解析出坏磁力）。"""
    if i >= len(data):
        raise BencodeError("种子数据被截断（文件不完整）")
    c = data[i:i + 1]
    if c == b"i":                                   # 整数 i<num>e
        j = data.find(b"e", i)
        if j < 0:
            raise BencodeError("种子数据被截断（整数无终止符）")
        return int(data[i + 1:j]), j + 1
    if c == b"l":                                   # 列表 l<...>e
        i += 1
        out = []
        while data[i:i + 1] != b"e":
            if i >= len(data):
                raise BencodeError("种子数据被截断（列表未闭合）")
            v, i = _decode(data, i)
            out.append(v)
        return out, i + 1
    if c == b"d":                                   # 字典 d<...>e
        i += 1
        out = {}
        while data[i:i + 1] != b"e":
            if i >= len(data):
                raise BencodeError("种子数据被截断（字典未闭合）")
            k, i = _decode(data, i)
            v, i = _decode(data, i)
            out[k] = v
        return out, i + 1
    j = data.find(b":", i)                          # 字符串 <len>:<bytes>
    if j < 0:
        raise BencodeError("种子数据被截断（字符串无长度分隔符）")
    n = int(data[i:j])
    if j + 1 + n > len(data):
        raise BencodeError(
            f"种子数据被截断（字符串声明 {n}B 实际不足）")
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
    if i >= len(data):
        raise BencodeError("种子数据被截断（skip 越界）")
    c = data[i:i + 1]
    if c == b"i":
        j = data.find(b"e", i)
        if j < 0:
            raise BencodeError("种子数据被截断（整数无终止符）")
        return j + 1
    if c in b"ld":
        i += 1
        while data[i:i + 1] != b"e":
            if i >= len(data):
                raise BencodeError("种子数据被截断（容器未闭合）")
            i = _skip(data, i)
        return i + 1
    j = data.find(b":", i)
    if j < 0:
        raise BencodeError("种子数据被截断（字符串无长度分隔符）")
    n = int(data[i:j])
    end = j + 1 + n
    if end > len(data):
        raise BencodeError("种子数据被截断（字符串声明长度超出文件）")
    return end


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
