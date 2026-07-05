// Command reins is the Reins command-line tool. Point it at a database and
// inspect / ask / chat over it with no hand-written code:
//
//	reins inspect ./app.db
//	reins ask ./app.db "how many active customers?"
//	reins chat ./app.db --model groq:llama-3.3-70b-versatile
//
// The model is resolved from --model or the environment (OPENAI_API_KEY,
// GEMINI_API_KEY, GROQ_API_KEY, ZAI_API_KEY, OLLAMA_BASE_URL, VLLM_BASE_URL).
package main

import (
	"fmt"
	"os"
	"strings"

	reins "github.com/shamspias/reins/packages/go"
	"github.com/shamspias/reins/packages/go/cli"
)

const usage = `reins ` + reins.Version + ` — operate your app in plain language (Go)

usage:
  reins inspect <db-url> [--write]              list the capabilities reins would expose
  reins ask     <db-url> "<question>" [--model <spec>]
  reins chat    <db-url> [--model <spec>]
  reins version

db-url:  ./app.db   :memory:   sqlite:///abs/path/app.db
--model: <spec> is "provider:model" (e.g. groq:llama-3.3-70b-versatile) or a bare
         model name; omit to auto-detect from the environment.
`

func main() {
	if err := run(os.Args[1:]); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}

func run(args []string) error {
	if len(args) == 0 {
		fmt.Print(usage)
		return nil
	}
	cmd, rest := args[0], args[1:]
	switch cmd {
	case "version", "--version", "-v":
		fmt.Println("reins " + reins.Version)
		return nil
	case "help", "-h", "--help":
		fmt.Print(usage)
		return nil
	case "inspect":
		pos, flags := parseArgs(rest)
		dbURL, err := requireDBURL(pos, "inspect")
		if err != nil {
			return err
		}
		return cli.Inspect(dbURL, flags.write, os.Stdout)
	case "ask":
		pos, flags := parseArgs(rest)
		dbURL, err := requireDBURL(pos, "ask")
		if err != nil {
			return err
		}
		if len(pos) < 2 {
			return reins.NewError("ask needs a question", `reins ask <db-url> "your question"`)
		}
		question := strings.Join(pos[1:], " ")
		model, err := resolveModel(flags.model)
		if err != nil {
			return err
		}
		return cli.Ask(dbURL, question, model, os.Stdout)
	case "chat":
		pos, flags := parseArgs(rest)
		dbURL, err := requireDBURL(pos, "chat")
		if err != nil {
			return err
		}
		model, err := resolveModel(flags.model)
		if err != nil {
			return err
		}
		return cli.Chat(dbURL, model, os.Stdin, os.Stdout)
	default:
		return reins.NewError("unknown command: "+cmd, "run `reins help` to see the available commands")
	}
}

type flagSet struct {
	write bool
	model string
}

// parseArgs splits a command's arguments into positionals and the flags reins
// understands (--write, --model <spec>). Unknown --flags are ignored so the tool
// stays forgiving.
func parseArgs(args []string) (positional []string, flags flagSet) {
	for i := 0; i < len(args); i++ {
		switch args[i] {
		case "--write", "-w":
			flags.write = true
		case "--model", "-m":
			if i+1 < len(args) {
				flags.model = args[i+1]
				i++
			}
		default:
			positional = append(positional, args[i])
		}
	}
	return positional, flags
}

func requireDBURL(positional []string, cmd string) (string, error) {
	if len(positional) == 0 {
		return "", reins.NewError(cmd+" needs a database URL", "reins "+cmd+" <db-url>  (e.g. ./app.db or :memory:)")
	}
	return positional[0], nil
}

// resolveModel picks the model from an explicit --model spec, else the environment.
func resolveModel(spec string) (reins.Model, error) {
	if spec != "" {
		return reins.ResolveModel(spec)
	}
	return reins.DefaultModel()
}
