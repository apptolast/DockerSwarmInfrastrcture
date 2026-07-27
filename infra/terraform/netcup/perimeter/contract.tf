locals {
  platform_contract = yamldecode(
    file("${path.module}/../../../../config/platform.yml")
  )
  public_tcp_ports = toset(
    local.platform_contract.platform_public_tcp_ports
  )
  public_ipv6_tcp_ports = toset(
    local.platform_contract.platform_public_ipv6_tcp_ports
  )
  effective_public_tcp_ports = setsubtract(
    local.public_tcp_ports,
    local.platform_contract.platform_minecraft_public_enabled
    ? toset([])
    : toset([25565])
  )
  netcup_server_id  = local.platform_contract.platform_netcup_server_id
  netcup_server_mac = local.platform_contract.platform_netcup_server_mac
  approved_tcp_ports = toset([
    80,
    443,
    25565,
  ])
}

check "platform_contract" {
  assert {
    condition = (
      local.public_tcp_ports == local.approved_tcp_ports &&
      can(tobool(
        local.platform_contract.platform_minecraft_public_enabled
      )) &&
      length(local.public_ipv6_tcp_ports) == 0 &&
      local.netcup_server_id == 900782 &&
      local.netcup_server_mac == "16:2e:0f:f4:c4:da" &&
      can(cidrhost(
        "${local.platform_contract.platform_public_ipv6}/128",
        0
      )) &&
      can(cidrhost(
        local.platform_contract.platform_public_ipv6_prefix,
        0
      )) &&
      !contains(local.public_tcp_ports, var.ssh_port)
    )
    error_message = <<-EOT
      Netcup identity must match the audited server, public IPv4 ports must be
      exactly 80/443/25565 and application TCP over IPv6 must remain closed.
      Expanding either allowlist requires a reviewed code change.
    EOT
  }
}
