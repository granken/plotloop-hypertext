"""Unit tests for macOS chat.db attributedBody typedstream binary decoder."""

from __future__ import annotations

import struct
import unittest

from instinct_mac.bridge_imessage import decode_attributed_body, encode_attributed_body


class TestAttributedBodyDecoder(unittest.TestCase):
    def test_empty_and_none(self) -> None:
        self.assertEqual(decode_attributed_body(None), "")
        self.assertEqual(decode_attributed_body(b""), "")

    def test_short_utf8_and_ufffc_stripping(self) -> None:
        raw_text = "明天早上虹桥T2冰美式少冰"
        blob = encode_attributed_body("\ufffc" + raw_text + "\x00\ufffc", class_name="NSString")
        self.assertLess(len(raw_text.encode("utf-8")), 0x80)
        self.assertEqual(decode_attributed_body(blob), raw_text)
        self.assertEqual(decode_attributed_body(memoryview(blob)), raw_text)

    def test_0x81_uint16_mutable_string(self) -> None:
        text = "长文本0x81测试：" + ("靠窗座位安静车厢_" * 15)
        utf8_len = len(text.encode("utf-8"))
        self.assertGreater(utf8_len, 128)
        self.assertLess(utf8_len, 65535)
        blob = encode_attributed_body(text, class_name="NSMutableString")
        self.assertIn(b"\x81", blob)
        self.assertEqual(decode_attributed_body(blob), text)

    def test_0x82_uint32_very_long_string(self) -> None:
        text = "超长0x82消息_" + ("百万上下文测试段落ABC123_" * 2200)
        utf8_len = len(text.encode("utf-8"))
        self.assertGreater(utf8_len, 65535)
        blob = encode_attributed_body("\ufffc" + text)
        self.assertEqual(decode_attributed_body(blob), text)

    def test_0x83_uint64_length_header(self) -> None:
        payload_str = "测试0x83八字节长度头解析"
        payload = payload_str.encode("utf-8")
        blob = (
            b"\x04\x0bstreamtyped\x84\x08NSString\x01+"
            + b"\x83"
            + struct.pack("<Q", len(payload))
            + payload
            + b"\x86\x00"
        )
        self.assertEqual(decode_attributed_body(blob), payload_str)

    def test_utf8_regex_fallback_without_standard_markers(self) -> None:
        corrupted = (
            b"\x04\x0bstreamtyped\x00\xffNSAttributedString\x00"
            + "这是一条走UTF8正则兜底恢复的重要通知".encode("utf-8")
            + b"\x00__kIMMessagePartAttributeName\x00"
        )
        self.assertEqual(
            decode_attributed_body(corrupted),
            "这是一条走UTF8正则兜底恢复的重要通知",
        )


if __name__ == "__main__":
    unittest.main()
