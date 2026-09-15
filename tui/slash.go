package main

import (
	"strings"
	"unicode"
)

type command struct {
	name        string
	aliases     []string
	description string
}

var commands = []command{
	{name: "provider", aliases: []string{"p"}, description: "Choose or configure an LLM provider"},
	{name: "api_key", aliases: []string{"key"}, description: "Set a provider API key"},
	{name: "learn", aliases: []string{"index"}, description: "Re-index the active workspace"},
	{name: "tasks", aliases: []string{"task"}, description: "Show durable task progress"},
	{name: "agent", aliases: nil, description: "Switch auto, coder, or researcher mode"},
	{name: "self-learning", aliases: []string{"learning-settings"}, description: "Tune self-learning features"},
	{name: "learning", aliases: nil, description: "Inspect learning proposals and evidence"},
	{name: "memory", aliases: nil, description: "Explain or forget a memory claim"},
	{name: "forget", aliases: nil, description: "Review recent learnings"},
	{name: "update", aliases: nil, description: "Update the installed agent"},
	{name: "quit", aliases: []string{"exit"}, description: "Close OpenKyrozen"},
}

// commandMatches deliberately only considers the first non-space character.
// A slash in prose, a URL, a path, or a code block therefore remains ordinary
// text and never steals the user's arrow keys.
func commandMatches(value string) []command {
	trimmed := strings.TrimLeftFunc(value, unicode.IsSpace)
	if !strings.HasPrefix(trimmed, "/") {
		return nil
	}
	body := strings.TrimPrefix(trimmed, "/")
	token := body
	if fields := strings.Fields(body); len(fields) > 0 {
		token = fields[0]
	}
	token = strings.ToLower(token)
	var matches []command
	for _, item := range commands {
		if strings.HasPrefix(item.name, token) {
			matches = append(matches, item)
			continue
		}
		for _, alias := range item.aliases {
			if strings.HasPrefix(alias, token) {
				matches = append(matches, item)
				break
			}
		}
	}
	return matches
}

func commandToken(value string) (start, end int, token string, ok bool) {
	start = len(value) - len(strings.TrimLeftFunc(value, unicode.IsSpace))
	if start >= len(value) || value[start] != '/' {
		return 0, 0, "", false
	}
	rest := value[start+1:]
	end = start + 1 + len(rest)
	if space := strings.IndexFunc(rest, unicode.IsSpace); space >= 0 {
		end = start + 1 + space
	}
	return start, end, strings.ToLower(value[start+1 : end]), true
}

func replaceCommand(value string, item command) string {
	start, end, _, ok := commandToken(value)
	if !ok {
		return value
	}
	return value[:start] + "/" + item.name + value[end:]
}
