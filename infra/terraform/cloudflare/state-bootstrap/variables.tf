variable "cloudflare_account_id" {
  description = "Cloudflare account that owns the private state buckets."
  type        = string

  validation {
    condition = can(regex(
      "^[a-f0-9]{32}$",
      var.cloudflare_account_id
    ))
    error_message = "cloudflare_account_id must be 32 hexadecimal characters."
  }
}

variable "state_buckets" {
  description = "Distinct private R2 buckets for the two production roots."
  type = object({
    cloudflare_dns = object({
      name     = string
      location = string
    })
    netcup_perimeter = object({
      name     = string
      location = string
    })
  })

  validation {
    condition = alltrue([
      for bucket in values(var.state_buckets) :
      can(regex("^[a-z0-9](?:[a-z0-9-]{1,61}[a-z0-9])$", bucket.name)) &&
      contains(["apac", "eeur", "enam", "oc", "weur", "wnam"], bucket.location)
    ])
    error_message = <<-EOT
      Bucket names must be 3-63 lowercase letters, digits or hyphens and each
      location must be an R2 location hint supported by the pinned provider.
    EOT
  }

  validation {
    condition = (
      var.state_buckets.cloudflare_dns.name !=
      var.state_buckets.netcup_perimeter.name
    )
    error_message = "Every Terraform root must use a different R2 bucket."
  }
}
