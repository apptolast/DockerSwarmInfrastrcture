locals {
  platform_contract = yamldecode(
    file("${path.module}/../../../../config/platform.yml")
  )
  public_tcp_ports   = toset(local.platform_contract.platform_public_tcp_ports)
  approved_tcp_ports = toset([80, 443])
  preserved_policy_ids = (
    var.preserved_policy_ids == null
    ? []
    : sort(tolist(var.preserved_policy_ids))
  )
}

check "platform_contract" {
  assert {
    condition = (
      local.public_tcp_ports == local.approved_tcp_ports &&
      !contains(local.public_tcp_ports, var.ssh_port)
    )
    error_message = <<-EOT
      Public provider ports must be exactly 80/443. Expanding this allowlist
      requires a reviewed code change; a tfvars file cannot open extra ports.
    EOT
  }
}
