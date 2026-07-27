variable "environment" {
  description = "Environment suffix used in provider resource names."
  type        = string
  default     = "production"

  validation {
    condition     = can(regex("^[a-z0-9][a-z0-9-]{1,30}[a-z0-9]$", var.environment))
    error_message = "environment must be 3-32 lowercase letters, digits or hyphens."
  }
}

variable "manage_firewall" {
  description = "Explicit gate for creating and assigning the Netcup firewall."
  type        = bool
  default     = false
}

variable "admin_cidrs" {
  description = "Reviewed IPv4/IPv6 source CIDRs allowed to reach SSH."
  type        = set(string)
  default     = []

  validation {
    condition = alltrue([
      for cidr in var.admin_cidrs :
      can(cidrhost(cidr, 0)) && !contains(["0.0.0.0/0", "::/0"], cidr)
    ])
    error_message = "Every admin CIDR must be valid and SSH cannot be world-open."
  }
}

variable "ssh_port" {
  description = "SSH destination port."
  type        = number
  default     = 22

  validation {
    condition     = var.ssh_port >= 1 && var.ssh_port <= 65535
    error_message = "ssh_port must be between 1 and 65535."
  }
}

variable "preserved_policy_ids" {
  description = "Must remain empty: unmanaged policies are never preserved blindly."
  type        = list(number)
  default     = []

  validation {
    condition     = length(var.preserved_policy_ids) == 0
    error_message = "Existing Netcup policies must be modeled and reviewed, never retained only by ID."
  }
}

variable "firewall_policy_inventory_confirmed" {
  description = "Explicit gate proving that every currently assigned policy was inventoried and can be replaced."
  type        = bool
  default     = false
}

variable "scp_ssh_public_keys" {
  description = "Public SSH keys registered for future SCP image installations."
  type = map(object({
    name = string
    key  = string
  }))
  default = {}

  validation {
    condition = alltrue([
      for item in values(var.scp_ssh_public_keys) :
      length(trimspace(item.name)) > 0 &&
      can(regex("^(ssh-ed25519|ssh-rsa|ecdsa-sha2-|sk-ssh-ed25519@openssh.com)", trimspace(item.key)))
    ])
    error_message = "Every SCP entry needs a name and a supported public key."
  }
}
