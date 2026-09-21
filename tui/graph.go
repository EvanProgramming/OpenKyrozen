package main

import (
	"fmt"
	"hash/fnv"
	"sort"
	"strings"

	"charm.land/lipgloss/v2"
)

type graphNode struct {
	id, label, source, location string
	community, degree           int
}

type graphEdge struct{ source, target, relation, confidence string }

type graphSnapshot struct {
	status, message, updatedAt, detail string
	nodes, edges, communities          int
	miniNodes                          []graphNode
	miniEdges                          []graphEdge
}

var communityColors = []string{cyan, green, amber, red, "#B99CFF", "#68A7FF"}

func numberValue(value any) int {
	switch typed := value.(type) {
	case int:
		return typed
	case float64:
		return int(typed)
	default:
		return 0
	}
}

func parseGraph(value any) graphSnapshot {
	raw, _ := value.(map[string]any)
	graph := graphSnapshot{
		status: stringValue(raw, "status"), message: stringValue(raw, "message"),
		updatedAt: stringValue(raw, "updated_at"), detail: stringValue(raw, "detail"),
		nodes: numberValue(raw["nodes"]), edges: numberValue(raw["edges"]), communities: numberValue(raw["communities"]),
	}
	mini, _ := raw["mini"].(map[string]any)
	if values, ok := mini["nodes"].([]any); ok {
		for _, value := range values {
			item, ok := value.(map[string]any)
			if !ok {
				continue
			}
			graph.miniNodes = append(graph.miniNodes, graphNode{
				id: stringValue(item, "id"), label: stringValue(item, "label"),
				source: stringValue(item, "source"), location: stringValue(item, "location"),
				community: numberValue(item["community"]), degree: numberValue(item["degree"]),
			})
		}
	}
	if values, ok := mini["edges"].([]any); ok {
		for _, value := range values {
			item, ok := value.(map[string]any)
			if !ok {
				continue
			}
			graph.miniEdges = append(graph.miniEdges, graphEdge{
				source: stringValue(item, "source"), target: stringValue(item, "target"),
				relation: stringValue(item, "relation"), confidence: stringValue(item, "confidence"),
			})
		}
	}
	return graph
}

func graphStatusStyle(status string) lipgloss.Style {
	switch strings.ToLower(status) {
	case "ready":
		return greenStyle
	case "indexing", "stale":
		return amberStyle
	case "error", "failed":
		return redStyle
	default:
		return mutedStyle
	}
}

func stableGraphPosition(id string, width, height int) (int, int) {
	h := fnv.New64a()
	_, _ = h.Write([]byte(id))
	value := h.Sum64()
	return int(value % uint64(maxInt(1, width))), int((value / 97) % uint64(maxInt(1, height)))
}

func (m model) graphMini(width, height int) string {
	width, height = maxInt(9, minInt(width, 22)), maxInt(3, minInt(height, 8))
	grid := make([][]rune, height)
	communities := make([][]int, height)
	for row := range grid {
		grid[row], communities[row] = []rune(strings.Repeat(" ", width)), make([]int, width)
		for column := range communities[row] {
			communities[row][column] = -1
		}
	}
	positions := map[string][2]int{}
	for _, node := range m.graph.miniNodes {
		x, y := stableGraphPosition(node.id, width, height)
		positions[node.id] = [2]int{x, y}
	}
	for _, edge := range m.graph.miniEdges {
		left, leftOK := positions[edge.source]
		right, rightOK := positions[edge.target]
		if !leftOK || !rightOK {
			continue
		}
		x, y := (left[0]+right[0])/2, (left[1]+right[1])/2
		if grid[y][x] == ' ' {
			grid[y][x] = '·'
		}
	}
	for index, node := range m.graph.miniNodes {
		point := positions[node.id]
		grid[point[1]][point[0]] = '●'
		communities[point[1]][point[0]] = node.community
		if index == m.graphSelected {
			grid[point[1]][point[0]] = '◆'
		}
	}
	lines := make([]string, 0, height)
	for row := range grid {
		var line strings.Builder
		for column, cell := range grid[row] {
			if communities[row][column] >= 0 {
				color := communityColors[communities[row][column]%len(communityColors)]
				line.WriteString(lipgloss.NewStyle().Foreground(lipgloss.Color(color)).Bold(cell == '◆').Render(string(cell)))
			} else {
				line.WriteString(ruleStyle.Render(string(cell)))
			}
		}
		lines = append(lines, line.String())
	}
	return strings.Join(lines, "\n")
}

func (m model) graphCompact() string {
	status := firstNonEmpty(m.graph.status, "missing")
	return m.graphMini(9, 3) + "  " + graphStatusStyle(status).Render(strings.ToUpper(status)) +
		mutedStyle.Render(fmt.Sprintf("  %d nodes · %d edges", m.graph.nodes, m.graph.edges))
}

func (m model) graphExplorer() string {
	width, height := maxInt(1, m.width-4), maxInt(1, m.height-2)
	status := firstNonEmpty(m.graph.status, "missing")
	lines := []string{
		brandStyle.Render("PROJECT GRAPH") + "  " + graphStatusStyle(status).Render(strings.ToUpper(status)),
		mutedStyle.Render(fmt.Sprintf("%d nodes · %d edges · %d communities", m.graph.nodes, m.graph.edges, m.graph.communities)),
		"",
		m.graphMini(minInt(48, maxInt(22, width/2)), minInt(14, maxInt(8, height/3))),
		"",
	}
	if m.graphSearching {
		lines = append(lines, focusStyle.Copy().Width(minInt(60, width-4)).Render(m.graphInput.View()), "")
	}
	nodes := m.visibleGraphNodes()
	for index, node := range nodes {
		marker, style := "·", softStyle
		if index == minInt(m.graphSelected, maxInt(0, len(nodes)-1)) {
			marker, style = "›", titleStyle
		}
		color := communityColors[node.community%len(communityColors)]
		row := lipgloss.NewStyle().Foreground(lipgloss.Color(color)).Render("●") + " " + style.Render(compactText(node.label, maxInt(8, width-20)))
		lines = append(lines, marker+" "+row+mutedStyle.Render(fmt.Sprintf("  degree %d", node.degree)))
	}
	if len(nodes) == 0 {
		lines = append(lines, mutedStyle.Render(firstNonEmpty(m.graph.message, "No indexed nodes yet.")))
	}
	if m.graph.detail != "" {
		lines = append(lines, "", titleStyle.Render("PATH / DETAILS"), softStyle.Render(compactText(m.graph.detail, width-2)))
	} else if len(nodes) > 0 {
		node := nodes[minInt(m.graphSelected, len(nodes)-1)]
		location := firstNonEmpty(node.source, "source unavailable")
		if node.location != "" {
			location += ":" + node.location
		}
		lines = append(lines, "", titleStyle.Render(node.label), mutedStyle.Render(location))
		shownEdges := 0
		for _, edge := range m.graph.miniEdges {
			neighbor := ""
			if edge.source == node.id {
				neighbor = edge.target
			} else if edge.target == node.id {
				neighbor = edge.source
			}
			if neighbor != "" {
				lines = append(lines, mutedStyle.Render(fmt.Sprintf("  %s [%s] → %s", firstNonEmpty(edge.relation, "related"), firstNonEmpty(edge.confidence, "unknown"), neighbor)))
				shownEdges++
				if shownEdges == 2 {
					break
				}
			}
		}
	}
	help := "↑↓/hjkl select · +/- zoom · / search · Tab communities · Enter neighbors · p path · r refresh · Esc close"
	lines = append(lines, "", mutedStyle.Render(help))
	return lipgloss.NewStyle().Width(width).MaxWidth(width).Height(height).MaxHeight(height).Border(lipgloss.NormalBorder()).BorderForeground(lipgloss.Color(cyan)).Padding(0, 1).Render(strings.Join(lines, "\n"))
}

func (m model) visibleGraphNodes() []graphNode {
	nodes := append([]graphNode(nil), m.graph.miniNodes...)
	if m.graphCommunity >= 0 {
		filtered := nodes[:0]
		for _, node := range nodes {
			if node.community == m.graphCommunity {
				filtered = append(filtered, node)
			}
		}
		nodes = filtered
	}
	sort.SliceStable(nodes, func(i, j int) bool {
		if nodes[i].degree != nodes[j].degree {
			return nodes[i].degree > nodes[j].degree
		}
		return nodes[i].label < nodes[j].label
	})
	limit := minInt(len(nodes), maxInt(4, 10+m.graphZoom*4))
	miniHeight := minInt(14, maxInt(8, maxInt(1, m.height-2)/3))
	limit = minInt(limit, maxInt(2, m.height-miniHeight-12))
	return nodes[:limit]
}
