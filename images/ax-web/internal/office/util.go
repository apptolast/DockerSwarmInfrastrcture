package office

import (
	"encoding/json"
	"errors"
	"strconv"
)

func jsonMarshal(v any) ([]byte, error) { return json.Marshal(v) }

func jsonUnmarshal(data []byte, v any) error { return json.Unmarshal(data, v) }

func asField(err error, target **FieldError) bool { return errors.As(err, target) }

func itoa(n int64) string { return strconv.FormatInt(n, 10) }
