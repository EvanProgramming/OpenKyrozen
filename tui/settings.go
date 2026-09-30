package main

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
)

type uiSettings struct {
	ShowToolDetails    bool   `json:"show_tool_details"`
	LastSeenVersion    string `json:"last_seen_version"`
	OnboardingComplete bool   `json:"onboarding_complete"`
}

func uiSettingsPath() string {
	home, err := os.UserHomeDir()
	if err != nil {
		return ""
	}
	return filepath.Join(home, ".kyrozen", "ui_settings.json")
}

func loadUISettings() uiSettings {
	path := uiSettingsPath()
	if path == "" {
		return uiSettings{}
	}
	data, err := os.ReadFile(path)
	if err != nil {
		return uiSettings{}
	}
	var settings uiSettings
	if json.Unmarshal(data, &settings) != nil {
		return uiSettings{}
	}
	return settings
}

func hasPriorInstallation() bool {
	home, err := os.UserHomeDir()
	if err != nil {
		return false
	}
	for _, path := range []string{
		filepath.Join(home, ".kyrozen_config.json"),
		filepath.Join(home, ".kyrozen", "v2", "openkyrozen.sqlite3"),
		filepath.Join(home, ".kyrozen", "ui_settings.json"),
	} {
		if _, err := os.Stat(path); err == nil {
			return true
		}
	}
	return false
}

func onboardingStatus(settings uiSettings) (string, string) {
	if settings.LastSeenVersion == "" {
		if hasPriorInstallation() {
			return "update", "previous installation"
		}
		return "new", ""
	}
	if settings.LastSeenVersion != version {
		return "update", settings.LastSeenVersion
	}
	return "", ""
}

func saveUISettings(settings uiSettings) error {
	path := uiSettingsPath()
	if path == "" {
		return fmt.Errorf("could not determine the user settings path")
	}
	directory := filepath.Dir(path)
	if err := os.MkdirAll(directory, 0o700); err != nil {
		return err
	}
	data, err := json.Marshal(settings)
	if err != nil {
		return err
	}
	temporary, err := os.CreateTemp(directory, ".ui_settings-*")
	if err != nil {
		return err
	}
	temporaryName := temporary.Name()
	defer os.Remove(temporaryName)
	if err := temporary.Chmod(0o600); err != nil {
		_ = temporary.Close()
		return err
	}
	if _, err := temporary.Write(data); err != nil {
		_ = temporary.Close()
		return err
	}
	if err := temporary.Close(); err != nil {
		return err
	}
	return os.Rename(temporaryName, path)
}
