locals {
  root_identity = {
    contract_version    = 1
    repository_contract = "apptolast-dockerswarm-iac"
    root                = "netcup/perimeter"
    backend_type        = "s3"
    backend_key         = "netcup/perimeter/terraform.tfstate"
    workspace           = "default"
    netcup_server_id    = local.netcup_server_id
    netcup_server_mac   = local.netcup_server_mac
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
