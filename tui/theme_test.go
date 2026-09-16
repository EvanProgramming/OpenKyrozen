package main

import (
	"strconv"
	"strings"
	"testing"

	"charm.land/lipgloss/v2"
)

func rgb(value string) (float64, float64, float64) {
	value = strings.TrimPrefix(value, "#")
	redValue, _ := strconv.ParseUint(value[0:2], 16, 8)
	greenValue, _ := strconv.ParseUint(value[2:4], 16, 8)
	blueValue, _ := strconv.ParseUint(value[4:6], 16, 8)
	return float64(redValue) / 255, float64(greenValue) / 255, float64(blueValue) / 255
}

func luminance(value string) float64 {
	r, g, b := rgb(value)
	channels := []float64{}
	for _, channel := range []float64{r, g, b} {
		if channel <= 0.03928 {
			channels = append(channels, channel/12.92)
		} else {
			channels = append(channels, ((channel+0.055)/1.055)*((channel+0.055)/1.055))
		}
	}
	return 0.2126*channels[0] + 0.7152*channels[1] + 0.0722*channels[2]
}

func contrast(first, second string) float64 {
	one, two := luminance(first), luminance(second)
	if one < two {
		one, two = two, one
	}
	return (one + 0.05) / (two + 0.05)
}

func TestThemeContrastAgainstBlackCanvas(t *testing.T) {
	for _, token := range []string{white, offWhite, muted, green, amber, red} {
		if ratio := contrast(token, ink); ratio < 4.5 {
			t.Fatalf("%s has insufficient contrast on %s: %.2f", token, ink, ratio)
		}
	}
	if ratio := contrast(border, ink); ratio < 3 {
		t.Fatalf("border contrast is too low: %.2f", ratio)
	}
}

func TestMarkdownRolesKeepCyanForEmphasis(t *testing.T) {
	if *markdownStyles.Document.Color != white || *markdownStyles.Text.Color != white || *markdownStyles.Paragraph.Color != white {
		t.Fatal("ordinary markdown text is not white-first")
	}
	if *markdownStyles.Strong.Color != cyan || *markdownStyles.Heading.Color != cyan || *markdownStyles.Link.Color != cyan {
		t.Fatal("markdown emphasis is not assigned the accent")
	}
	if *markdownStyles.Code.Color != white || *markdownStyles.Code.BackgroundColor != surfaceHi || *markdownStyles.CodeBlock.BackgroundColor != surface {
		t.Fatal("code blocks do not use the readable slate treatment")
	}
	for _, token := range []string{ink, deep, surface, surfaceHi, border, white, offWhite, muted} {
		if token == cyan {
			t.Fatal("cyan leaked into a base theme token")
		}
	}
}

func TestFillBackgroundUsesEveryCell(t *testing.T) {
	const width, height = 37, 9
	for index, line := range strings.Split(fillBackground("hello\nworld", width, height), "\n") {
		if got := lipgloss.Width(line); got != width {
			t.Fatalf("line %d fills %d cells, want %d", index, got, width)
		}
	}
}
