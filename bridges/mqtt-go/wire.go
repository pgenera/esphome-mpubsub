// Wire-format primitives for mpubsub. Mirrors
// components/mpubsub/{topic_hash,wire_format}.{h,cpp} and the
// Python reference at tests/unit/reference.py. Keep these three in lockstep.
package main

import (
	"crypto/sha256"
	"encoding/binary"
	"errors"
	"fmt"
	"hash/crc32"
	"net"
)

const (
	wireMagic0   byte = 'M'
	wireMagic1   byte = 'P'
	wireVersion  byte = 0x01
	headerLen         = 12
	maxDatagram       = 1232 // IPv6 min MTU (1280) - 40 (IPv6) - 8 (UDP).
	maxPayload        = maxDatagram - headerLen
	encodingRaw  byte = 0x00
	encodingProto byte = 0x01
)

// Scope is the low nibble of the IPv6 multicast scope field (RFC 4291 §2.7).
type Scope uint8

const (
	ScopeLinkLocal Scope = 0x2
	ScopeSiteLocal Scope = 0x5
	ScopeOrgLocal  Scope = 0x8
)

func ParseScope(s string) (Scope, error) {
	switch s {
	case "link-local", "":
		return ScopeLinkLocal, nil
	case "site-local":
		return ScopeSiteLocal, nil
	case "organization-local", "org-local":
		return ScopeOrgLocal, nil
	default:
		return 0, fmt.Errorf("unknown scope %q (want link-local/site-local/organization-local)", s)
	}
}

// TopicToGroup derives the IPv6 multicast group for a topic. Matches
// components/mpubsub/topic_hash.cpp byte-for-byte.
func TopicToGroup(topic string, scope Scope) net.IP {
	digest := sha256.Sum256([]byte(topic))
	addr := make(net.IP, 16)
	addr[0] = 0xFF
	// T-bit (0x1, transient) per RFC 4291 §2.7.
	addr[1] = (0x1 << 4) | (byte(scope) & 0x0F)
	copy(addr[2:], digest[:14])
	return addr
}

// TopicCRC32 is the IEEE CRC-32 of the UTF-8 topic, matching
// zlib.crc32 and the C++ topic_crc32 helper.
func TopicCRC32(topic string) uint32 {
	return crc32.ChecksumIEEE([]byte(topic))
}

// EncodePacket builds the 12-byte header + payload datagram.
func EncodePacket(topic string, payload []byte, encoding byte) ([]byte, error) {
	if len(payload) > maxPayload {
		return nil, fmt.Errorf("payload too large (%d > %d)", len(payload), maxPayload)
	}
	if encoding != encodingRaw && encoding != encodingProto {
		return nil, fmt.Errorf("unknown encoding 0x%02x", encoding)
	}
	buf := make([]byte, headerLen+len(payload))
	buf[0] = wireMagic0
	buf[1] = wireMagic1
	buf[2] = wireVersion
	buf[3] = encoding
	binary.LittleEndian.PutUint32(buf[4:8], TopicCRC32(topic))
	binary.LittleEndian.PutUint16(buf[8:10], uint16(len(payload)))
	// buf[10:12] reserved, already zero.
	copy(buf[headerLen:], payload)
	return buf, nil
}

// DecodedPacket is the result of a successful parse.
type DecodedPacket struct {
	TopicCRC uint32
	Encoding byte
	Payload  []byte
}

var (
	errTooShort       = errors.New("datagram too short")
	errBadMagic       = errors.New("bad magic")
	errBadVersion     = errors.New("unsupported version")
	errUnknownEncoding = errors.New("unknown encoding")
	errLengthMismatch = errors.New("length mismatch")
)

func DecodePacket(data []byte) (*DecodedPacket, error) {
	if len(data) < headerLen {
		return nil, errTooShort
	}
	if data[0] != wireMagic0 || data[1] != wireMagic1 {
		return nil, errBadMagic
	}
	if data[2] != wireVersion {
		return nil, errBadVersion
	}
	enc := data[3]
	if enc != encodingRaw && enc != encodingProto {
		return nil, errUnknownEncoding
	}
	crc := binary.LittleEndian.Uint32(data[4:8])
	payloadLen := binary.LittleEndian.Uint16(data[8:10])
	// data[10:12] reserved; receivers ignore for forward-compat.
	if int(payloadLen)+headerLen != len(data) {
		return nil, errLengthMismatch
	}
	return &DecodedPacket{
		TopicCRC: crc,
		Encoding: enc,
		Payload:  data[headerLen:],
	}, nil
}
