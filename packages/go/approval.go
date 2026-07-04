package reins

import (
	"bufio"
	"fmt"
	"io"
	"os"
	"sort"
	"strings"
)

// The human gate (M6.4): pause a gated write for approval before it executes. The
// policy decides WHETHER approval is needed; the handler decides HOW the human is
// asked. Everything fails closed (invariant §2.2): the default handler denies, and
// a TerminalApprovalHandler denies on EOF / no interactive input.

const (
	valueWidth  = 48 // bound each rendered argument value in the preview
	maxPrompts  = 5  // bound the terminal re-prompt loop (§2.13)
	previewCaps = 8  // bound the listed actions
)

// ApprovalRequest is one gated batch: the exact calls about to run, and why.
type ApprovalRequest struct {
	Actions   []ProposedAction
	Reason    string
	BulkCount *int
}

// Preview is the human-facing rendering: exact calls, values bounded.
func (r ApprovalRequest) Preview() string {
	var lines []string
	if len(r.Actions) == 1 {
		lines = append(lines, fmt.Sprintf("⚠ Reins wants to run a %s: %s",
			accessWord(r.Actions[0]), renderCall(r.Actions[0])))
	} else {
		lines = append(lines, fmt.Sprintf("⚠ Reins wants to run %d gated operations:", len(r.Actions)))
		for i, a := range r.Actions {
			if i >= previewCaps {
				lines = append(lines, fmt.Sprintf("    … and %d more", len(r.Actions)-previewCaps))
				break
			}
			lines = append(lines, "    "+renderCall(a)+"  ["+strings.ToLower(accessWord(a))+"]")
		}
	}
	if r.BulkCount != nil {
		lines = append(lines, fmt.Sprintf("  This will touch up to %d items.", *r.BulkCount))
	}
	return strings.Join(lines, "\n")
}

// ApprovalHandler is the gate: return true to execute the gated actions, false to deny.
type ApprovalHandler interface {
	Approve(request ApprovalRequest) bool
}

// ApprovalFunc adapts a plain func to an ApprovalHandler.
type ApprovalFunc func(ApprovalRequest) bool

func (f ApprovalFunc) Approve(r ApprovalRequest) bool { return f(r) }

// denyAll is the fail-closed default gate — no approval, no write.
type denyAll struct{}

func (denyAll) Approve(ApprovalRequest) bool { return false }

// TerminalApprovalHandler prints the preview and asks [y]es / [n]o / [s]how on a
// terminal, failing closed on EOF or an exhausted re-prompt bound. In and Out are
// injectable for tests; zero value uses stdin/stdout.
type TerminalApprovalHandler struct {
	In  io.Reader
	Out io.Writer
}

func (h TerminalApprovalHandler) Approve(request ApprovalRequest) bool {
	in := h.In
	if in == nil {
		in = os.Stdin
	}
	out := h.Out
	if out == nil {
		out = os.Stdout
	}
	fmt.Fprintln(out, request.Preview())
	scanner := bufio.NewScanner(in)
	for i := 0; i < maxPrompts; i++ {
		fmt.Fprint(out, "Approve? [y]es / [n]o / [s]how: ")
		if !scanner.Scan() {
			fmt.Fprintln(out, "No interactive terminal — denying.\n  → run interactively, or pass an approve handler.")
			return false // EOF / closed stdin ⇒ deny (fail closed)
		}
		switch strings.ToLower(strings.TrimSpace(scanner.Text())) {
		case "y", "yes":
			return true
		case "n", "no":
			return false
		case "s", "show":
			fmt.Fprintln(out, detail(request))
		default:
			fmt.Fprintln(out, "Unrecognized — answer y, n, or s.")
		}
	}
	return false // re-prompt bound exhausted ⇒ deny
}

// bulkCount returns the largest count of a >1-item list argument on a write, or nil.
// The pre-ORM row-limit default: such a call is treated as touching that many rows.
func bulkCount(actions []ProposedAction) *int {
	best := 0
	for _, a := range actions {
		if a.Capability.Access == AccessRead {
			continue
		}
		for _, v := range a.Call.Arguments {
			if list, ok := v.([]any); ok && len(list) > 1 && len(list) > best {
				best = len(list)
			}
		}
	}
	if best == 0 {
		return nil
	}
	return &best
}

func accessWord(a ProposedAction) string {
	if a.Capability.Access == AccessDestructive {
		return "DESTRUCTIVE WRITE"
	}
	if a.Capability.Access == AccessRead {
		return "READ"
	}
	return "WRITE"
}

func renderCall(a ProposedAction) string {
	keys := make([]string, 0, len(a.Call.Arguments))
	for k := range a.Call.Arguments {
		keys = append(keys, k)
	}
	sort.Strings(keys) // deterministic rendering
	parts := make([]string, 0, len(keys))
	for _, k := range keys {
		parts = append(parts, fmt.Sprintf("%s=%s", k, bounded(a.Call.Arguments[k])))
	}
	return fmt.Sprintf("%s(%s)", a.Call.Name, strings.Join(parts, ", "))
}

func detail(r ApprovalRequest) string {
	var lines []string
	for _, a := range r.Actions {
		lines = append(lines, fmt.Sprintf("  %s %v", a.Call.Name, a.Call.Arguments))
	}
	return strings.Join(lines, "\n")
}

func bounded(v any) string {
	s := fmt.Sprintf("%v", v)
	if len(s) > valueWidth {
		return s[:valueWidth] + "…"
	}
	return s
}
