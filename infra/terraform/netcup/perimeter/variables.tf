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

variable "server_id" {
  description = "Numeric SCP identifier of the existing server."
  type        = number
  default     = null
  nullable    = true

  validation {
    condition     = var.server_id == null || var.server_id > 0
    error_message = "server_id must be positive when supplied."
  }
}

variable "server_mac" {
  description = "MAC address of the existing public server interface."
  type        = string
  default     = null
  nullable    = true

  validation {
    condition     = var.server_mac == null || can(regex("^([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$", var.server_mac))
    error_message = "server_mac must use the form aa:bb:cc:dd:ee:ff."
  }
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
  description = "Complete set of existing Netcup policy IDs retained on assignment."
  type        = set(number)
  default     = null
  nullable    = true

  validation {
    condition = (
      var.preserved_policy_ids == null ||
      alltrue([for id in var.preserved_policy_ids : id > 0])
    )
    error_message = "Every preserved Netcup firewall policy ID must be positive."
  }
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
