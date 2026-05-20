package main

import (
	"encoding/base64"
	"encoding/json"
	"fmt"
)

// JSONToProto encodes a JSON object as protobuf bytes per the schema. JSON
// rules:
//   - object keys must match field names in the schema; unknown keys are
//     an error (lets typos surface instead of silently dropping data).
//   - missing fields are omitted from the wire (proto3 default-elision).
//   - JSON numbers go to the schema-declared numeric type; non-integer
//     values to integer fields error out.
//   - bytes fields accept a base64-std-encoded string (proto3 JSON convention).
//   - repeated fields accept a JSON array of the per-element type.
func JSONToProto(s *Schema, jsonBody []byte) ([]byte, error) {
	var raw map[string]any
	if err := json.Unmarshal(jsonBody, &raw); err != nil {
		return nil, fmt.Errorf("parse json: %w", err)
	}
	// Pre-validate keys before any coercion so we don't half-encode.
	for k := range raw {
		if _, ok := s.byName[k]; !ok {
			return nil, fmt.Errorf("schema %q: unknown field %q", s.ID, k)
		}
	}
	// Coerce bytes (base64) and repeated bytes specially.
	prepared := make(map[string]any, len(raw))
	for k, v := range raw {
		f := s.Fields[s.byName[k]]
		coerced, err := jsonValueToGo(f, v)
		if err != nil {
			return nil, fmt.Errorf("field %q: %w", k, err)
		}
		prepared[k] = coerced
	}
	return EncodeProto(s, prepared)
}

// jsonValueToGo applies the JSON-side quirks (base64 for bytes) and shapes
// repeated values into []any so EncodeProto sees a uniform format.
func jsonValueToGo(f SchemaField, v any) (any, error) {
	if f.Repeated {
		arr, ok := v.([]any)
		if !ok {
			return nil, fmt.Errorf("repeated field expects a JSON array, got %T", v)
		}
		out := make([]any, len(arr))
		for i, elem := range arr {
			c, err := jsonScalarToGo(f.Type, elem)
			if err != nil {
				return nil, fmt.Errorf("element %d: %w", i, err)
			}
			out[i] = c
		}
		return out, nil
	}
	return jsonScalarToGo(f.Type, v)
}

func jsonScalarToGo(typ string, v any) (any, error) {
	if typ == "bytes" {
		s, ok := v.(string)
		if !ok {
			return nil, fmt.Errorf("bytes field expects a base64 string, got %T", v)
		}
		b, err := base64.StdEncoding.DecodeString(s)
		if err != nil {
			return nil, fmt.Errorf("base64 decode: %w", err)
		}
		return b, nil
	}
	return v, nil
}

// ProtoToJSON decodes proto bytes per the schema and serializes the result
// as JSON. Bytes fields emerge as base64-std strings; numeric int64/uint64
// emerge as JSON numbers (which collapses to float64 on the JS side --
// that's the price of standard JSON, and matches proto3's JSON mapping for
// our supported types since we don't use the 64-bit varint types in
// practice on tiny devices).
func ProtoToJSON(s *Schema, body []byte) ([]byte, error) {
	values, err := DecodeProto(s, body)
	if err != nil {
		return nil, err
	}
	// Convert []byte (from bytes fields) to base64 strings, and convert
	// repeated []any whose elements are []byte the same way.
	for k, v := range values {
		f := s.Fields[s.byName[k]]
		values[k] = goValueToJSON(f, v)
	}
	return json.Marshal(values)
}

func goValueToJSON(f SchemaField, v any) any {
	if f.Repeated {
		arr, ok := v.([]any)
		if !ok {
			return v
		}
		out := make([]any, len(arr))
		for i, elem := range arr {
			out[i] = goScalarToJSON(f.Type, elem)
		}
		return out
	}
	return goScalarToJSON(f.Type, v)
}

func goScalarToJSON(typ string, v any) any {
	if typ == "bytes" {
		if b, ok := v.([]byte); ok {
			return base64.StdEncoding.EncodeToString(b)
		}
	}
	return v
}
