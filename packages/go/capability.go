package reins

import (
	"reflect"
	"strings"
)

// Handler runs one capability call. The run's Context (identity, §2.7) is always
// passed as the first argument; args is the validated argument map. Failures
// return an error, surfaced to the model as errors-as-data by the registry.
type Handler func(ctx Context, args map[string]any) (any, error)

// BoundCapability is a Capability descriptor bound to its executable handler.
type BoundCapability struct {
	Spec    Capability
	Handler Handler
}

// Option overrides the name-based classification or sets safety metadata, mirroring
// the Python decorator's keywords.
type Option func(*capOptions)

type capOptions struct {
	reads       bool
	destructive bool
	confirm     bool
	idempotent  bool
	scope       string
}

// Reads forces READ classification (mutually exclusive with Destructive).
func Reads() Option { return func(o *capOptions) { o.reads = true } }

// Destructive forces DESTRUCTIVE classification.
func Destructive() Option { return func(o *capOptions) { o.destructive = true } }

// Confirm requires approval for this write/destructive capability (reads never gate).
func Confirm() Option { return func(o *capOptions) { o.confirm = true } }

// Idempotent marks the capability safe to retry.
func Idempotent() Option { return func(o *capOptions) { o.idempotent = true } }

// Scope tags the capability for row-level security (only the caller's rows, §2.8).
func Scope(s string) Option { return func(o *capOptions) { o.scope = s } }

// Define builds a capability whose input schema is derived by reflection from the
// struct type T (its exported fields + json tags). A field is required unless it
// is a pointer or its json tag has ",omitempty". The handler receives the run's
// Context and the validated argument map. Access is classified from name unless
// an option overrides it.
func Define[T any](name, description string, handler Handler, opts ...Option) BoundCapability {
	var o capOptions
	for _, opt := range opts {
		opt(&o)
	}
	var zero T
	spec := Capability{
		Name:        name,
		Description: description,
		InputSchema: schemaFromStruct(reflect.TypeOf(zero)),
		Access:      classifyWith(name, o.reads, o.destructive),
		Confirm:     o.confirm,
		Idempotent:  o.idempotent,
		Scope:       o.scope,
	}
	return BoundCapability{Spec: spec, Handler: handler}
}

// DefineNoArgs is Define for a capability that takes no arguments.
func DefineNoArgs(name, description string, handler Handler, opts ...Option) BoundCapability {
	return Define[struct{}](name, description, handler, opts...)
}

var pyToJSON = map[reflect.Kind]string{
	reflect.String:  "string",
	reflect.Bool:    "boolean",
	reflect.Int:     "integer",
	reflect.Int8:    "integer",
	reflect.Int16:   "integer",
	reflect.Int32:   "integer",
	reflect.Int64:   "integer",
	reflect.Uint:    "integer",
	reflect.Uint8:   "integer",
	reflect.Uint16:  "integer",
	reflect.Uint32:  "integer",
	reflect.Uint64:  "integer",
	reflect.Float32: "number",
	reflect.Float64: "number",
}

func schemaFromStruct(t reflect.Type) map[string]any {
	properties := map[string]any{}
	required := []string{}
	if t != nil && t.Kind() == reflect.Struct {
		for i := 0; i < t.NumField(); i++ {
			field := t.Field(i)
			if !field.IsExported() {
				continue
			}
			name, omitempty := jsonName(field)
			if name == "-" {
				continue
			}
			properties[name] = map[string]any{"type": jsonType(field.Type)}
			// Required unless the caller may omit it: pointer or ",omitempty".
			if !omitempty && field.Type.Kind() != reflect.Ptr {
				required = append(required, name)
			}
		}
	}
	return map[string]any{
		"type":                 "object",
		"properties":           properties,
		"required":             required,
		"additionalProperties": false,
	}
}

func jsonName(field reflect.StructField) (name string, omitempty bool) {
	tag := field.Tag.Get("json")
	if tag == "" {
		return strings.ToLower(field.Name), false
	}
	parts := strings.Split(tag, ",")
	name = parts[0]
	if name == "" {
		name = strings.ToLower(field.Name)
	}
	for _, p := range parts[1:] {
		if p == "omitempty" {
			omitempty = true
		}
	}
	return name, omitempty
}

func jsonType(t reflect.Type) string {
	for t.Kind() == reflect.Ptr {
		t = t.Elem()
	}
	switch t.Kind() {
	case reflect.Slice, reflect.Array:
		return "array"
	case reflect.Map, reflect.Struct:
		return "object"
	default:
		if s, ok := pyToJSON[t.Kind()]; ok {
			return s
		}
		return "string"
	}
}
