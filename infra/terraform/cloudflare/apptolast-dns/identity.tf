locals {
  root_identity = {
    contract_version      = 1
    repository_contract   = "apptolast-dockerswarm-iac"
    root                  = "cloudflare/apptolast-dns"
    backend_type          = "s3"
    backend_key           = "cloudflare/apptolast-dns/terraform.tfstate"
    workspace             = "default"
    cloudflare_account_id = local.cloudflare_account_id
    cloudflare_zone_id    = local.cloudflare_zone_id
  }
}

resource "terraform_data" "root_identity" {
  input = local.root_identity

  lifecycle {
    prevent_destroy = true
  }
}

output "root_identity" {
  description = "Fail-closed identity used to reject a wrong Terraform state."
  value       = terraform_data.root_identity.output
}
