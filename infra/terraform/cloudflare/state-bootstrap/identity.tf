locals {
  root_identity = {
    contract_version      = 1
    repository_contract   = "apptolast-dockerswarm-iac"
    root                  = "cloudflare/state-bootstrap"
    backend_type          = "local"
    backend_key           = ""
    workspace             = "default"
    cloudflare_account_id = "becc1c00820a0f6779e9b278ab150abf"
  }
}

resource "terraform_data" "root_identity" {
  input = local.root_identity

  lifecycle {
    prevent_destroy = true

    precondition {
      condition = (
        var.cloudflare_account_id ==
        local.root_identity.cloudflare_account_id
      )
      error_message = "The state-bootstrap account differs from the reviewed root identity."
    }
  }
}

output "root_identity" {
  description = "Fail-closed identity used to reject a wrong Terraform state."
  value       = terraform_data.root_identity.output
}
