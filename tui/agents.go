package main

import (
	"charm.land/lipgloss/v2"
	"encoding/json"
	"fmt"
	"strings"
)

func (m *model) updateAgent(agent map[string]any) {
	for i, old := range m.agents {
		if stringValue(old, "run_id") == stringValue(agent, "run_id") {
			if numberValue(agent["version"]) >= numberValue(old["version"]) {
				m.agents[i] = agent
			}
			return
		}
	}
	m.agents = append(m.agents, agent)
}

func (m model) activityAgents() []map[string]any {
	agents := append([]map[string]any{}, m.agents...)
	names := map[string]bool{}
	for _, agent := range agents {
		names[stringValue(agent, "name")] = true
	}
	for _, agent := range m.agents {
		if reviewer, ok := agent["review_agent"].(map[string]any); ok && !names[stringValue(reviewer, "name")] {
			card := map[string]any{}
			for key, value := range reviewer {
				card[key] = value
			}
			card["assignment"] = map[string]any{"objective": "Verify " + stringValue(agent, "name") + "'s results"}
			agents = append(agents, card)
			names[stringValue(reviewer, "name")] = true
		}
	}
	return agents
}

func agentIcon(agent map[string]any) string {
	rows, _ := agent["icon"].([]any)
	if len(rows) != 5 {
		return brandStyle.Render("▦")
	}
	output := []string{}
	for y := 0; y < 5; y += 2 {
		upper, _ := rows[y].(string)
		lower := "00000"
		if y+1 < 5 {
			lower, _ = rows[y+1].(string)
		}
		if len(upper) != 5 || len(lower) != 5 {
			return "▦"
		}
		row := ""
		for x := 0; x < 5; x++ {
			switch {
			case upper[x] == '1' && lower[x] == '1':
				row += "█"
			case upper[x] == '1':
				row += "▀"
			case lower[x] == '1':
				row += "▄"
			default:
				row += " "
			}
		}
		output = append(output, brandStyle.Render(row))
	}
	return strings.Join(output, "\n")
}

func (m model) agentExplorer() string {
	width := maxInt(1, m.width-2)
	lines := []string{titleStyle.Render("AGENTS · independent contexts and verification")}
	if len(m.agents) == 0 {
		lines = append(lines, "No delegated work in this chat.")
	} else {
		index := minInt(m.agentSelected, len(m.agents)-1)
		agent := m.agents[index]
		lines = append(lines, fmt.Sprintf("%d/%d  %s · %s · %s", index+1, len(m.agents), stringValue(agent, "name"), stringValue(agent, "profile"), stringValue(agent, "status")), agentIcon(agent), stringValue(agent, "provider_model"))
		if reviewer, ok := agent["review_agent"].(map[string]any); ok {
			lines = append(lines, "REVIEWER · "+stringValue(reviewer, "name")+" · "+stringValue(reviewer, "status"), agentIcon(reviewer), stringValue(reviewer, "provider_model"))
		}
		if detail := stringValue(agent, "detail_text"); detail != "" {
			lines = append(lines, detail)
		} else {
			for _, key := range []string{"assignment", "progress", "report", "reviews", "result", "attempts", "usage", "messages", "receipts", "error"} {
				if value, ok := agent[key]; ok {
					encoded, _ := json.MarshalIndent(value, "", "  ")
					lines = append(lines, strings.ToUpper(key), string(encoded))
				}
			}
		}
	}
	text := strings.Join(lines, "\n")
	wrapped := strings.Split(lipgloss.NewStyle().Width(width).MaxWidth(width).Render(text), "\n")
	height := maxInt(1, m.height-2)
	start := minInt(m.agentScroll, maxInt(0, len(wrapped)-height))
	end := minInt(len(wrapped), start+height)
	body := strings.Join(wrapped[start:end], "\n")
	footer := "←→ agent · ↑↓/PgUp/PgDn scroll · Enter inspect · c cancel · Esc/q back"
	return lipgloss.NewStyle().Width(maxInt(1, m.width)).Height(maxInt(1, m.height)).MaxHeight(maxInt(1, m.height)).MaxWidth(maxInt(1, m.width)).Render(body + "\n" + compactText(footer, width))
}
