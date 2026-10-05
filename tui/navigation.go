package main

import (
	"strings"
)

type navigationTarget struct{ scopeID, id, path, kind string }

func (m model) navigationTargets() []navigationTarget {
	var targets []navigationTarget
	for _, group := range m.navigation {
		kind := "workspace"
		if group.scope == "project" {
			kind = "project"
		}
		targets = append(targets, navigationTarget{scopeID: group.scopeID, path: group.path, kind: kind})
		for _, chat := range group.chats {
			targets = append(targets, navigationTarget{scopeID: group.scopeID, id: chat.id, path: group.path, kind: "chat"})
		}
	}
	return targets
}

func (m model) navigationSidebar(panelWidth, height int) string {
	width := maxInt(1, panelWidth-2)
	textWidth := maxInt(1, width-2)
	lines := []string{
		sectionStyle.Render("PROJECTS & CHATS"),
		brandStyle.Render("N  + New chat"),
		brandStyle.Render("P  + New project"),
		mutedStyle.Render("Ctrl+B focus / close"),
		mutedStyle.Render("d delete selected chat / project"),
	}
	if height > 9 {
		lines = append(lines, "")
	}
	body := []string{}
	selectedRow, activeRow := -1, -1
	index := 0
	for _, group := range m.navigation {
		groupStyle := softStyle
		if group.scopeID == m.activeScopeID {
			groupStyle = brandStyle
		}
		body = append(body, groupStyle.Render(compactText(group.name, textWidth)))
		if m.navigationFocused && index == m.navigationIndex {
			selectedRow = len(body) - 1
		}
		index++
		if len(group.chats) == 0 {
			body = append(body, mutedStyle.Render("  No chats"))
		}
		for _, chat := range group.chats {
			marker := "  "
			style := mutedStyle
			if chat.id == m.activeSessionID {
				marker, style = "● ", titleStyle
				activeRow = len(body)
			}
			if m.navigationFocused && index == m.navigationIndex {
				marker, style = "› ", brandStyle
				selectedRow = len(body)
			}
			body = append(body, style.Render(marker+compactText(firstNonEmpty(chat.title, "New chat"), maxInt(1, textWidth-2))))
			index++
		}
		body = append(body, "")
	}
	visible := maxInt(1, height-len(lines))
	focusRow := activeRow
	if selectedRow >= 0 {
		focusRow = selectedRow
	}
	start := 0
	if focusRow >= visible {
		start = focusRow - visible + 1
	}
	end := minInt(len(body), start+visible)
	lines = append(lines, body[start:end]...)
	return activityStyle.Copy().Width(width).MaxWidth(width).Height(height).MaxHeight(height).Render(limitRows(strings.Join(lines, "\n"), height))
}
