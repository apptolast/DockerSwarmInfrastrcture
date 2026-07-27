variable "adoption_only" {
  description = <<-EOT
    Keep every pre-existing application record on the legacy IPv4 while the
    declarative import blocks and root identity are adopted. Edge is new and
    still points to the platform IPv4. Set explicitly to true only for the
    initial adoption plan. The permanent default converges to reviewed gates.
  EOT
  type        = bool
  default     = false
}
