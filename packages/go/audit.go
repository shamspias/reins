package reins

import (
	"strings"
	"time"
)

// The audit log (M6.4): a structured record of every write attempt. Whenever the
// harness blocks, gates, or executes a write it emits an AuditRecord — principal,
// capability, PII-redacted arguments, decision, timestamp, trace id — to the sink.
// Auditing is part of the gate chain (§2.2): a write that cannot be recorded does
// not run (no audit, no action). Reads are not audited.

// Redacted replaces a sensitive argument value in the audit copy.
const Redacted = "[redacted]"

var sensitiveKeyParts = []string{
	"password", "passwd", "secret", "token", "api_key", "apikey",
	"authorization", "credential", "private_key", "ssn", "card_number", "cvv",
}

// AuditRecord is one write attempt: who, what, with which (redacted) args, and the
// decision (refused / denied / approved / approval_denied / executed).
type AuditRecord struct {
	Timestamp  time.Time
	Principal  string
	Capability string
	Arguments  map[string]any
	Access     Access
	Decision   string
	Reason     string
	TraceID    string
}

// AuditSink is where records go. Record persists (or forwards) one record; an
// error means the write it logs must not run.
type AuditSink interface {
	Record(record AuditRecord) error
}

// MemoryAuditSink is the default sink: an in-process, in-order list.
type MemoryAuditSink struct {
	Records []AuditRecord
}

func (m *MemoryAuditSink) Record(record AuditRecord) error {
	m.Records = append(m.Records, record)
	return nil
}

// RedactArguments returns a deep copy of args with sensitive-looking keys masked;
// the original is never mutated (the capability still receives the real values).
func RedactArguments(args map[string]any) map[string]any {
	out := make(map[string]any, len(args))
	for k, v := range args {
		out[k] = redactValue(k, v)
	}
	return out
}

func redactValue(key string, value any) any {
	if isSensitive(key) {
		return Redacted
	}
	switch v := value.(type) {
	case map[string]any:
		out := make(map[string]any, len(v))
		for k, inner := range v {
			out[k] = redactValue(k, inner)
		}
		return out
	case []any:
		out := make([]any, len(v))
		for i, inner := range v {
			out[i] = redactValue(key, inner)
		}
		return out
	default:
		return value
	}
}

func isSensitive(key string) bool {
	lowered := strings.ToLower(key)
	for _, part := range sensitiveKeyParts {
		if strings.Contains(lowered, part) {
			return true
		}
	}
	return false
}

// auditClock is the audit timestamp source (one place to swap for tests later).
var auditClock = time.Now
