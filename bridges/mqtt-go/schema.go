package main

import (
	"fmt"
	"hash/crc32"
	"sort"
	"strings"
)

// Schema mirrors components/mpubsub/proto_emitter.py: a named message with
// a tuple of typed fields. The canonical schema string and SCHEMA_ID
// computation must stay byte-identical to the Python side so a Go bridge
// and an ESPHome node compute the same id for the same schema.
type Schema struct {
	ID     string         // schema name, e.g. "climate_reading"
	Fields []SchemaField
	byTag  map[uint32]int // tag -> index in Fields
	byName map[string]int // name -> index in Fields
	sid    uint16         // cached SCHEMA_ID
}

type SchemaField struct {
	Name     string
	Type     string // bool|int32|int64|uint32|uint64|sint32|sint64|float|string|bytes
	Tag      uint32
	Repeated bool
}

// validTypes mirrors TYPE_INFO keys in proto_emitter.py.
var validTypes = map[string]struct{}{
	"bool": {}, "int32": {}, "int64": {}, "uint32": {}, "uint64": {},
	"sint32": {}, "sint64": {}, "float": {}, "string": {}, "bytes": {},
}

// NewSchema validates the schema and pre-computes lookup tables and the
// SCHEMA_ID. Returns an error for malformed schemas (mirrors validate() in
// proto_emitter.py).
func NewSchema(id string, fields []SchemaField) (*Schema, error) {
	if id == "" {
		return nil, fmt.Errorf("schema id must be non-empty")
	}
	if len(fields) == 0 {
		return nil, fmt.Errorf("schema %q must have at least one field", id)
	}
	s := &Schema{
		ID:     id,
		Fields: fields,
		byTag:  make(map[uint32]int, len(fields)),
		byName: make(map[string]int, len(fields)),
	}
	for i, f := range fields {
		if f.Tag < 1 || f.Tag > 536870911 {
			return nil, fmt.Errorf("schema %q field %q: tag %d out of range (1..536870911)", id, f.Name, f.Tag)
		}
		if f.Tag >= 19000 && f.Tag <= 19999 {
			return nil, fmt.Errorf("schema %q field %q: tag %d is in proto3's reserved 19000-19999 range", id, f.Name, f.Tag)
		}
		if _, dup := s.byTag[f.Tag]; dup {
			return nil, fmt.Errorf("schema %q: duplicate tag %d (field %q)", id, f.Tag, f.Name)
		}
		if _, dup := s.byName[f.Name]; dup {
			return nil, fmt.Errorf("schema %q: duplicate field name %q", id, f.Name)
		}
		if _, ok := validTypes[f.Type]; !ok {
			return nil, fmt.Errorf("schema %q field %q: unknown type %q", id, f.Name, f.Type)
		}
		s.byTag[f.Tag] = i
		s.byName[f.Name] = i
	}
	s.sid = computeSchemaID(s)
	return s, nil
}

// canonicalSchemaString matches proto_emitter.canonical_schema_string:
// lines "<tag>:[repeated ]<type>:<name>" sorted lexicographically, joined
// with '\n', no trailing newline.
func canonicalSchemaString(s *Schema) string {
	lines := make([]string, len(s.Fields))
	for i, f := range s.Fields {
		typ := f.Type
		if f.Repeated {
			typ = "repeated " + f.Type
		}
		lines[i] = fmt.Sprintf("%d:%s:%s", f.Tag, typ, f.Name)
	}
	sort.Strings(lines)
	return strings.Join(lines, "\n")
}

// computeSchemaID is the low 16 bits of CRC-32/IEEE of the canonical
// schema string (matches proto_emitter.schema_id).
func computeSchemaID(s *Schema) uint16 {
	return uint16(crc32.ChecksumIEEE([]byte(canonicalSchemaString(s))) & 0xFFFF)
}

// SchemaID returns the cached 16-bit id used as the wire-format prefix on
// proto-encoded mpubsub bodies.
func (s *Schema) SchemaID() uint16 { return s.sid }
