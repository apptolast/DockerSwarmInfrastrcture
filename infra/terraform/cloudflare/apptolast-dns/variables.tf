variable "cloudflare_zone_id" {
  description = "Cloudflare zone ID for apptolast.com."
  type        = string
  sensitive   = true

  validation {
    condition     = can(regex("^[a-f0-9]{32}$", var.cloudflare_zone_id))
    error_message = "cloudflare_zone_id must be a 32-character hexadecimal zone ID."
  }
}
