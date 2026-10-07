package migration

import (
	"encoding/json"
	"fmt"
	"sort"
	"strconv"
)

// object is one table of an old document (a JSON object or a TOML table)
// with where it was read, for messages: every accessor reports a value of
// the wrong type as a problem naming the file and the key.
type object struct {
	file  string
	where string
	m     map[string]any
	p     *problems
}

func newObject(file string, value any, p *problems) (object, bool) {
	m, ok := value.(map[string]any)
	if !ok {
		p.add("%s is not a JSON object or TOML table; an rlsbl record is one", file)
		return object{}, false
	}
	return object{file: file, m: m, p: p}, true
}

// key is the dotted name of k in the object, for messages.
func (o object) key(k string) string {
	if o.where == "" {
		return k
	}
	return o.where + "." + k
}

func (o object) has(k string) bool {
	_, ok := o.m[k]
	return ok
}

// keys are the object's keys, sorted.
func (o object) keys() []string {
	out := make([]string, 0, len(o.m))
	for k := range o.m {
		out = append(out, k)
	}
	sort.Strings(out)
	return out
}

func (o object) wrong(k, want string) {
	o.p.add("%s: %s must be %s, not %s", o.file, o.key(k), want, describeValue(o.m[k]))
}

// str is the string at k; found is false when the key is absent or holds
// another type (reported).
func (o object) str(k string) (string, bool) {
	v, ok := o.m[k]
	if !ok {
		return "", false
	}
	s, ok := v.(string)
	if !ok {
		o.wrong(k, "a string")
		return "", false
	}
	return s, true
}

func (o object) boolean(k string) (bool, bool) {
	v, ok := o.m[k]
	if !ok {
		return false, false
	}
	b, ok := v.(bool)
	if !ok {
		o.wrong(k, "true or false")
		return false, false
	}
	return b, true
}

// integer is the integer at k, from a JSON number or a TOML integer.
func (o object) integer(k string) (int64, bool) {
	v, ok := o.m[k]
	if !ok {
		return 0, false
	}
	switch n := v.(type) {
	case json.Number:
		i, err := strconv.ParseInt(n.String(), 10, 64)
		if err == nil {
			return i, true
		}
	case int64:
		return n, true
	}
	o.wrong(k, "an integer")
	return 0, false
}

// strings is the list of strings at k.
func (o object) strings(k string) ([]string, bool) {
	v, ok := o.m[k]
	if !ok {
		return nil, false
	}
	items, ok := asList(v)
	if !ok {
		o.wrong(k, "a list of strings")
		return nil, false
	}
	out := []string{}
	for _, item := range items {
		s, ok := item.(string)
		if !ok {
			o.wrong(k, "a list of strings")
			return nil, false
		}
		out = append(out, s)
	}
	return out, true
}

// list is the list at k.
func (o object) list(k string) ([]any, bool) {
	v, ok := o.m[k]
	if !ok {
		return nil, false
	}
	items, ok := asList(v)
	if !ok {
		o.wrong(k, "a list")
		return nil, false
	}
	return items, true
}

// table is the object at k.
func (o object) table(k string) (object, bool) {
	v, ok := o.m[k]
	if !ok {
		return object{}, false
	}
	m, ok := v.(map[string]any)
	if !ok {
		o.wrong(k, "an object")
		return object{}, false
	}
	return object{file: o.file, where: o.key(k), m: m, p: o.p}, true
}

// child is item, an element of the list at k, as an object.
func (o object) child(k string, i int, item any) (object, bool) {
	m, ok := item.(map[string]any)
	if !ok {
		o.p.add("%s: %s[%d] must be an object, not %s", o.file, o.key(k), i, describeValue(item))
		return object{}, false
	}
	return object{file: o.file, where: fmt.Sprintf("%s[%d]", o.key(k), i), m: m, p: o.p}, true
}

// asList reads a JSON array or a TOML array (of values or of tables).
func asList(v any) ([]any, bool) {
	switch l := v.(type) {
	case []any:
		return l, true
	case []map[string]any:
		out := make([]any, len(l))
		for i, m := range l {
			out[i] = m
		}
		return out, true
	}
	return nil, false
}

func describeValue(v any) string {
	switch x := v.(type) {
	case nil:
		return "null"
	case string:
		return strconv.Quote(x)
	case bool:
		return strconv.FormatBool(x)
	case json.Number:
		return x.String()
	case int64:
		return strconv.FormatInt(x, 10)
	case []any, []map[string]any:
		return "a list"
	case map[string]any:
		return "an object"
	}
	return fmt.Sprintf("%v", v)
}
