// What the shell program says when it is asked for nothing: the entry points
// and their arguments, rendered from the contract compiled into it.
//
// The spec says a program asked for nothing says what it takes, and that what
// the listing looks like is for a person and not part of the contract - so this
// file is where the looking lives, and the mutation tools leave it alone, as
// they leave a message's wording alone. That it names every entry point is the
// contract's, and scripts/conform.py checks it.
package main

import (
	"encoding/json"
	"fmt"
	"sort"
	"strings"
)

// listing is the entry points and their arguments, read from the contract that
// is compiled in. What it looks like is for a person and not part of the
// contract; where it comes from is, so that no help text can drift from it.
func listing(only string) string {
	var doc struct {
		EntryPoints map[string]json.RawMessage `json:"entry_points"`
	}
	if err := json.Unmarshal(contract, &doc); err != nil {
		return "the contract compiled into this program could not be read\n"
	}
	lines := []string{
		"go-cli <entry point> [<arguments file> | -]",
		"",
		"the arguments are one JSON object under these names (! required); the exit",
		"code is the worst verdict in the answer (0 ok, 1 warning, 2 halt), 3 for",
		"arguments outside the contract, 4 when no call was made. rules between",
		"arguments (at_most, requires) are in spec/contract.json.",
		"",
	}
	names := make([]string, 0, len(doc.EntryPoints))
	for name := range doc.EntryPoints {
		names = append(names, name)
	}
	sort.Strings(names)
	for _, name := range names {
		if only != "" && name != only {
			continue
		}
		var entry struct {
			TakesAFunction bool                       `json:"takes_a_function"`
			Call           map[string]any             `json:"call"`
			Arguments      map[string]json.RawMessage `json:"arguments"`
		}
		if err := json.Unmarshal(doc.EntryPoints[name], &entry); err != nil {
			continue
		}
		if entry.TakesAFunction {
			lines = append(lines, name+"  (takes a function: not from a shell)")
			continue
		}
		if _, known := entryPoints[name]; !known {
			lines = append(lines, name+"  (the contract has it and this program does not)")
			continue
		}
		lines = append(lines, name)
		if entry.Call != nil {
			lines = append(lines, "  the object itself: "+describe(entry.Call))
		}
		arguments := make([]string, 0, len(entry.Arguments))
		for argument := range entry.Arguments {
			arguments = append(arguments, argument)
		}
		sort.Strings(arguments)
		for _, argument := range arguments {
			var held map[string]any
			if err := json.Unmarshal(entry.Arguments[argument], &held); err != nil {
				continue
			}
			lines = append(lines, "  "+field(argument, held))
		}
	}
	return strings.Join(lines, "\n") + "\n"
}

func field(name string, held map[string]any) string {
	mark := ""
	if required, _ := held["required"].(bool); required {
		mark = "!"
	}
	return name + mark + ": " + describe(held)
}

// describe is one type of the contract, in a line: what a value must be.
func describe(held map[string]any) string {
	kind, _ := held["type"].(string)
	shown := kind
	switch kind {
	case "integer":
		shown = fmt.Sprintf("integer %v..%v", held["min"], held["max"])
	case "enum":
		shown = strings.Join(texts(held["of"]), " | ")
	case "list":
		shown = "list of " + describe(inner(held["of"]))
	case "map", "open_map", "one_of":
		shown = kind + " " + strings.Join(fieldNames(held), ", ")
	}
	if held["null_is_a_value"] == true {
		shown += " or null"
	}
	return shown
}

func inner(held any) map[string]any {
	if members, isMap := held.(map[string]any); isMap {
		return members
	}
	return map[string]any{}
}

func texts(held any) []string {
	items, isList := held.([]any)
	if !isList {
		return nil
	}
	written := make([]string, 0, len(items))
	for _, item := range items {
		written = append(written, fmt.Sprint(item))
	}
	return written
}

// fieldNames are the names a map's members go under, marked where required, in
// one line: the contract file has the whole of each.
func fieldNames(held map[string]any) []string {
	source := inner(held["fields"])
	if cases, isMap := held["cases"].(map[string]any); isMap {
		source = map[string]any{}
		for name := range cases {
			source[name] = map[string]any{}
		}
	}
	names := make([]string, 0, len(source))
	for name, member := range source {
		mark := ""
		if required, _ := inner(member)["required"].(bool); required {
			mark = "!"
		}
		names = append(names, name+mark)
	}
	sort.Strings(names)
	return names
}
