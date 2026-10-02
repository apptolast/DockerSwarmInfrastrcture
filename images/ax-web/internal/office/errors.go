package office

import (
	"errors"
	"net/http"
)

// FieldError rejects one field of a request; Message is shown to the
// owner as is (Spanish).
type FieldError struct {
	Field   string
	Message string
}

func (e *FieldError) Error() string { return e.Field + ": " + e.Message }

// Error is any other refusal the web layer shows as is, with its HTTP
// status (404, 409, 502...). Message never carries a credential, a
// prompt or a GitHub response body.
type Error struct {
	Status  int
	Message string
}

func (e *Error) Error() string { return e.Message }

func fieldErr(field, msg string) error { return &FieldError{Field: field, Message: msg} }

func notFound(msg string) error { return &Error{Status: http.StatusNotFound, Message: msg} }

func conflict(msg string) error { return &Error{Status: http.StatusConflict, Message: msg} }

func upstream(msg string) error { return &Error{Status: http.StatusBadGateway, Message: msg} }

// ErrClosed is returned once the office is shutting down.
var ErrClosed = &Error{Status: http.StatusServiceUnavailable, Message: "La Oficina se está deteniendo"}

// IsStatus reports the HTTP status an error maps to: 400 for a
// FieldError, the Error's own, or 500.
func IsStatus(err error) int {
	var fe *FieldError
	if errors.As(err, &fe) {
		return http.StatusBadRequest
	}
	var oe *Error
	if errors.As(err, &oe) {
		return oe.Status
	}
	return http.StatusInternalServerError
}
