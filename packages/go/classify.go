package reins

import "strings"

// Read/write classification by the name's leading verb (the easy-by-design
// heuristic). Ambiguous ⇒ write (invariant §2.4): a wrong guess only ever adds a
// needless approval; it must never let a write run as a read. Kept identical to
// the Python port so classification is language-neutral.

var readVerbs = set(
	"get", "list", "find", "search", "fetch", "count", "show", "read", "view", "lookup", "describe",
)

var writeVerbs = set(
	"create", "add", "update", "set", "save", "send", "apply", "edit", "modify",
	"insert", "put", "post", "make", "assign", "schedule", "approve", "register",
)

var destructiveVerbs = set(
	"delete", "remove", "cancel", "refund", "charge", "drop",
	"deactivate", "purge", "archive", "revoke", "reset", "wipe",
)

// Classify returns a capability's access from its name's leading verb
// (ambiguous ⇒ write, §2.4).
func Classify(name string) Access {
	return classifyWith(name, false, false)
}

// classifyWith applies explicit overrides (reads / destructive) over the heuristic.
// Setting both is a developer error and panics — as the Python decorator raises.
func classifyWith(name string, reads, destructive bool) Access {
	if reads && destructive {
		panic("capability " + name + " marked both Reads and Destructive — pick one")
	}
	if destructive {
		return AccessDestructive
	}
	if reads {
		return AccessRead
	}
	verb := name
	if i := strings.IndexByte(name, '_'); i >= 0 {
		verb = name[:i]
	}
	verb = strings.ToLower(verb)
	switch {
	case destructiveVerbs[verb]:
		return AccessDestructive
	case readVerbs[verb]:
		return AccessRead
	case writeVerbs[verb]:
		return AccessWrite
	default:
		return AccessWrite // ambiguous ⇒ write (invariant §2.4)
	}
}

func set(items ...string) map[string]bool {
	m := make(map[string]bool, len(items))
	for _, s := range items {
		m[s] = true
	}
	return m
}
