package main

import (
	"charm.land/lipgloss/v2"
	"strings"
)

func rule(width int) string { return ruleStyle.Render(strings.Repeat("─", maxInt(1, width))) }

func bannerRows(width int) []string {
	if width < 8 {
		return []string{centerText("OPENKYROZEN", width)}
	}
	inner := width - 2
	line := strings.Repeat("─", inner)
	return []string{
		"╭" + line + "╮",
		"│" + centerText("◈  OPENKYROZEN  ◈", inner) + "│",
		"│" + centerText("COMPUTER-NATIVE  /  SELF-LEARNING", inner) + "│",
		"│" + centerText("A LOCAL WORKBENCH FOR YOUR IDEAS", inner) + "│",
		"╰" + line + "╯",
	}
}

func centerText(value string, width int) string {
	if lipgloss.Width(value) > width {
		value = string([]rune(value)[:maxInt(0, width)])
	}
	padding := maxInt(0, width-lipgloss.Width(value))
	return strings.Repeat(" ", padding/2) + value + strings.Repeat(" ", padding-padding/2)
}

func startupMilestones(m model) []string {
	phase := m.splashFrame / 3
	if m.reducedMotion {
		phase = 5
	}
	return []string{
		startupMilestone("workspace", phase >= 2, phase == 1),
		startupMilestone("provider", m.backendReady, !m.backendReady && phase >= 2),
		startupMilestone("memory", m.backendReady && phase >= 5, m.backendReady && phase < 5),
	}
}

func startupMilestone(name string, done, active bool) string {
	icon, style := "·", mutedStyle
	if done {
		icon, style = "✓", greenStyle
	} else if active {
		icon, style = taskSpinner(1), amberStyle
	}
	return style.Render(icon) + " " + softStyle.Render(name)
}

func splashStatus(m model) string {
	if m.backendReady {
		return greenStyle.Render("READY") + "  " + softStyle.Render("Opening your workbench…")
	}
	return amberStyle.Render(taskSpinner(m.splashFrame)) + "  " + softStyle.Render(firstNonEmpty(m.status, "Starting the workspace…"))
}

func taskSpinner(frame int) string {
	return []string{"◐", "◓", "◑", "◒"}[maxInt(0, frame)%4]
}

func taskState(status string, frame int) (string, lipgloss.Style, string) {
	switch strings.ToLower(status) {
	case "succeeded", "completed", "complete", "done":
		return "✓", greenStyle, "completed"
	case "running", "active", "in_progress":
		return taskSpinner(frame), amberStyle, "running"
	case "failed":
		return "×", redStyle, "failed"
	case "blocked":
		return "⊘", redStyle, "blocked"
	default:
		return "○", mutedStyle, firstNonEmpty(status, "pending")
	}
}

func receiptStyle(status string) lipgloss.Style {
	switch strings.ToLower(status) {
	case "blocked", "approval_denied":
		return amberStyle
	case "failure", "failed", "error":
		return redStyle
	default:
		return greenStyle
	}
}

func compactText(value string, width int) string {
	value = strings.TrimSpace(value)
	if width < 1 || lipgloss.Width(value) <= width {
		return value
	}
	runes := []rune(value)
	if len(runes) <= width {
		return value
	}
	return string(runes[:maxInt(1, width-1)]) + "…"
}
