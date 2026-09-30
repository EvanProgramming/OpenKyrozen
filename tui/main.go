package main

import (
	tea "charm.land/bubbletea/v2"
	"flag"
	"fmt"
	"os"
	"strings"
)

func main() {
	const restartExitCode = 75

	arguments := os.Args[1:]
	forceOnboarding := len(arguments) > 0 && strings.EqualFold(arguments[0], "onboarding")
	if forceOnboarding {
		arguments = arguments[1:]
	}
	project := flag.String("project", "", "active project path")
	global := flag.Bool("global", false, "use the global workspace")
	showVersion := flag.Bool("version", false, "show version")
	_ = flag.CommandLine.Parse(arguments)
	if *showVersion {
		fmt.Printf("OpenKyrozen %s\n", version)
		return
	}
	m := initialModel(*project, *global || *project == "")
	if forceOnboarding {
		m.onboardingKind = "new"
		m.onboardingPreviousVersion = ""
	}
	p := tea.NewProgram(m)
	finalModel, err := p.Run()
	if err != nil {
		fmt.Fprintln(os.Stderr, "OpenKyrozen TUI:", err)
		os.Exit(1)
	}
	if m, ok := finalModel.(model); ok && m.restart {
		os.Exit(restartExitCode)
	}
}
