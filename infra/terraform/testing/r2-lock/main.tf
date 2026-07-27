variable "generation" {
  description = "Unique generation used to force a disposable probe replacement."
  type        = string

  validation {
    condition     = can(regex("^[a-z0-9-]{8,80}$", var.generation))
    error_message = "generation must be a lowercase disposable identifier."
  }
}

variable "marker" {
  description = "Absolute marker path proving that the lock-holding provisioner started."
  type        = string

  validation {
    condition     = startswith(var.marker, "/")
    error_message = "marker must be an absolute temporary path."
  }
}

variable "hold_seconds" {
  description = "Seconds for which the disposable apply holds the state lock."
  type        = number

  validation {
    condition     = var.hold_seconds >= 0 && var.hold_seconds <= 60
    error_message = "hold_seconds must be between 0 and 60."
  }
}

resource "terraform_data" "lock_probe" {
  input            = var.generation
  triggers_replace = [var.generation]

  provisioner "local-exec" {
    command = "/usr/bin/touch \"$MARKER\" && /usr/bin/sleep \"$HOLD_SECONDS\""
    environment = {
      HOLD_SECONDS = tostring(var.hold_seconds)
      MARKER       = var.marker
    }
  }
}
