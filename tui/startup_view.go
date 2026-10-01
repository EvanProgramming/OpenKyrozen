package main

import (
	tea "charm.land/bubbletea/v2"
	"charm.land/lipgloss/v2"
	"strings"
)

func renderOpenKyrozenBanner(frame int, animated bool) string {
	glyphs := make([][]rune, len(openKyrozenBanner))
	for index, row := range openKyrozenBanner {
		glyphs[index] = []rune(row)
	}
	width := len(glyphs[0]) + 2
	height := len(glyphs) + 1
	highlightX := -10
	if animated {
		highlightX = (frame/2)%(width+12) - 6
	}
	front, highlight, shadow := brandStyle.Render("█"), titleStyle.Render("█"), ruleStyle.Render("░")
	lines := make([]string, height)
	for y := 0; y < height; y++ {
		var line strings.Builder
		for x := 0; x < width; x++ {
			isFront := y < len(glyphs) && x < len(glyphs[y]) && glyphs[y][x] != ' '
			isShadow := y > 0 && x > 1 && x-2 < len(glyphs[y-1]) && glyphs[y-1][x-2] != ' '
			switch {
			case isFront && x >= highlightX-1 && x <= highlightX+1:
				line.WriteString(highlight)
			case isFront:
				line.WriteString(front)
			case isShadow:
				line.WriteString(shadow)
			default:
				line.WriteByte(' ')
			}
		}
		lines[y] = line.String()
	}
	return strings.Join(lines, "\n")
}

func (m model) View() tea.View {
	var content string
	if m.screen == screenSplash {
		content = m.splash()
	} else if m.screen == screenGraph {
		content = m.graphExplorer()
	} else if m.screen == screenAgents {
		content = m.agentExplorer()
	} else if m.screen == screenUpdating {
		content = m.updatingView()
	} else {
		content = m.chatView()
		if m.screen != screenChat {
			content = m.modal(content)
		}
	}
	view := tea.NewView(fillBackground(content, m.width, m.height))
	view.AltScreen = true
	view.MouseMode = tea.MouseModeCellMotion
	view.BackgroundColor = lipgloss.Color(ink)
	view.ForegroundColor = lipgloss.Color(white)
	view.WindowTitle = "OpenKyrozen"
	return view
}

func (m model) updatingView() string {
	width := maxInt(1, m.width-8)
	lines := []string{
		brandStyle.Render("OPENKYROZEN"),
		"",
		titleStyle.Render("Updating OpenKyrozen"),
		"",
		amberStyle.Render(taskSpinner(m.motionFrame) + " " + firstNonEmpty(m.status, "Updating files and preparing the restart…")),
		"",
		softStyle.Render("Your workspace is temporarily locked while the update is installed."),
		softStyle.Render("OpenKyrozen will restart automatically when it is ready."),
		"",
		mutedStyle.Render("Ctrl+C  emergency quit"),
	}
	return lipgloss.NewStyle().Width(maxInt(1, m.width)).Height(maxInt(1, m.height)).Align(lipgloss.Center, lipgloss.Center).Render(
		lipgloss.NewStyle().Width(width).MaxWidth(width).Align(lipgloss.Center).Render(strings.Join(lines, "\n")),
	)
}

func (m model) splash() string {
	width := maxInt(1, minInt(56, m.width-4))
	rows := bannerRows(width)
	visible := len(rows)
	if !m.reducedMotion {
		visible = minInt(len(rows), maxInt(0, m.splashFrame-1))
	}
	logo := make([]string, 0, len(rows))
	for index, row := range rows {
		if index >= visible {
			row = strings.Repeat(" ", lipgloss.Width(row))
		}
		style := softStyle
		if index != 3 {
			style = brandStyle
		}
		logo = append(logo, style.Copy().Width(width).Align(lipgloss.Center).Render(row))
	}
	lines := append(logo, "")
	lines = append(lines, startupMilestones(m)...)
	lines = append(lines, "", splashStatus(m))
	content := strings.Join(lines, "\n")
	return lipgloss.NewStyle().Width(maxInt(1, m.width)).Height(maxInt(1, m.height)).Align(lipgloss.Center, lipgloss.Center).Render(content)
}
