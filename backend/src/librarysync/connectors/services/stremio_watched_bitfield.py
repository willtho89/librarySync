from __future__ import annotations

import base64
import binascii
import math
import zlib
from dataclasses import dataclass
from typing import Iterable


class WatchedBitFieldError(ValueError):
    def __init__(self, code: str, message: str, cause: Exception | None = None) -> None:
        super().__init__(f"[{code}] {message}")
        self.code = code
        self.cause = cause


def _index_of(items: Iterable[str], value: str) -> int:
    for idx, item in enumerate(items):
        if item == value:
            return idx
    return -1


@dataclass
class BitField8:
    Length: int
    values: bytearray

    def get(self, i: int) -> bool:
        index = i // 8
        bit = i % 8
        if index >= len(self.values):
            return False
        return (self.values[index] >> bit) & 1 != 0

    def set(self, i: int, value: bool) -> None:
        index = i // 8
        mask = 1 << (i % 8)
        if index >= len(self.values):
            self.values.extend(b"\x00" * (index - len(self.values) + 1))
            self.Length = len(self.values) * 8
        if value:
            self.values[index] |= mask
        else:
            self.values[index] &= ~mask

    def first_index_of(self, value: bool) -> int:
        for i in range(self.Length):
            if self.get(i) == value:
                return i
        return -1

    def last_index_of(self, value: bool) -> int:
        for i in range(self.Length - 1, -1, -1):
            if self.get(i) == value:
                return i
        return -1

    def marshal_text(self) -> bytes:
        try:
            packed = zlib.compress(bytes(self.values), 6)
        except zlib.error as exc:
            raise WatchedBitFieldError("unexpected", "failed to compress bitfield", exc) from exc
        return base64.b64encode(packed)

    def unmarshal_text(self, text: bytes) -> None:
        try:
            packed = base64.b64decode(text)
        except (ValueError, binascii.Error) as exc:
            raise WatchedBitFieldError("invalid_format", "failed to decode bitfield", exc) from exc
        try:
            decoded = zlib.decompress(packed)
        except zlib.error as exc:
            raise WatchedBitFieldError("invalid_format", "failed to decompress data", exc) from exc
        updated = new_bitfield8_with_values(decoded, self.Length)
        self.Length = updated.Length
        self.values = updated.values

    def to_string(self) -> str:
        return self.marshal_text().decode("utf-8")


def new_bitfield8(length: int) -> BitField8:
    byte_length = int(math.ceil(length / 8)) if length > 0 else 0
    return BitField8(Length=length, values=bytearray(byte_length))


def new_bitfield8_from_string(encoded: str, length: int) -> BitField8:
    bitfield = BitField8(Length=length, values=bytearray())
    bitfield.unmarshal_text(encoded.encode("utf-8"))
    return bitfield


def new_bitfield8_with_values(values: bytes | bytearray, length: int) -> BitField8:
    if length == 0:
        length = len(values) * 8
    byte_length = int(math.ceil(length / 8)) if length > 0 else 0
    updated = bytearray(values)
    if byte_length > len(updated):
        updated.extend(b"\x00" * (byte_length - len(updated)))
    return BitField8(Length=length, values=updated)


@dataclass
class WatchedField:
    AnchorVideo: str
    AnchorLength: int
    BitField: BitField8

    def marshal_text(self) -> bytes:
        bitfield_str = self.BitField.to_string()
        return f"{self.AnchorVideo}:{self.AnchorLength}:{bitfield_str}".encode("utf-8")

    def unmarshal_text(self, text: bytes) -> None:
        components = text.decode("utf-8").split(":")
        if len(components) < 3:
            raise WatchedBitFieldError("invalid_format", "Not enough components")
        bitfield_buf = components[-1]
        try:
            anchor_length = int(components[-2])
        except ValueError as exc:
            raise WatchedBitFieldError(
                "invalid_format", "Cannot obtain the length field", exc
            ) from exc
        anchor_video_id = ":".join(components[:-2])
        bitfield = BitField8(Length=0, values=bytearray())
        bitfield.unmarshal_text(bitfield_buf.encode("utf-8"))
        self.AnchorVideo = anchor_video_id
        self.AnchorLength = anchor_length
        self.BitField = bitfield

    def to_string(self) -> str:
        return self.marshal_text().decode("utf-8")

    def to_watched_bitfield(self, video_ids: list[str]) -> WatchedBitField:
        anchor_video_idx = _index_of(video_ids, self.AnchorVideo)
        if anchor_video_idx != -1:
            offset = self.AnchorLength - anchor_video_idx - 1
            bitfield = new_bitfield8_with_values(self.BitField.values, len(video_ids))
            if offset != 0:
                resized_wbf = WatchedBitField(new_bitfield8(len(video_ids)), video_ids)
                for i in range(len(video_ids)):
                    id_in_prev = i + offset
                    if 0 <= id_in_prev < bitfield.Length:
                        resized_wbf.set(i, bitfield.get(id_in_prev))
                return resized_wbf
            return WatchedBitField(bitfield, video_ids)
        return WatchedBitField(new_bitfield8(len(video_ids)), video_ids)


@dataclass
class WatchedBitField:
    bitfield: BitField8
    video_ids: list[str]

    def marshal_text(self) -> bytes:
        field = new_watched_field_from_watched_bitfield(self)
        return field.marshal_text()

    def unmarshal_text(self, text: bytes) -> None:
        field = WatchedField(AnchorVideo="", AnchorLength=0, BitField=new_bitfield8(0))
        field.unmarshal_text(text)
        converted = field.to_watched_bitfield(self.video_ids)
        self.bitfield = converted.bitfield

    def to_string(self) -> str:
        return self.marshal_text().decode("utf-8")

    def get(self, idx: int) -> bool:
        return self.bitfield.get(idx)

    def get_video(self, video_id: str) -> bool:
        pos = _index_of(self.video_ids, video_id)
        if pos == -1:
            return False
        return self.bitfield.get(pos)

    def set(self, idx: int, value: bool) -> None:
        self.bitfield.set(idx, value)

    def set_video(self, video_id: str, value: bool) -> None:
        pos = _index_of(self.video_ids, video_id)
        if pos == -1:
            return
        self.bitfield.set(pos, value)

    def get_first_unwatched_video_id(self) -> str:
        if not self.video_ids:
            return ""
        idx = self.bitfield.first_index_of(False)
        if idx == -1 or idx >= len(self.video_ids):
            idx = len(self.video_ids) - 1
        return self.video_ids[idx]

    def get_next_unwatched_video_id(self) -> str:
        idx = self.bitfield.last_index_of(True) + 1
        if idx <= 0 or idx >= len(self.video_ids):
            return ""
        return self.video_ids[idx]


def new_watched_field_from_watched_bitfield(bitfield: WatchedBitField) -> WatchedField:
    last_id = max(bitfield.bitfield.last_index_of(True), 0)
    last_video_id = "undefined"
    if last_id < len(bitfield.video_ids):
        last_video_id = bitfield.video_ids[last_id]
    return WatchedField(
        AnchorVideo=last_video_id,
        AnchorLength=last_id + 1,
        BitField=bitfield.bitfield,
    )


def new_watched_bitfield(bitfield: BitField8, video_ids: list[str]) -> WatchedBitField:
    return WatchedBitField(bitfield=bitfield, video_ids=video_ids)


def watched_bitfield_from_string(value: str, video_ids: list[str]) -> WatchedBitField:
    bitfield = WatchedBitField(bitfield=new_bitfield8(0), video_ids=video_ids)
    bitfield.unmarshal_text(value.encode("utf-8"))
    return bitfield


def watched_bitfield_from_array(values: list[bool], video_ids: list[str]) -> WatchedBitField:
    bitfield = new_bitfield8(len(video_ids))
    for idx, val in enumerate(values):
        bitfield.set(idx, val)
    return new_watched_bitfield(bitfield, video_ids)
