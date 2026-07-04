package reins

import (
	"crypto/rand"
	"encoding/hex"
	"fmt"
)

// The harness loop (M6.3 + M6.4 safety) and the Agent — the Go mirror of the
// Python ETCSLV lifecycle: Engage → (Think → Call → Sense → Loop?) → Verify. The
// model is the only stochastic step; dispatch, policy, and the gate are
// deterministic and owned here (invariant §2.14).
//
// Ask is structurally read-only — a requested write is refused before anything
// executes (§2.1). Run may write; a write travels policy → human approval → audit
// → execution (M6.4). Every write attempt is audited, PII-redacted, and a write
// that cannot be recorded does not run (no audit, no action). The caller's
// principal propagates as a Context into every call (§2.7); the gate and policy
// fail closed, and neither is described to the model (§2.5). Code-mode and
// progressive disclosure arrive in M6.5.

const systemPrompt = "You operate an application through its registered capabilities. " +
	"Use them to accomplish the user's goal, then reply with a final answer."

func newTraceID() string {
	var b [16]byte
	if _, err := rand.Read(b[:]); err != nil {
		return "trace"
	}
	return hex.EncodeToString(b[:])
}

// RunReport is the detailed result of one run: outcome plus telemetry. Outcome is
// one of refused / completed / executed / not_executed / stopped. Audit carries
// this run's records; Audited is their presence.
type RunReport struct {
	Outcome  string
	Output   Message
	Finish   FinishReason
	Usage    Usage
	Turn     int
	Executed []string
	Reason   string
	Audited  bool
	Audit    []AuditRecord
	TraceID  string
}

// RunOptions configure a run's governance. Zero values are safe defaults:
// DefaultPolicy, a fail-closed deny-all gate, bulk confirm on, an in-memory audit
// sink, and DefaultBudget.
type RunOptions struct {
	Autonomy    AutonomyLevel
	Policy      Policy
	Approve     ApprovalHandler
	ConfirmBulk *bool // nil ⇒ true
	Principal   string
	Audit       AuditSink
	Schema      *SchemaIndex
	Defer       *bool
	Budget      Budget
}

// runEnv is the immutable per-run wiring the loop's helpers share.
type runEnv struct {
	reg         *CapabilityRegistry
	policy      Policy
	gate        ApprovalHandler
	confirmBulk bool
	audit       AuditSink
	ctx         Context
	canWrite    bool
	autonomy    AutonomyLevel
	disclosure  *Disclosure
}

// tally is the mutable per-run state the loop accumulates.
type tally struct {
	executed []string
	wrote    bool
	audit    []AuditRecord
}

// Run drives the loop with default governance (reads free; writes fail closed
// unless a gate approves). A control-plane failure (the model erroring) is
// returned as the error; everything the model does wrong is data.
func Run(model Model, reg *CapabilityRegistry, goal string, canWrite bool, budget Budget) (RunReport, error) {
	return RunWith(model, reg, goal, canWrite, RunOptions{Budget: budget})
}

// RunWith drives the loop with explicit governance options.
func RunWith(model Model, reg *CapabilityRegistry, goal string, canWrite bool, opts RunOptions) (RunReport, error) {
	confirmBulk := opts.ConfirmBulk == nil || *opts.ConfirmBulk
	autonomy := opts.Autonomy
	if !canWrite {
		autonomy = AutonomyReadOnly
	} else if autonomy == "" {
		autonomy = AutonomyApprovedWrites
	}
	disclosure, err := newDisclosure(reg, opts.Schema, opts.Defer)
	if err != nil {
		return RunReport{}, err
	}
	env := &runEnv{
		reg:         reg,
		policy:      orDefault(opts.Policy),
		gate:        orDenyAll(opts.Approve),
		confirmBulk: confirmBulk,
		audit:       orMemorySink(opts.Audit),
		ctx:         Context{Principal: opts.Principal, TraceID: newTraceID()},
		canWrite:    canWrite,
		autonomy:    autonomy,
		disclosure:  disclosure,
	}
	budget := opts.Budget
	if budget.MaxTurns == 0 {
		budget.MaxTurns = DefaultBudget().MaxTurns
	}

	messages := []Message{{Role: RoleSystem, Text: systemPrompt}, {Role: RoleUser, Text: goal}}
	stops := []StopCondition{GoalReached{}, MaxTurns{Limit: budget.MaxTurns}}
	tl := &tally{}
	var usage Usage
	var last *ModelResponse
	turn := 0
	params := DefaultModelParams()

	for {
		if budget.OverTokens(usage) {
			return env.report("stopped",
				synthetic("Stopped: token budget exhausted.\n  → raise MaxTokens or simplify the goal."),
				FinishInterrupted, usage, turn, tl, "budget_exhausted"), nil
		}
		resp, err := model.Complete(messages, env.disclosure.tools(), params)
		if err != nil {
			return RunReport{}, err
		}
		usage = addUsage(usage, resp.Usage)
		messages = append(messages, resp.Message)
		last = &resp

		if resp.FinishReason == FinishToolCalls && len(resp.Message.ToolCalls) > 0 {
			if blocked := env.act(resp.Message.ToolCalls, tl, &messages, usage, turn); blocked != nil {
				return *blocked, nil
			}
		}

		turn++
		state := RunState{Messages: messages, Turn: turn, CumulativeUsage: usage, LastResponse: last}
		if reason, stop := firstStop(stops, state); stop {
			if reason == "goal_reached" {
				outcome := "completed"
				if tl.wrote {
					outcome = "executed"
				}
				return env.report(outcome, resp.Message, FinishStop, usage, turn, tl, ""), nil
			}
			return env.report("stopped",
				synthetic("Stopped: turn budget exhausted.\n  → raise the budget or simplify the goal."),
				FinishInterrupted, usage, turn, tl, "budget_exhausted"), nil
		}
	}
}

func (env *runEnv) act(toolCalls []ToolCall, tl *tally, messages *[]Message, usage Usage, turn int) *RunReport {
	// Discovery calls (search_capabilities / search_schema) are harness-internal
	// reads: answered here, free at every autonomy level, allowed under Ask (§2.12).
	var pending []ToolCall
	for _, call := range toolCalls {
		if result, handled := env.disclosure.answer(call); handled {
			*messages = append(*messages, toolMessage(call.ID, result))
			continue
		}
		pending = append(pending, call)
	}

	type resolved struct {
		call ToolCall
		cap  BoundCapability
		ok   bool
	}
	items := make([]resolved, len(pending))
	for i, call := range pending {
		cap, ok := env.reg.Get(call.Name)
		items[i] = resolved{call, cap, ok}
	}

	if !env.canWrite {
		// Ask: structurally read-only — refuse the whole turn if any call writes (§2.1).
		for _, it := range items {
			if it.ok && it.cap.Spec.Access != AccessRead {
				action := ProposedAction{Call: it.call, Capability: it.cap.Spec}
				env.auditAction(tl, action, "refused", "write_in_read_only")
				r := env.report("refused",
					synthetic(fmt.Sprintf("Refused: %q would write, but this is a read-only Ask().\n  → use Run(...) if writing is intended.", it.call.Name)),
					FinishStop, usage, turn, tl, "write_in_read_only")
				return &r
			}
		}
	} else {
		var proposed []ProposedAction
		for _, it := range items {
			if it.ok {
				proposed = append(proposed, ProposedAction{Call: it.call, Capability: it.cap.Spec})
			}
		}
		if len(proposed) > 0 {
			verdict := env.policy.CheckActions(proposed, env.autonomy)
			if verdict.Decision == DecisionDeny {
				for _, a := range proposed {
					if a.Capability.Access != AccessRead {
						env.auditAction(tl, a, "denied", verdict.Reason)
					}
				}
				outcome := "not_executed"
				if verdict.Reason == "write_in_read_only" {
					outcome = "refused"
				}
				r := env.report(outcome,
					synthetic(fmt.Sprintf("Not run (%s).\n  → adjust the goal or the agent's autonomy level.", verdict.Reason)),
					FinishStop, usage, turn, tl, verdict.Reason)
				return &r
			}
			if blocked := env.gateTurn(proposed, verdict.Decision == DecisionRequireApproval, verdict.Reason, tl, usage, turn); blocked != nil {
				return blocked
			}
		}
	}

	for _, it := range items {
		if !it.ok {
			*messages = append(*messages, toolMessage(it.call.ID,
				CapabilityResult{OK: false, Error: fmt.Sprintf("no capability named %q", it.call.Name)}))
			continue
		}
		// A scope-refused call never reaches user code (§2.8), so it must not be
		// audited as executed or count as a write.
		if violation, blocked := scopeViolation(it.cap.Spec, env.ctx); blocked {
			*messages = append(*messages, toolMessage(it.call.ID, CapabilityResult{OK: false, Error: violation}))
			continue
		}
		if it.cap.Spec.Access != AccessRead {
			action := ProposedAction{Call: it.call, Capability: it.cap.Spec}
			if !env.auditAction(tl, action, "executed", "") { // no audit, no action (§2.2)
				*messages = append(*messages, toolMessage(it.call.ID, CapabilityResult{
					OK: false, Error: "audit sink failed — the write was not executed\n  → fix the audit sink",
				}))
				continue
			}
			tl.wrote = true
		}
		result := env.reg.Call(it.call.Name, it.call.Arguments, env.ctx)
		tl.executed = append(tl.executed, it.call.Name)
		*messages = append(*messages, toolMessage(it.call.ID, result))
	}
	return nil
}

func (env *runEnv) gateTurn(proposed []ProposedAction, verdictRequires bool, verdictReason string, tl *tally, usage Usage, turn int) *RunReport {
	var gated []ProposedAction
	for _, a := range proposed {
		if a.Capability.Access != AccessRead {
			gated = append(gated, a)
		}
	}
	var count *int
	if env.confirmBulk {
		count = bulkCount(gated)
	}
	needsApproval := verdictRequires
	reason := verdictReason
	if reason == "" {
		reason = "approval_required"
	}
	if count != nil && !needsApproval {
		needsApproval, reason = true, "bulk_confirm"
	}
	if !needsApproval {
		return nil
	}
	batch := gated
	if len(batch) == 0 {
		batch = proposed // a custom policy may gate a read-only turn; show the real batch
	}
	request := ApprovalRequest{Actions: batch, Reason: reason, BulkCount: count}
	approved := env.gate.Approve(request)
	decision := "approval_denied"
	if approved {
		decision = "approved"
	}
	recorded := true
	for _, a := range request.Actions {
		if !env.auditAction(tl, a, decision, reason) {
			recorded = false
		}
	}
	if approved && recorded {
		return nil
	}
	text := "Not executed: the write was not approved.\n  → approve it, or pass an approve handler that returns true."
	report_reason := "approval_denied"
	if approved && !recorded {
		text = "Not executed: the write was approved but could not be audited.\n  → fix the audit sink; no audit, no action."
		report_reason = "audit_failed"
	}
	r := env.report("not_executed", synthetic(text), FinishStop, usage, turn, tl, report_reason)
	return &r
}

func (env *runEnv) auditAction(tl *tally, action ProposedAction, decision, reason string) bool {
	record := AuditRecord{
		Timestamp:  auditClock(),
		Principal:  env.ctx.Principal,
		Capability: action.Capability.Name,
		Arguments:  RedactArguments(action.Call.Arguments),
		Access:     action.Capability.Access,
		Decision:   decision,
		Reason:     reason,
		TraceID:    env.ctx.TraceID,
	}
	if err := env.audit.Record(record); err != nil {
		return false
	}
	tl.audit = append(tl.audit, record)
	return true
}

func (env *runEnv) report(outcome string, output Message, finish FinishReason, usage Usage, turn int, tl *tally, reason string) RunReport {
	executed := make([]string, len(tl.executed))
	copy(executed, tl.executed)
	audit := make([]AuditRecord, len(tl.audit))
	copy(audit, tl.audit)
	return RunReport{
		Outcome: outcome, Output: output, Finish: finish, Usage: usage, Turn: turn,
		Executed: executed, Reason: reason, Audited: len(audit) > 0, Audit: audit,
		TraceID: env.ctx.TraceID,
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

func orDefault(p Policy) Policy {
	if p == nil {
		return DefaultPolicy{}
	}
	return p
}

func orDenyAll(h ApprovalHandler) ApprovalHandler {
	if h == nil {
		return denyAll{}
	}
	return h
}

func orMemorySink(s AuditSink) AuditSink {
	if s == nil {
		return &MemoryAuditSink{}
	}
	return s
}

// --- the Agent -------------------------------------------------------------------

// Agent is the user-facing harness: wrap your capabilities, hand it a goal. Ask is
// read-only and never prompts; Run may write, gated by the autonomy ladder.
type Agent struct {
	model       Model
	reg         *CapabilityRegistry
	budget      Budget
	autonomy    AutonomyLevel
	approve     ApprovalHandler
	audit       AuditSink
	principal   string
	confirmBulk bool
	schema      *SchemaIndex
	defer_      *bool
	dupErr      error // carries a duplicate-registration error out of WithCapabilities
}

// AgentOption configures an Agent.
type AgentOption func(*Agent)

// WithCapabilities registers capabilities on the agent.
func WithCapabilities(caps ...BoundCapability) AgentOption {
	return func(a *Agent) {
		for _, c := range caps {
			// A duplicate is caught in NewAgent, which surfaces the error.
			if a.reg.Register(c) != nil {
				a.dupErr = NewError(fmt.Sprintf("capability %q is already registered", c.Spec.Name), "give each capability a unique name")
			}
		}
	}
}

// WithBudget sets the run budget.
func WithBudget(b Budget) AgentOption { return func(a *Agent) { a.budget = b } }

// WithAutonomy sets the autonomy level for Run (default ApprovedWrites).
func WithAutonomy(level AutonomyLevel) AgentOption { return func(a *Agent) { a.autonomy = level } }

// WithApprove sets the human gate (default: fail-closed deny-all).
func WithApprove(h ApprovalHandler) AgentOption { return func(a *Agent) { a.approve = h } }

// WithApproveFunc sets the human gate from a plain func.
func WithApproveFunc(f func(ApprovalRequest) bool) AgentOption {
	return func(a *Agent) { a.approve = ApprovalFunc(f) }
}

// WithAudit sets the audit sink (default: in-memory, see Agent.Audit()).
func WithAudit(s AuditSink) AgentOption { return func(a *Agent) { a.audit = s } }

// WithPrincipal sets who the agent acts for (identity → RLS).
func WithPrincipal(p string) AgentOption { return func(a *Agent) { a.principal = p } }

// WithConfirmBulk toggles the >1-row bulk-confirm default (on by default).
func WithConfirmBulk(on bool) AgentOption { return func(a *Agent) { a.confirmBulk = on } }

// WithSchema attaches a data-model index for progressive disclosure (search_schema).
func WithSchema(s SchemaIndex) AgentOption { return func(a *Agent) { a.schema = &s } }

// WithDefer forces capability disclosure on/off (default: auto, above deferAfter).
func WithDefer(on bool) AgentOption { return func(a *Agent) { a.defer_ = &on } }

// NewAgent builds an agent. A duplicate capability name is a control-plane error.
func NewAgent(model Model, opts ...AgentOption) (*Agent, error) {
	a := &Agent{
		model:       model,
		reg:         NewRegistry(),
		budget:      DefaultBudget(),
		autonomy:    AutonomyApprovedWrites,
		confirmBulk: true,
	}
	for _, opt := range opts {
		opt(a)
	}
	if a.dupErr != nil {
		return nil, a.dupErr
	}
	return a, nil
}

// Audit returns the agent's audit sink (default in-memory).
func (a *Agent) Audit() AuditSink { return a.audit }

// Ask accomplishes goal using only read capabilities (structurally read-only).
func (a *Agent) Ask(goal string) (RunResult, error) { return a.run(goal, false) }

// Run may write; writes are gated by the autonomy ladder.
func (a *Agent) Run(goal string) (RunResult, error) { return a.run(goal, true) }

func (a *Agent) run(goal string, canWrite bool) (RunResult, error) {
	if a.audit == nil {
		a.audit = &MemoryAuditSink{}
	}
	rep, err := RunWith(a.model, a.reg, goal, canWrite, RunOptions{
		Autonomy:    a.autonomy,
		Approve:     a.approve,
		ConfirmBulk: &a.confirmBulk,
		Principal:   a.principal,
		Audit:       a.audit,
		Schema:      a.schema,
		Defer:       a.defer_,
		Budget:      a.budget,
	})
	if err != nil {
		return RunResult{}, err
	}
	return RunResult{Output: rep.Output, Reason: rep.Finish, Usage: rep.Usage, TraceID: rep.TraceID}, nil
}
