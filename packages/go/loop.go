package reins

import (
	"crypto/rand"
	"encoding/hex"
	"fmt"
)

func newTraceID() string {
	var b [16]byte
	if _, err := rand.Read(b[:]); err != nil { // pragma: unreachable on supported platforms
		return "trace"
	}
	return hex.EncodeToString(b[:])
}

// The harness loop (M6.3, simple mode) and the Agent — the Go mirror of the Python
// ETCSLV lifecycle: Engage → (Think → Call → Sense → Loop?) → Verify. The model is
// the only stochastic step; dispatch and stop logic are deterministic and owned
// here (invariant §2.14).
//
// Safety boundary: Ask is structurally read-only — a requested write/destructive
// capability is refused before anything executes (§2.1). Run may write, but write
// gating (policy → approval → audit) arrives in M6.4; until then a write under Run
// is not executed (fail-closed). Code-mode and progressive disclosure arrive in
// M6.5.

const systemPrompt = "You operate an application through its registered capabilities. " +
	"Use them to accomplish the user's goal, then reply with a final answer."

// RunReport is the detailed result of one run: outcome plus telemetry. Ask/Run
// return the public RunResult; this richer report is what tests (and the Go
// conformance runner) assert. Outcome is one of
// refused / completed / executed / not_executed / stopped.
type RunReport struct {
	Outcome  string
	Output   Message
	Finish   FinishReason
	Usage    Usage
	Turn     int
	Executed []string
	Reason   string
	TraceID  string
}

// Run drives the loop to a stop condition and returns a RunReport. A control-plane
// failure (the model erroring) is returned as the error; everything the model does
// wrong is data.
func Run(model Model, reg *CapabilityRegistry, goal string, canWrite bool, budget Budget) (RunReport, error) {
	traceID := newTraceID()
	messages := []Message{
		{Role: RoleSystem, Text: systemPrompt},
		{Role: RoleUser, Text: goal},
	}
	stops := []StopCondition{GoalReached{}, MaxTurns{Limit: budget.MaxTurns}}
	var executed []string
	var usage Usage
	var last *ModelResponse
	turn := 0
	params := DefaultModelParams()

	for {
		if budget.OverTokens(usage) { // E/L: pre-think budget guard
			return report("stopped",
				synthetic("Stopped: token budget exhausted.\n  → raise MaxTokens or simplify the goal."),
				FinishInterrupted, usage, turn, executed, "budget_exhausted", traceID), nil
		}

		resp, err := model.Complete(messages, reg.ToolSpecs(), params) // T: think
		if err != nil {
			return RunReport{}, err
		}
		usage = addUsage(usage, resp.Usage)
		messages = append(messages, resp.Message)
		last = &resp

		if resp.FinishReason == FinishToolCalls && len(resp.Message.ToolCalls) > 0 { // C: call
			if blocked := act(resp.Message.ToolCalls, reg, canWrite, &executed, &messages, usage, turn, traceID); blocked != nil {
				return *blocked, nil
			}
		}

		turn++ // S: sense
		state := RunState{Messages: messages, Turn: turn, CumulativeUsage: usage, LastResponse: last}
		if reason, stop := firstStop(stops, state); stop { // L: loop?
			if reason == "goal_reached" { // V: verify + return
				outcome := "completed"
				if len(executed) > 0 && wroteAny(reg, executed) {
					outcome = "executed"
				}
				return report(outcome, resp.Message, FinishStop, usage, turn, executed, "", traceID), nil
			}
			return report("stopped",
				synthetic("Stopped: turn budget exhausted.\n  → raise the budget or simplify the goal."),
				FinishInterrupted, usage, turn, executed, "budget_exhausted", traceID), nil
		}
	}
}

func act(toolCalls []ToolCall, reg *CapabilityRegistry, canWrite bool, executed *[]string, messages *[]Message, usage Usage, turn int, traceID string) *RunReport {
	type resolved struct {
		call ToolCall
		cap  BoundCapability
		ok   bool
	}
	items := make([]resolved, len(toolCalls))
	for i, call := range toolCalls {
		cap, ok := reg.Get(call.Name)
		items[i] = resolved{call, cap, ok}
	}

	if !canWrite {
		// Ask: structurally read-only — refuse the whole turn if any call writes,
		// before anything executes (invariant §2.1).
		for _, it := range items {
			if it.ok && it.cap.Spec.Access != AccessRead {
				r := report("refused",
					synthetic(fmt.Sprintf("Refused: %q would write, but this is a read-only Ask().\n  → use Run(...) if writing is intended.", it.call.Name)),
					FinishStop, usage, turn, *executed, "write_in_read_only", traceID)
				return &r
			}
		}
	} else {
		// Run: writes are gated. The policy engine + approval land in M6.4; until
		// then the default never auto-executes a write (fail-closed).
		for _, it := range items {
			if it.ok && it.cap.Spec.Access != AccessRead {
				r := report("not_executed",
					synthetic("Not executed: this write needs approval.\n  → write gating arrives with the policy engine and human gate."),
					FinishStop, usage, turn, *executed, "approval_required", traceID)
				return &r
			}
		}
	}

	ctx := Context{TraceID: traceID}
	for _, it := range items {
		if !it.ok {
			*messages = append(*messages, toolMessage(it.call.ID,
				CapabilityResult{OK: false, Error: fmt.Sprintf("no capability named %q", it.call.Name)}))
			continue
		}
		result := reg.Call(it.call.Name, it.call.Arguments, ctx)
		*executed = append(*executed, it.call.Name)
		*messages = append(*messages, toolMessage(it.call.ID, result))
	}
	return nil
}

func wroteAny(reg *CapabilityRegistry, executed []string) bool {
	for _, name := range executed {
		if cap, ok := reg.Get(name); ok && cap.Spec.Access != AccessRead {
			return true
		}
	}
	return false
}

func report(outcome string, output Message, finish FinishReason, usage Usage, turn int, executed []string, reason, traceID string) RunReport {
	cp := make([]string, len(executed))
	copy(cp, executed)
	return RunReport{
		Outcome: outcome, Output: output, Finish: finish, Usage: usage,
		Turn: turn, Executed: cp, Reason: reason, TraceID: traceID,
	}
}

func toolMessage(callID string, result CapabilityResult) Message {
	text := result.Error
	if result.OK {
		text = fmt.Sprintf("%v", result.Value)
	}
	return Message{Role: RoleTool, ToolCallID: callID, Text: text}
}

func synthetic(text string) Message { return Message{Role: RoleAssistant, Text: text} }

func addUsage(a, b Usage) Usage {
	return Usage{
		InputTokens:  a.InputTokens + b.InputTokens,
		OutputTokens: a.OutputTokens + b.OutputTokens,
		Cost:         a.Cost + b.Cost,
	}
}

// Agent is the user-facing harness: wrap your capabilities, hand it a goal. Ask is
// read-only and never prompts; Run may write (gated from M6.4).
type Agent struct {
	model  Model
	reg    *CapabilityRegistry
	budget Budget
}

// NewAgent builds an agent over the given capabilities. A duplicate capability
// name is a control-plane error.
func NewAgent(model Model, caps ...BoundCapability) (*Agent, error) {
	reg := NewRegistry()
	for _, c := range caps {
		if err := reg.Register(c); err != nil {
			return nil, err
		}
	}
	return &Agent{model: model, reg: reg, budget: DefaultBudget()}, nil
}

// Ask accomplishes goal using only read capabilities (structurally read-only).
func (a *Agent) Ask(goal string) (RunResult, error) {
	return a.run(goal, false)
}

// Run may write; writes are gated by the autonomy ladder (from M6.4).
func (a *Agent) Run(goal string) (RunResult, error) {
	return a.run(goal, true)
}

func (a *Agent) run(goal string, canWrite bool) (RunResult, error) {
	rep, err := Run(a.model, a.reg, goal, canWrite, a.budget)
	if err != nil {
		return RunResult{}, err
	}
	return RunResult{Output: rep.Output, Reason: rep.Finish, Usage: rep.Usage, TraceID: rep.TraceID}, nil
}
