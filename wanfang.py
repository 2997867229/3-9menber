#!/usr/bin/env python3
from __future__ import annotations

import base64
import json
import struct
import sys
from typing import Any, Dict, Iterable, List, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urlparse
from urllib.request import Request, urlopen


DETAIL_ENDPOINT = "https://d.wanfangdata.com.cn/Detail.DetailService/getDetailInFormation"
DEFAULT_TIMEOUT = 30.0
DEFAULT_DETAIL_URL = (
    "https://d.wanfangdata.com.cn/periodical/"
    "Ch9QZXJpb2RpY2FsQ0hJTmV3UzIwMjUwMTE2MTYzNjE0Ehl6Z2dkeHh4c3d6LWpzamt4MjAyNDA1MDE2Ggg1ZTl0c3J4bg%3D%3D"
)

RESOURCE_TYPE_MAP = {
    "periodical": "Periodical",
    "thesis": "Thesis",
    "conference": "Conference",
    "patent": "Patent",
    "standard": "Standard",
    "video": "Video",
    "nstr": "Nstr",
    "cstad": "Cstad",
    "law": "Claw",
    "newspaper": "Newspaper",
}


class WanfangError(RuntimeError):
    pass


def encode_varint(value: int) -> bytes:
    # protobuf 的整数默认用 varint 编码。
    if value < 0:
        raise ValueError("negative varint is not supported")
    chunks = bytearray()
    while value > 0x7F:
        chunks.append((value & 0x7F) | 0x80)
        value >>= 7
    chunks.append(value)
    return bytes(chunks)


def read_varint(data: bytes, offset: int) -> Tuple[int, int]:
    shift = 0
    value = 0
    while True:
        if offset >= len(data):
            raise WanfangError("unexpected EOF while reading varint")
        byte = data[offset]
        offset += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, offset
        shift += 7
        if shift > 63:
            raise WanfangError("varint is too large")


def encode_length_delimited(field_number: int, payload: bytes) -> bytes:
    # string、bytes、嵌套 message 都属于 length-delimited。
    key = encode_varint((field_number << 3) | 2)
    return key + encode_varint(len(payload)) + payload


def encode_string(field_number: int, value: str) -> bytes:
    return encode_length_delimited(field_number, value.encode("utf-8"))


def parse_message(data: bytes) -> Dict[int, List[Tuple[int, Any]]]:
    # 返回值保留原始 field number，后面的解析函数再按字段号取值。
    fields: Dict[int, List[Tuple[int, Any]]] = {}
    offset = 0
    while offset < len(data):
        key, offset = read_varint(data, offset)
        field_number = key >> 3
        wire_type = key & 0x07
        if wire_type == 0:
            value, offset = read_varint(data, offset)
        elif wire_type == 1:
            if offset + 8 > len(data):
                raise WanfangError("unexpected EOF while reading fixed64")
            value = data[offset : offset + 8]
            offset += 8
        elif wire_type == 2:
            length, offset = read_varint(data, offset)
            end = offset + length
            if end > len(data):
                raise WanfangError("unexpected EOF while reading length-delimited field")
            value = data[offset:end]
            offset = end
        elif wire_type == 5:
            if offset + 4 > len(data):
                raise WanfangError("unexpected EOF while reading fixed32")
            value = data[offset : offset + 4]
            offset += 4
        else:
            raise WanfangError(f"unsupported wire type: {wire_type}")
        fields.setdefault(field_number, []).append((wire_type, value))
    return fields


def decode_text(raw: bytes) -> str:
    return raw.decode("utf-8", errors="replace")


def get_first(fields: Dict[int, List[Tuple[int, Any]]], field_number: int) -> Any:
    values = fields.get(field_number)
    if not values:
        return None
    return values[0][1]


def get_string(fields: Dict[int, List[Tuple[int, Any]]], field_number: int, default: str = "") -> str:
    value = get_first(fields, field_number)
    if value is None:
        return default
    if isinstance(value, bytes):
        return decode_text(value)
    return str(value)


def get_strings(fields: Dict[int, List[Tuple[int, Any]]], field_number: int) -> List[str]:
    return [decode_text(value) for wire_type, value in fields.get(field_number, []) if wire_type == 2]


def get_int(fields: Dict[int, List[Tuple[int, Any]]], field_number: int, default: int = 0) -> int:
    value = get_first(fields, field_number)
    if value is None:
        return default
    if isinstance(value, int):
        return value
    raise WanfangError(f"field {field_number} is not a varint")


def get_bool(fields: Dict[int, List[Tuple[int, Any]]], field_number: int, default: bool = False) -> bool:
    return bool(get_int(fields, field_number, int(default)))


def get_messages(fields: Dict[int, List[Tuple[int, Any]]], field_number: int) -> List[Dict[int, List[Tuple[int, Any]]]]:
    messages: List[Dict[int, List[Tuple[int, Any]]]] = []
    for wire_type, value in fields.get(field_number, []):
        if wire_type != 2:
            continue
        messages.append(parse_message(value))
    return messages


def compact_dict(data: Dict[str, Any]) -> Dict[str, Any]:
    return {key: value for key, value in data.items() if value not in ("", [], {}, None)}


def parse_map_entry(entry: Dict[int, List[Tuple[int, Any]]]) -> Tuple[str, str]:
    return get_string(entry, 1), get_string(entry, 2)


def parse_route_token(token: str) -> Dict[str, str]:
    # 详情页最后一段本身就是一个 protobuf message 的 base64 文本。
    # 目前能稳定识别出的三段分别是页面路由键、资源 id、transaction。
    normalized = token + "=" * ((4 - len(token) % 4) % 4)
    raw = base64.b64decode(normalized)
    fields = parse_message(raw)
    return {
        "route_key": get_string(fields, 1),
        "resource_id": get_string(fields, 2),
        "transaction": get_string(fields, 3),
    }


def resource_type_from_url(url: str) -> str:
    parts = [part for part in urlparse(url).path.split("/") if part]
    if len(parts) < 2:
        raise WanfangError("URL path does not look like a Wanfang detail page")
    resource_type = RESOURCE_TYPE_MAP.get(parts[-2].lower())
    if not resource_type:
        raise WanfangError(f"unsupported Wanfang resource path: {parts[-2]}")
    return resource_type


def token_from_url(url: str) -> str:
    parts = [part for part in urlparse(url).path.split("/") if part]
    if len(parts) < 2:
        raise WanfangError("URL path does not contain a detail token")
    return unquote(parts[-1])


def build_detail_request(resource_type: str, resource_id: str, transaction: str) -> bytes:
    # 这里只写实际请求成功所需的字段：
    #   1 => ResourceType
    #   2 => Id
    #   5 => Transaction
    message = bytearray()
    message.extend(encode_string(1, resource_type))
    message.extend(encode_string(2, resource_id))
    if transaction:
        message.extend(encode_string(5, transaction))
    return bytes(message)


def wrap_grpc_web(message: bytes) -> bytes:
    # grpc-web unary 请求体格式：
    #   1 byte  frame type
    #   4 bytes message length, big-endian
    #   N bytes protobuf message
    return b"\x00" + struct.pack(">I", len(message)) + message


def unwrap_grpc_web_frames(payload: bytes) -> Tuple[List[bytes], List[str]]:
    # 响应可能包含多个 frame：
    #   0x00 普通 message
    #   0x80 trailers
    messages: List[bytes] = []
    trailers: List[str] = []
    offset = 0
    while offset < len(payload):
        if offset + 5 > len(payload):
            raise WanfangError("invalid grpc-web frame")
        frame_type = payload[offset]
        frame_length = struct.unpack(">I", payload[offset + 1 : offset + 5])[0]
        offset += 5
        frame_payload = payload[offset : offset + frame_length]
        offset += frame_length
        if len(frame_payload) != frame_length:
            raise WanfangError("truncated grpc-web frame")
        if frame_type == 0x00:
            messages.append(frame_payload)
        elif frame_type == 0x80:
            trailers.append(frame_payload.decode("utf-8", errors="replace"))
        else:
            raise WanfangError(f"unsupported grpc-web frame type: {frame_type}")
    return messages, trailers


def post_binary(url: str, body: bytes, referer: str, timeout: float) -> bytes:
    headers = {
        "content-type": "application/grpc-web+proto",
        "x-grpc-web": "1",
        "x-user-agent": "grpc-web-javascript/0.1",
        "origin": "https://d.wanfangdata.com.cn",
        "referer": referer,
    }
    request = Request(url=url, data=body, headers=headers, method="POST")
    with urlopen(request, timeout=timeout) as response:
        return response.read()


def parse_third_party(message: Dict[int, List[Tuple[int, Any]]]) -> Dict[str, Any]:
    return compact_dict(
        {
            "url": get_string(message, 1),
            "show_name": get_string(message, 2),
            "status": get_string(message, 3),
            "platform": get_string(message, 4),
            "id": get_string(message, 5),
        }
    )


def parse_origin_button(message: Dict[int, List[Tuple[int, Any]]]) -> Dict[str, Any]:
    return compact_dict(
        {
            "type": get_int(message, 1),
            "third_party_list": [parse_third_party(item) for item in get_messages(message, 2)],
            "info": get_string(message, 3),
            "type_name": get_string(message, 4),
        }
    )


def parse_periodical(message: Dict[int, List[Tuple[int, Any]]]) -> Dict[str, Any]:
    # 这里按前端 bundle 里的 Periodical protobuf 字段号做映射。
    # 没出现的字段不强行补默认值，最后统一用 compact_dict 清掉空值。
    return compact_dict(
        {
            "id": get_string(message, 1),
            "title_list": get_strings(message, 2),
            "creator_list": get_strings(message, 3),
            "first_creator": get_string(message, 4),
            "scholarid_author_list": get_strings(message, 58),
            "scholarid_list": get_strings(message, 5),
            "foreign_creator_list": get_strings(message, 6),
            "organization_norm_list": get_strings(message, 8),
            "organization_original_norm_list": get_strings(message, 83),
            "organization_new_list": get_strings(message, 9),
            "original_organization_list": get_strings(message, 10),
            "original_class_code_list": get_strings(message, 12),
            "machined_class_code_list": get_strings(message, 13),
            "class_code_for_search_list": get_strings(message, 14),
            "periodical_class_code_list": get_strings(message, 57),
            "keywords_list": get_strings(message, 16),
            "foreign_keywords_list": get_strings(message, 17),
            "machined_keywords_list": get_strings(message, 18),
            "abstract_list": get_strings(message, 20),
            "cited_count": get_int(message, 21),
            "periodical_id": get_string(message, 22),
            "periodical_title_for_search_list": get_strings(message, 23),
            "periodical_title_list": get_strings(message, 24),
            "source_db_list": get_strings(message, 25),
            "single_source_db_list": get_strings(message, 55),
            "is_oa": get_bool(message, 26),
            "fund_list": get_strings(message, 27),
            "publish_date": get_string(message, 28),
            "metadata_online_date": get_string(message, 29),
            "fulltext_online_date": get_string(message, 30),
            "service_mode": get_int(message, 31),
            "has_fulltext": get_bool(message, 32),
            "publish_year": get_int(message, 33),
            "issue": get_string(message, 34),
            "volume": get_string(message, 35),
            "page": get_string(message, 36),
            "page_no": get_string(message, 37),
            "column_list": get_strings(message, 38),
            "core_periodical_list": get_strings(message, 39),
            "fulltext_path": get_string(message, 40),
            "doi": get_string(message, 41),
            "author_org_list": get_strings(message, 42),
            "thirdparty_url_list": get_strings(message, 43),
            "language": get_string(message, 44),
            "issn": get_string(message, 45),
            "cn": get_string(message, 46),
            "sequence_in_issue": get_int(message, 47),
            "metadata_view_count": get_int(message, 48),
            "thirdparty_link_click_count": get_int(message, 49),
            "download_count": get_int(message, 50),
            "export_count": get_int(message, 56),
            "deliver_count": get_int(message, 70),
            "prepublish_version": get_string(message, 51),
            "prepublish_group_id": get_string(message, 52),
            "publish_status": get_string(message, 53),
            "type": get_string(message, 54),
            "project_id_list": get_strings(message, 63),
            "fund_group_name_list": get_strings(message, 64),
            "project_grant_no_list": get_strings(message, 65),
            "creator_with_org_sequence_list": get_strings(message, 71),
            "project_title_original_list": get_strings(message, 72),
            "lead_title_list": get_strings(message, 76),
            "subtitle_list": get_strings(message, 77),
            "obtain_way_list": get_strings(message, 74),
            "db_type": get_string(message, 81),
            "is_multi_source": get_bool(message, 82),
            "source_db_status_list": get_strings(message, 79),
            "resource_type": get_string(message, 157),
            "original_list": [parse_third_party(item) for item in get_messages(message, 158)],
        }
    )


def parse_resource(message: Dict[int, List[Tuple[int, Any]]]) -> Dict[str, Any]:
    # Resource 是 oneof 结构，同一条记录只会落在一种资源类型上。
    parsed = compact_dict(
        {
            "type": get_string(message, 1),
            "origin_buttons": [parse_origin_button(item) for item in get_messages(message, 2)],
            "uid": get_string(message, 3),
        }
    )
    if 103 in message:
        parsed["periodical"] = parse_periodical(get_messages(message, 103)[0])
    return parsed


def parse_detail_response(message: bytes) -> Dict[str, Any]:
    fields = parse_message(message)
    extra_data = {}
    # extradata 是 protobuf map<string, string>，落地后直接转普通 dict。
    for item in get_messages(fields, 2):
        key, value = parse_map_entry(item)
        extra_data[key] = value
    return compact_dict(
        {
            "detail_list": [parse_resource(item) for item in get_messages(fields, 1)],
            "extra_data": extra_data,
            "total": get_int(fields, 3),
        }
    )


def fetch_detail(url: str, timeout: float) -> Dict[str, Any]:
    # 先从详情页 URL 里拆出 token，再补成接口需要的请求字段。
    token = token_from_url(url)
    route = parse_route_token(token)
    resource_type = resource_type_from_url(url)
    request_message = build_detail_request(resource_type, route["resource_id"], route["transaction"])
    body = post_binary(DETAIL_ENDPOINT, wrap_grpc_web(request_message), referer=url, timeout=timeout)
    frames, trailers = unwrap_grpc_web_frames(body)
    if not frames:
        raise WanfangError("grpc-web response does not contain a protobuf message")
    detail = parse_detail_response(frames[0])
    return {
        "url": url,
        "route": route,
        "request": {
            "resource_type": resource_type,
            "resource_id": route["resource_id"],
            "transaction": route["transaction"],
        },
        "response": detail,
        "trailers": trailers,
    }


def main() -> int:
    url = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_DETAIL_URL

    try:
        result = fetch_detail(url, timeout=DEFAULT_TIMEOUT)
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        print(json.dumps({"error": f"HTTP {exc.code}", "body": body}, ensure_ascii=False, indent=2), file=sys.stderr)
        return 2
    except URLError as exc:
        print(json.dumps({"error": f"network error: {exc}"}, ensure_ascii=False, indent=2), file=sys.stderr)
        return 2
    except WanfangError as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False, indent=2), file=sys.stderr)
        return 2

    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    main()
