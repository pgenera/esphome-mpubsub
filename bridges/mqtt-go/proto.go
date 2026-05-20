package main

import (
	"encoding/binary"
	"fmt"
	"math"
)

// Minimal protobuf wire-format helpers, scoped to the field types that the
// mpubsub schema vocabulary supports (bool, int32/64, uint32/64, sint32/64,
// float, string, bytes -- plus repeated unpacked encoding of each).
//
// We deliberately do not pull in google.golang.org/protobuf: the bridge has
// no .proto files to compile, schemas are runtime data (parsed from YAML),
// and the ESPHome side speaks the same narrow subset. Hand-rolling keeps
// the dependency footprint tiny and the bytes-on-wire identical to what
// esphome::api::ProtoEncode produces.

// Proto wire types we use. Wire type 1 (64-bit fixed) is unused -- matches
// ESPHome's encoder, which does not support double/fixed64/sfixed64.
const (
	wireVarint  = 0
	wireLen     = 2
	wireFixed32 = 5
)

func wireTypeFor(typ string) (int, error) {
	switch typ {
	case "bool", "int32", "int64", "uint32", "uint64", "sint32", "sint64":
		return wireVarint, nil
	case "float":
		return wireFixed32, nil
	case "string", "bytes":
		return wireLen, nil
	default:
		return 0, fmt.Errorf("no wire type for %q", typ)
	}
}

func appendVarint(buf []byte, v uint64) []byte {
	for v >= 0x80 {
		buf = append(buf, byte(v)|0x80)
		v >>= 7
	}
	return append(buf, byte(v))
}

func appendTag(buf []byte, field uint32, wt int) []byte {
	return appendVarint(buf, uint64(field)<<3|uint64(wt))
}

func zigzag32(v int32) uint32 { return uint32((v << 1) ^ (v >> 31)) }
func zigzag64(v int64) uint64 { return uint64((v << 1) ^ (v >> 63)) }
func unzigzag32(v uint32) int32 {
	return int32((v >> 1) ^ -(v & 1))
}
func unzigzag64(v uint64) int64 {
	return int64((v >> 1) ^ -(v & 1))
}

// readVarint pulls one varint off data, returning the value and the number
// of bytes consumed. Returns ok=false on truncation or overlong encoding.
func readVarint(data []byte) (val uint64, n int, ok bool) {
	var shift uint
	for i, b := range data {
		if i >= 10 { // 10 bytes max for uint64 varint
			return 0, 0, false
		}
		val |= uint64(b&0x7F) << shift
		if b < 0x80 {
			return val, i + 1, true
		}
		shift += 7
	}
	return 0, 0, false
}

// appendValue encodes one logical field value of the given schema type
// (already tagged) onto buf. The caller is responsible for emitting the
// tag. Used for both singular and per-element repeated encoding.
func appendValue(buf []byte, typ string, v any) ([]byte, error) {
	switch typ {
	case "bool":
		b, ok := v.(bool)
		if !ok {
			return nil, typeMismatchErr(typ, v)
		}
		if b {
			return appendVarint(buf, 1), nil
		}
		return appendVarint(buf, 0), nil
	case "int32":
		n, err := toInt64(v)
		if err != nil {
			return nil, err
		}
		// Proto3 sign-extends negative int32 to 64 bits before varint encoding.
		return appendVarint(buf, uint64(n)), nil
	case "int64":
		n, err := toInt64(v)
		if err != nil {
			return nil, err
		}
		return appendVarint(buf, uint64(n)), nil
	case "uint32":
		n, err := toUint64(v)
		if err != nil {
			return nil, err
		}
		return appendVarint(buf, n), nil
	case "uint64":
		n, err := toUint64(v)
		if err != nil {
			return nil, err
		}
		return appendVarint(buf, n), nil
	case "sint32":
		n, err := toInt64(v)
		if err != nil {
			return nil, err
		}
		return appendVarint(buf, uint64(zigzag32(int32(n)))), nil
	case "sint64":
		n, err := toInt64(v)
		if err != nil {
			return nil, err
		}
		return appendVarint(buf, zigzag64(n)), nil
	case "float":
		f, err := toFloat64(v)
		if err != nil {
			return nil, err
		}
		var tmp [4]byte
		binary.LittleEndian.PutUint32(tmp[:], math.Float32bits(float32(f)))
		return append(buf, tmp[:]...), nil
	case "string":
		s, ok := v.(string)
		if !ok {
			return nil, typeMismatchErr(typ, v)
		}
		buf = appendVarint(buf, uint64(len(s)))
		return append(buf, s...), nil
	case "bytes":
		b, ok := v.([]byte)
		if !ok {
			return nil, typeMismatchErr(typ, v)
		}
		buf = appendVarint(buf, uint64(len(b)))
		return append(buf, b...), nil
	default:
		return nil, fmt.Errorf("unsupported proto type %q", typ)
	}
}

func typeMismatchErr(typ string, v any) error {
	return fmt.Errorf("value %v (%T) is not assignable to proto %s", v, v, typ)
}

// EncodeProto encodes a map[field-name]value into the proto body bytes
// according to schema. Repeated fields take a []any whose elements match
// the field's scalar type.
//
// Fields not present in the input are omitted (proto3 default-elision).
// This includes repeated fields with an empty/missing list.
func EncodeProto(s *Schema, values map[string]any) ([]byte, error) {
	var buf []byte
	for _, f := range s.Fields {
		v, present := values[f.Name]
		if !present {
			continue
		}
		wt, err := wireTypeFor(f.Type)
		if err != nil {
			return nil, err
		}
		if f.Repeated {
			arr, ok := v.([]any)
			if !ok {
				return nil, fmt.Errorf("field %q: repeated value must be a list, got %T", f.Name, v)
			}
			for _, elem := range arr {
				buf = appendTag(buf, f.Tag, wt)
				buf, err = appendValue(buf, f.Type, elem)
				if err != nil {
					return nil, fmt.Errorf("field %q: %w", f.Name, err)
				}
			}
			continue
		}
		buf = appendTag(buf, f.Tag, wt)
		buf, err = appendValue(buf, f.Type, v)
		if err != nil {
			return nil, fmt.Errorf("field %q: %w", f.Name, err)
		}
	}
	return buf, nil
}

// DecodeProto walks a proto body and returns a map[field-name]value.
// Unknown fields (no matching tag in the schema) are skipped, matching
// proto3 forward-compat semantics. Repeated fields accumulate into []any.
// Missing fields are absent from the returned map (no default-fill); the
// caller can supply defaults if needed.
func DecodeProto(s *Schema, body []byte) (map[string]any, error) {
	out := make(map[string]any)
	i := 0
	for i < len(body) {
		key, n, ok := readVarint(body[i:])
		if !ok {
			return nil, fmt.Errorf("truncated tag at offset %d", i)
		}
		i += n
		field := uint32(key >> 3)
		wt := int(key & 0x7)
		idx, known := s.byTag[field]
		if !known {
			// Skip unknown field per wire type.
			skip, err := skipField(body[i:], wt)
			if err != nil {
				return nil, fmt.Errorf("skip unknown field %d: %w", field, err)
			}
			i += skip
			continue
		}
		f := s.Fields[idx]
		val, consumed, err := decodeValue(body[i:], f.Type, wt)
		if err != nil {
			return nil, fmt.Errorf("field %q: %w", f.Name, err)
		}
		i += consumed
		if f.Repeated {
			cur, _ := out[f.Name].([]any)
			out[f.Name] = append(cur, val)
		} else {
			out[f.Name] = val
		}
	}
	return out, nil
}

func skipField(data []byte, wt int) (int, error) {
	switch wt {
	case wireVarint:
		_, n, ok := readVarint(data)
		if !ok {
			return 0, fmt.Errorf("truncated varint")
		}
		return n, nil
	case wireFixed32:
		if len(data) < 4 {
			return 0, fmt.Errorf("truncated fixed32")
		}
		return 4, nil
	case wireLen:
		l, n, ok := readVarint(data)
		if !ok {
			return 0, fmt.Errorf("truncated length")
		}
		// Compare as unsigned: a malicious varint can exceed INT_MAX, in
		// which case int(l) wraps negative and the signed bound check
		// silently passes -- then the slice index overflows and panics.
		if l > uint64(len(data)-n) {
			return 0, fmt.Errorf("length overflow")
		}
		return n + int(l), nil
	default:
		return 0, fmt.Errorf("unsupported wire type %d in unknown field skip", wt)
	}
}

func decodeValue(data []byte, typ string, wt int) (any, int, error) {
	expectedWT, err := wireTypeFor(typ)
	if err != nil {
		return nil, 0, err
	}
	if wt != expectedWT {
		return nil, 0, fmt.Errorf("wire type %d does not match declared %s (expected %d)", wt, typ, expectedWT)
	}
	switch typ {
	case "bool":
		v, n, ok := readVarint(data)
		if !ok {
			return nil, 0, fmt.Errorf("truncated bool")
		}
		return v != 0, n, nil
	case "int32":
		v, n, ok := readVarint(data)
		if !ok {
			return nil, 0, fmt.Errorf("truncated int32")
		}
		return int64(int32(v)), n, nil
	case "int64":
		v, n, ok := readVarint(data)
		if !ok {
			return nil, 0, fmt.Errorf("truncated int64")
		}
		return int64(v), n, nil
	case "uint32":
		v, n, ok := readVarint(data)
		if !ok {
			return nil, 0, fmt.Errorf("truncated uint32")
		}
		return uint64(uint32(v)), n, nil
	case "uint64":
		v, n, ok := readVarint(data)
		if !ok {
			return nil, 0, fmt.Errorf("truncated uint64")
		}
		return v, n, nil
	case "sint32":
		v, n, ok := readVarint(data)
		if !ok {
			return nil, 0, fmt.Errorf("truncated sint32")
		}
		return int64(unzigzag32(uint32(v))), n, nil
	case "sint64":
		v, n, ok := readVarint(data)
		if !ok {
			return nil, 0, fmt.Errorf("truncated sint64")
		}
		return unzigzag64(v), n, nil
	case "float":
		if len(data) < 4 {
			return nil, 0, fmt.Errorf("truncated float")
		}
		bits := binary.LittleEndian.Uint32(data[:4])
		return float64(math.Float32frombits(bits)), 4, nil
	case "string":
		l, n, ok := readVarint(data)
		if !ok {
			return nil, 0, fmt.Errorf("truncated string length")
		}
		// Unsigned compare: see skipField for the wrap-on-int-cast rationale.
		if l > uint64(len(data)-n) {
			return nil, 0, fmt.Errorf("string overruns body")
		}
		return string(data[n : n+int(l)]), n + int(l), nil
	case "bytes":
		l, n, ok := readVarint(data)
		if !ok {
			return nil, 0, fmt.Errorf("truncated bytes length")
		}
		if l > uint64(len(data)-n) {
			return nil, 0, fmt.Errorf("bytes overruns body")
		}
		// Copy so the returned slice doesn't alias the caller's buffer
		// (the bridge reuses its receive buffer across packets).
		cp := make([]byte, int(l))
		copy(cp, data[n:n+int(l)])
		return cp, n + int(l), nil
	default:
		return nil, 0, fmt.Errorf("unsupported type %q", typ)
	}
}

// Numeric coercion: JSON numbers come in as float64 from encoding/json by
// default, but a config or test might hand us int / int64 / uint64 directly.
// Accept any of those.
func toInt64(v any) (int64, error) {
	switch x := v.(type) {
	case int:
		return int64(x), nil
	case int32:
		return int64(x), nil
	case int64:
		return x, nil
	case uint:
		return int64(x), nil
	case uint32:
		return int64(x), nil
	case uint64:
		return int64(x), nil
	case float64:
		if x != math.Trunc(x) {
			return 0, fmt.Errorf("value %v has fractional part, cannot convert to integer", x)
		}
		return int64(x), nil
	case float32:
		return toInt64(float64(x))
	default:
		return 0, fmt.Errorf("value %v (%T) is not a number", v, v)
	}
}

func toUint64(v any) (uint64, error) {
	switch x := v.(type) {
	case int:
		if x < 0 {
			return 0, fmt.Errorf("negative %d cannot convert to unsigned", x)
		}
		return uint64(x), nil
	case int32:
		if x < 0 {
			return 0, fmt.Errorf("negative %d cannot convert to unsigned", x)
		}
		return uint64(x), nil
	case int64:
		if x < 0 {
			return 0, fmt.Errorf("negative %d cannot convert to unsigned", x)
		}
		return uint64(x), nil
	case uint:
		return uint64(x), nil
	case uint32:
		return uint64(x), nil
	case uint64:
		return x, nil
	case float64:
		if x != math.Trunc(x) || x < 0 {
			return 0, fmt.Errorf("value %v cannot convert to unsigned integer", x)
		}
		return uint64(x), nil
	default:
		return 0, fmt.Errorf("value %v (%T) is not a number", v, v)
	}
}

func toFloat64(v any) (float64, error) {
	switch x := v.(type) {
	case float64:
		return x, nil
	case float32:
		return float64(x), nil
	case int:
		return float64(x), nil
	case int32:
		return float64(x), nil
	case int64:
		return float64(x), nil
	case uint32:
		return float64(x), nil
	case uint64:
		return float64(x), nil
	default:
		return 0, fmt.Errorf("value %v (%T) is not a number", v, v)
	}
}
