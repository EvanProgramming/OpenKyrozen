package main

import (
	"bufio"
	"encoding/json"
	"fmt"
	"os"
	"os/exec"
	"strings"
)

type backendEvent map[string]any

type backendLineMsg struct {
	event backendEvent
	err   error
}

type backendExitMsg struct{}

type bridge struct {
	command string
	python  string
	module  string
	cmd     *exec.Cmd
	stdin   *bufio.Writer
	events  chan backendLineMsg
}

func newBridge() *bridge {
	return &bridge{
		command: os.Getenv("KYROZEN_BACKEND_COMMAND"),
		python:  os.Getenv("KYROZEN_BACKEND_PYTHON"),
		module:  firstNonEmpty(os.Getenv("KYROZEN_BACKEND_MODULE"), "tui_backend"),
		events:  make(chan backendLineMsg, 128),
	}
}

func firstNonEmpty(values ...string) string {
	for _, value := range values {
		if strings.TrimSpace(value) != "" {
			return value
		}
	}
	return ""
}

func (b *bridge) start() error {
	var command *exec.Cmd
	if b.command != "" {
		command = exec.Command(b.command)
	} else {
		python := firstNonEmpty(b.python, "python3")
		command = exec.Command(python, "-m", b.module)
	}
	stdin, err := command.StdinPipe()
	if err != nil {
		return err
	}
	stdout, err := command.StdoutPipe()
	if err != nil {
		return err
	}
	command.Stderr = nil // backend stdout is the only machine-readable channel.
	if err := command.Start(); err != nil {
		return err
	}
	b.cmd, b.stdin = command, bufio.NewWriter(stdin)
	go func() {
		scanner := bufio.NewScanner(stdout)
		scanner.Buffer(make([]byte, 4096), 64*1024)
		for scanner.Scan() {
			event, err := parseBackendLine(scanner.Bytes())
			b.events <- backendLineMsg{event: event, err: err}
		}
		if err := scanner.Err(); err != nil {
			b.events <- backendLineMsg{err: err}
		}
		if err := command.Wait(); err != nil {
			b.events <- backendLineMsg{err: fmt.Errorf("backend exited: %w", err)}
		}
		close(b.events)
	}()
	return nil
}

func parseBackendLine(line []byte) (backendEvent, error) {
	if len(line) == 0 || len(line) > 64*1024 {
		return nil, fmt.Errorf("backend message is empty or too large")
	}
	var event backendEvent
	if err := json.Unmarshal(line, &event); err != nil {
		return nil, fmt.Errorf("malformed backend JSONL: %w", err)
	}
	if _, ok := event["event"].(string); !ok {
		return nil, fmt.Errorf("backend event is missing its name")
	}
	if requestID, ok := event["request_id"]; ok && requestID != nil {
		id, ok := requestID.(string)
		if !ok || len(id) > 100 {
			return nil, fmt.Errorf("backend event has an invalid request id")
		}
	}
	return event, nil
}

func (b *bridge) send(payload map[string]any) error {
	if b.stdin == nil {
		return fmt.Errorf("backend is not running")
	}
	data, err := json.Marshal(payload)
	if err != nil {
		return err
	}
	if len(data) > 64*1024 {
		return fmt.Errorf("backend message is too large")
	}
	if _, err := b.stdin.Write(append(data, '\n')); err != nil {
		return err
	}
	return b.stdin.Flush()
}

func (b *bridge) stop() {
	if b.stdin != nil {
		_ = b.send(map[string]any{"command": "shutdown"})
		_ = b.stdin.Flush()
	}
	if b.cmd != nil && b.cmd.Process != nil {
		_ = b.cmd.Process.Kill()
	}
}
