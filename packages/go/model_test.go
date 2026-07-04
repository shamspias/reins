package reins

import (
	"errors"
	"strings"
	"testing"
)

func TestFakeModelReplaysInOrder(t *testing.T) {
	fake := NewFakeModel(FinalResponse("one"), FinalResponse("two"))
	p := DefaultModelParams()
	first, err := fake.Complete(nil, nil, p)
	if err != nil || first.Message.Text != "one" {
		t.Fatalf("first = %+v, err %v", first, err)
	}
	second, _ := fake.Complete(nil, nil, p)
	if second.Message.Text != "two" {
		t.Errorf("second = %+v", second)
	}
}

func TestFakeModelRecordsWhatItWasShown(t *testing.T) {
	fake := NewFakeModel(FinalResponse("hi"))
	msgs := []Message{{Role: RoleUser, Text: "hello"}}
	tools := []ToolSpec{{Name: "find_orders", Description: "Find orders."}}
	if _, err := fake.Complete(msgs, tools, DefaultModelParams()); err != nil {
		t.Fatalf("complete: %v", err)
	}
	if len(fake.Calls) != 1 {
		t.Fatalf("expected 1 recorded call, got %d", len(fake.Calls))
	}
	call := fake.Calls[0]
	if len(call.Messages) != 1 || call.Messages[0].Text != "hello" {
		t.Errorf("recorded messages wrong: %+v", call.Messages)
	}
	if len(call.Tools) != 1 || call.Tools[0].Name != "find_orders" {
		t.Errorf("recorded tools wrong: %+v", call.Tools)
	}
}

func TestFakeModelExhaustedIsHintedError(t *testing.T) {
	fake := NewFakeModel(FinalResponse("only"))
	if _, err := fake.Complete(nil, nil, DefaultModelParams()); err != nil {
		t.Fatalf("first call: %v", err)
	}
	_, err := fake.Complete(nil, nil, DefaultModelParams())
	if err == nil {
		t.Fatal("expected an error once the script is exhausted")
	}
	if !strings.Contains(err.Error(), "→") {
		t.Errorf("error must carry a → next step: %q", err.Error())
	}
	var re *Error
	if !errors.As(err, &re) {
		t.Errorf("exhaustion error should be a *reins.Error, got %T", err)
	}
}

func TestScriptedResponseShapes(t *testing.T) {
	final := FinalResponse("done")
	if final.FinishReason != FinishStop || final.Message.Text != "done" {
		t.Errorf("final response wrong: %+v", final)
	}

	call := CallResponse("c1", "refund_order", map[string]any{"order_id": 8842})
	if call.FinishReason != FinishToolCalls {
		t.Errorf("call finish reason wrong: %v", call.FinishReason)
	}
	if len(call.Message.ToolCalls) != 1 || call.Message.ToolCalls[0].Name != "refund_order" {
		t.Errorf("call response wrong: %+v", call.Message.ToolCalls)
	}

	prog := ProgramResponse("c2", "print(1)")
	tc := prog.Message.ToolCalls[0]
	if prog.FinishReason != FinishToolCalls || tc.Name != "run_program" {
		t.Errorf("program response wrong: %+v", prog)
	}
	if tc.Arguments["code"] != "print(1)" {
		t.Errorf("program code not carried: %+v", tc.Arguments)
	}
}

func TestFakeModelIsAModel(t *testing.T) {
	var m Model = NewFakeModel()
	_ = m // compile-time interface satisfaction is the assertion
}
