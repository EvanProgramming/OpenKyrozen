package main

import (
	"strings"

	"charm.land/lipgloss/v2"
)

const (
	ink       = "#050608"
	surface   = "#11161b"
	surfaceHi = "#1a2229"
	white     = "#f4f7f9"
	muted     = "#93a0aa"
	cyan      = "#00f0ff"
	green     = "#61d095"
	amber     = "#e8ae5b"
	red       = "#f2788f"
)

var (
	brandStyle = lipgloss.NewStyle().Foreground(lipgloss.Color(cyan)).Bold(true)
	titleStyle = lipgloss.NewStyle().Foreground(lipgloss.Color(white)).Bold(true)
	mutedStyle = lipgloss.NewStyle().Foreground(lipgloss.Color(muted))
	panelStyle = lipgloss.NewStyle().Background(lipgloss.Color(surface)).Padding(1, 2)
	focusStyle = lipgloss.NewStyle().Border(lipgloss.RoundedBorder()).BorderForeground(lipgloss.Color(cyan)).Padding(0, 1)
	quietStyle = lipgloss.NewStyle().Border(lipgloss.RoundedBorder()).BorderForeground(lipgloss.Color(muted)).Padding(0, 1)
)

func clampSize(value int) int {
	if value < 1 {
		return 1
	}
	return value
}

// fillBackground gives every cell an explicit dark background. This keeps
// off-white text readable in terminals with a light or transparent default.
func fillBackground(content string, width, height int) string {
	width, height = clampSize(width), clampSize(height)
	lines := strings.Split(content, "\n")
	if len(lines) > height {
		lines = lines[:height]
	}
	for len(lines) < height {
		lines = append(lines, "")
	}
	style := lipgloss.NewStyle().Background(lipgloss.Color(ink)).Foreground(lipgloss.Color(white)).Width(width).MaxWidth(width)
	for index, line := range lines {
		lines[index] = style.Render(line)
	}
	return strings.Join(lines, "\n")
}
